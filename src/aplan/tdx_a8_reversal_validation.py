from __future__ import annotations

import argparse
import csv
import hashlib
import json
import math
import sqlite3
import tomllib
from collections import defaultdict
from dataclasses import dataclass
from datetime import UTC, date, datetime
from pathlib import Path
from statistics import mean, median, pstdev
from typing import Any, Iterable

from .announcement_event_validation import (
    MARKET_CODE,
    _benchmark_return,
    _build_bar_database,
    _canonical,
    _canonicalizer,
    _date,
    _industry_at,
    _load_benchmarks,
    _load_calendar,
    _load_security_state,
    _market_regimes,
    _number,
    _purged_development_cutoff,
    _return,
    _sha256,
)
from .tdx_a8_reversal import analyze_a8_prompt
from .trend_monitor_validation import (
    HORIZONS,
    Observation,
    _finalize_oos,
    _gate,
    _listing_dates,
    _nominate_horizon,
)


AS_OF_TRADE_DATE = "20251231"
DEVELOPMENT_END = "20241231"
PROTOCOL_SHA256 = (
    "14b56c80c772573e3ee3e3d8a195c4e637e90dfd682f1899eb58e72fe660ef5e"
)
SIGNAL_IMPLEMENTATION_SHA256 = (
    "8fe3d8f89b96da2c95d18c8260be603011aa89d2b846ba96e1899af8e1a122fb"
)
VARIANTS = (
    "exact_raw_buy",
    "exact_text_prompt",
    "text_prompt_above_ema245",
    "text_prompt_trend_aligned",
)
MINIMUM_LISTING_DAYS = 120
MINIMUM_AVERAGE_TURNOVER20 = 50_000_000.0
MATCH_COUNT = 5


@dataclass(frozen=True, slots=True)
class Candidate:
    symbol: str
    signal_date: str
    variants: tuple[str, ...]
    pre20_return: float
    drawdown20: float
    volatility20: float
    median_turnover20: float
    industry_code: str
    red_hold_state: bool


def _protocol(project: Path) -> dict[str, Any]:
    path = project / "config" / "tdx_a8_reversal_validation.toml"
    lock_path = project / "config" / "tdx_a8_reversal_validation.lock.json"
    signal_path = project / "src" / "aplan" / "tdx_a8_reversal.py"
    if not path.exists() or not lock_path.exists() or not signal_path.exists():
        raise ValueError("缺少 A8 冻结规范、锁文件或信号实现")
    protocol_hash = _sha256(path)
    signal_hash = _sha256(signal_path)
    lock = json.loads(lock_path.read_text(encoding="utf-8"))
    document = tomllib.loads(path.read_text(encoding="utf-8"))
    if protocol_hash != PROTOCOL_SHA256 or lock.get("sha256") != PROTOCOL_SHA256:
        raise ValueError("A8 冻结规范哈希不匹配")
    if (
        signal_hash != SIGNAL_IMPLEMENTATION_SHA256
        or lock.get("signal_implementation_sha256")
        != SIGNAL_IMPLEMENTATION_SHA256
    ):
        raise ValueError("A8 信号实现哈希不匹配")
    if document.get("final_holdout_opened") is not False:
        raise ValueError("A8 冻结规范必须保持 final_holdout_opened=false")
    if (
        _date(document.get("time_design", {}).get("research_as_of_trade_date"))
        != AS_OF_TRADE_DATE
    ):
        raise ValueError("A8 冻结规范 as_of_trade_date 已变化")
    if set(document.get("variants", {})) != set(VARIANTS):
        raise ValueError("A8 冻结变体集合已变化")
    if document.get("formula_audit", {}).get("cost90_status") != (
        "data_unavailable_display_context_only"
    ):
        raise ValueError("A8 COST90 冻结状态已变化")
    return {
        "path": str(path),
        "sha256": protocol_hash,
        "lock_path": str(lock_path),
        "lock_sha256": _sha256(lock_path),
        "document": document,
    }


def _days_listed(list_date: str, signal_date: str) -> int:
    return (
        date(
            int(signal_date[:4]),
            int(signal_date[4:6]),
            int(signal_date[6:8]),
        )
        - date(
            int(list_date[:4]),
            int(list_date[4:6]),
            int(list_date[6:8]),
        )
    ).days


def _delisting_dates(
    path: Path,
    aliases: dict[str, str],
) -> dict[str, str]:
    output: dict[str, str] = {}
    with path.open(encoding="utf-8-sig", newline="") as handle:
        for row in csv.DictReader(handle):
            symbol = _canonical(
                str(row.get("symbol") or "").strip(),
                aliases,
            )
            delisted = _date(row.get("delist_date"))
            if len(symbol) == 6 and delisted:
                output[symbol] = delisted
    return output


def _window_features(
    closes: list[float],
    turnovers: list[float],
    index: int,
) -> tuple[float, float, float, float] | None:
    if index < 21:
        return None
    previous_closes = closes[index - 21 : index]
    previous_turnovers = turnovers[index - 20 : index]
    if (
        len(previous_closes) != 21
        or len(previous_turnovers) != 20
        or previous_closes[0] <= 0
    ):
        return None
    returns = [
        previous_closes[position] / previous_closes[position - 1] - 1
        for position in range(1, len(previous_closes))
        if previous_closes[position - 1] > 0
    ]
    peak = previous_closes[1]
    drawdown = 0.0
    for close in previous_closes[1:]:
        peak = max(peak, close)
        drawdown = max(drawdown, 1 - close / peak)
    return (
        previous_closes[-1] / previous_closes[0] - 1,
        drawdown,
        pstdev(returns) if len(returns) >= 2 else 0.0,
        median(previous_turnovers),
    )


def _variant_names(snapshot: Any) -> tuple[str, ...]:
    output: list[str] = []
    if snapshot.raw_buy:
        output.append("exact_raw_buy")
    if snapshot.filtered_buy:
        output.append("exact_text_prompt")
        if snapshot.above_ema245:
            output.append("text_prompt_above_ema245")
        if snapshot.trend_aligned:
            output.append("text_prompt_trend_aligned")
    return tuple(output)


def _feature_database(
    project: Path,
    bar_database: Path,
    *,
    end_date: str,
    source_signature: str,
) -> tuple[Path, dict[str, Any]]:
    state = project / "state" / "tdx_a8_reversal_validation"
    state.mkdir(parents=True, exist_ok=True)
    database = state / f"qfq_features_20230101_{end_date}.sqlite3"
    connection = sqlite3.connect(database)
    existing = connection.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='metadata'"
    ).fetchone()
    if existing:
        signature = connection.execute(
            "SELECT value FROM metadata WHERE key='source_signature'"
        ).fetchone()
        count = connection.execute("SELECT COUNT(*) FROM features").fetchone()[0]
        if signature and signature[0] == source_signature and count:
            reset_count = connection.execute(
                "SELECT value FROM metadata WHERE key='contiguous_resets'"
            ).fetchone()
            connection.close()
            return database, {
                "status": "reused",
                "rows": count,
                "contiguous_history_resets": (
                    int(reset_count[0]) if reset_count else None
                ),
                "source_signature": source_signature,
            }
    connection.executescript(
        """
        DROP TABLE IF EXISTS metadata;
        DROP TABLE IF EXISTS features;
        CREATE TABLE metadata (key TEXT PRIMARY KEY, value TEXT NOT NULL);
        CREATE TABLE features (
            trade_date TEXT NOT NULL,
            symbol TEXT NOT NULL,
            exact_raw_buy INTEGER NOT NULL,
            exact_text_prompt INTEGER NOT NULL,
            text_prompt_above_ema245 INTEGER NOT NULL,
            text_prompt_trend_aligned INTEGER NOT NULL,
            red_hold_state INTEGER NOT NULL,
            pre20_return REAL,
            drawdown20 REAL,
            volatility20 REAL,
            median_turnover20 REAL,
            PRIMARY KEY (trade_date, symbol)
        );
        CREATE INDEX idx_a8_features_symbol_date
        ON features(symbol, trade_date);
        """
    )
    connection.execute("PRAGMA journal_mode=WAL")
    connection.execute("PRAGMA synchronous=NORMAL")
    bars = sqlite3.connect(bar_database)
    bars.row_factory = sqlite3.Row
    dates = [
        row[0]
        for row in bars.execute(
            "SELECT DISTINCT trade_date FROM bars "
            "WHERE trade_date<=? ORDER BY trade_date",
            (end_date,),
        )
    ]
    date_index = {day: index for index, day in enumerate(dates)}
    cursor = bars.execute(
        "SELECT symbol,trade_date,close,turnover FROM bars "
        "WHERE trade_date<=? ORDER BY symbol,trade_date",
        (end_date,),
    )
    current_symbol = ""
    run: list[sqlite3.Row] = []
    rows_written = 0
    resets = 0
    symbols_processed = 0

    def flush(rows: list[sqlite3.Row]) -> int:
        if not rows:
            return 0
        closes = [float(row["close"]) for row in rows]
        turnovers = [float(row["turnover"]) for row in rows]
        snapshots = analyze_a8_prompt(closes, warmup_bars=60)
        incoming = []
        for index, (row, snapshot) in enumerate(zip(rows, snapshots, strict=True)):
            features = _window_features(closes, turnovers, index)
            incoming.append(
                (
                    row["trade_date"],
                    row["symbol"],
                    int(snapshot.raw_buy),
                    int(snapshot.filtered_buy),
                    int(snapshot.filtered_buy and snapshot.above_ema245),
                    int(snapshot.filtered_buy and snapshot.trend_aligned),
                    int(snapshot.red_hold_state),
                    *(features or (None, None, None, None)),
                )
            )
        connection.executemany(
            "INSERT OR REPLACE INTO features VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            incoming,
        )
        return len(incoming)

    last_index: int | None = None
    for row in cursor:
        symbol = str(row["symbol"])
        index = date_index[str(row["trade_date"])]
        if symbol != current_symbol:
            rows_written += flush(run)
            if current_symbol:
                symbols_processed += 1
                if symbols_processed % 100 == 0:
                    connection.commit()
                if symbols_processed % 500 == 0:
                    print(
                        f"A8 特征扫描：symbols={symbols_processed}，"
                        f"rows={rows_written}",
                        flush=True,
                    )
            current_symbol = symbol
            run = []
            last_index = None
        if last_index is not None and index != last_index + 1:
            rows_written += flush(run)
            run = []
            resets += 1
        run.append(row)
        last_index = index
    rows_written += flush(run)
    if current_symbol:
        symbols_processed += 1
    connection.executemany(
        "INSERT INTO metadata VALUES (?,?)",
        (
            ("source_signature", source_signature),
            ("contiguous_resets", str(resets)),
        ),
    )
    connection.commit()
    connection.close()
    bars.close()
    return database, {
        "status": "built",
        "rows": rows_written,
        "symbols": symbols_processed,
        "contiguous_history_resets": resets,
        "source_signature": source_signature,
    }


def _raw_signal_sets(
    project: Path,
    aliases: dict[str, str],
    dates: list[str],
    *,
    end_date: str,
    signature: str,
) -> tuple[dict[str, set[tuple[str, str]]], dict[str, Any]]:
    state = project / "state" / "tdx_a8_reversal_validation"
    state.mkdir(parents=True, exist_ok=True)
    database = state / f"raw_closes_20230101_{end_date}.sqlite3"
    connection = sqlite3.connect(database)
    connection.execute(
        "CREATE TABLE IF NOT EXISTS metadata "
        "(key TEXT PRIMARY KEY, value TEXT NOT NULL)"
    )
    existing = connection.execute(
        "SELECT value FROM metadata WHERE key='source_signature'"
    ).fetchone()
    has_prices = connection.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='prices'"
    ).fetchone()
    if not (existing and existing[0] == signature and has_prices):
        connection.executescript(
            """
            DROP TABLE IF EXISTS prices;
            DELETE FROM metadata;
            CREATE TABLE prices (
                symbol TEXT NOT NULL,
                trade_date TEXT NOT NULL,
                close REAL NOT NULL,
                PRIMARY KEY(symbol, trade_date)
            );
            """
        )
        raw_dir = project / "data" / "processed" / "yinhe_daily"
        for day_number, day in enumerate(dates, 1):
            path = raw_dir / f"{day}.csv"
            if not path.exists():
                continue
            incoming = []
            with path.open(encoding="utf-8-sig", newline="") as handle:
                for row in csv.DictReader(handle):
                    symbol = _canonical(
                        str(row.get("symbol") or "").strip(),
                        aliases,
                    )
                    close = _number(row.get("close"))
                    if len(symbol) == 6 and close is not None and close > 0:
                        incoming.append((symbol, day, close))
            connection.executemany(
                "INSERT OR REPLACE INTO prices VALUES (?,?,?)",
                incoming,
            )
            if day_number % 100 == 0:
                connection.commit()
        connection.execute(
            "INSERT INTO metadata VALUES ('source_signature',?)",
            (signature,),
        )
        connection.commit()
    date_index = {day: index for index, day in enumerate(dates)}
    signals = {variant: set() for variant in VARIANTS}
    current_symbol = ""
    run_dates: list[str] = []
    closes: list[float] = []
    last_index: int | None = None
    resets = 0

    def flush() -> None:
        if not closes:
            return
        snapshots = analyze_a8_prompt(closes, warmup_bars=60)
        for day, snapshot in zip(run_dates, snapshots, strict=True):
            for variant in _variant_names(snapshot):
                signals[variant].add((day, current_symbol))

    for symbol, day, close in connection.execute(
        "SELECT symbol,trade_date,close FROM prices ORDER BY symbol,trade_date"
    ):
        index = date_index[str(day)]
        if symbol != current_symbol:
            flush()
            current_symbol = str(symbol)
            run_dates = []
            closes = []
            last_index = None
        if last_index is not None and index != last_index + 1:
            flush()
            run_dates = []
            closes = []
            resets += 1
        run_dates.append(str(day))
        closes.append(float(close))
        last_index = index
    flush()
    rows = connection.execute("SELECT COUNT(*) FROM prices").fetchone()[0]
    connection.close()
    return signals, {
        "rows": rows,
        "contiguous_history_resets": resets,
        "signals": {key: len(value) for key, value in signals.items()},
    }


def _load_candidates(
    feature_database: Path,
    security_database: Path,
    listing_dates: dict[str, str],
    delisting_dates: dict[str, str],
    industry_by_symbol: dict[str, list[tuple[str, str, str]]],
    *,
    end_date: str,
) -> tuple[dict[str, list[Candidate]], dict[str, Any]]:
    features = sqlite3.connect(feature_database)
    features.row_factory = sqlite3.Row
    statuses = sqlite3.connect(security_database)
    statuses.row_factory = sqlite3.Row
    output: dict[str, list[Candidate]] = defaultdict(list)
    counters: dict[str, int] = defaultdict(int)
    query = (
        "SELECT * FROM features WHERE trade_date<=? AND "
        "(exact_raw_buy=1 OR exact_text_prompt=1 OR "
        "text_prompt_above_ema245=1 OR text_prompt_trend_aligned=1) "
        "ORDER BY trade_date,symbol"
    )
    try:
        for row in features.execute(query, (end_date,)):
            counters["raw_signal_grains"] += 1
            required = (
                row["pre20_return"],
                row["drawdown20"],
                row["volatility20"],
                row["median_turnover20"],
            )
            if any(value is None for value in required):
                counters["missing_match_features"] += 1
                continue
            if float(row["median_turnover20"]) < MINIMUM_AVERAGE_TURNOVER20:
                counters["below_liquidity_threshold"] += 1
                continue
            state = statuses.execute(
                "SELECT is_st,is_suspended "
                "FROM daily_status WHERE trade_date=? AND symbol=?",
                (row["trade_date"], row["symbol"]),
            ).fetchone()
            if not state:
                counters["missing_signal_security_state"] += 1
                continue
            counters["signal_security_state_joined"] += 1
            if state["is_st"]:
                counters["excluded_st"] += 1
                continue
            if state["is_suspended"]:
                counters["excluded_signal_suspended"] += 1
                continue
            if (
                delisting_dates.get(row["symbol"])
                and row["trade_date"] >= delisting_dates[row["symbol"]]
            ):
                counters["excluded_delisting_risk"] += 1
                continue
            listed = listing_dates.get(row["symbol"])
            if (
                not listed
                or _days_listed(listed, row["trade_date"])
                < MINIMUM_LISTING_DAYS
            ):
                counters["excluded_listing_age"] += 1
                continue
            variants = tuple(
                variant for variant in VARIANTS if row[variant]
            )
            industry = _industry_at(
                row["symbol"],
                row["trade_date"],
                industry_by_symbol,
            )
            if not industry:
                counters["missing_signal_industry"] += 1
            output[row["trade_date"]].append(
                Candidate(
                    symbol=row["symbol"],
                    signal_date=row["trade_date"],
                    variants=variants,
                    pre20_return=float(row["pre20_return"]),
                    drawdown20=float(row["drawdown20"]),
                    volatility20=float(row["volatility20"]),
                    median_turnover20=float(row["median_turnover20"]),
                    industry_code=industry,
                    red_hold_state=bool(row["red_hold_state"]),
                )
            )
    finally:
        features.close()
        statuses.close()
    denominator = (
        counters["signal_security_state_joined"]
        + counters["missing_signal_security_state"]
    )
    counters["eligible_signal_grains"] = sum(map(len, output.values()))
    counters["signal_security_state_join_coverage"] = (
        counters["signal_security_state_joined"] / denominator
        if denominator
        else None
    )
    counters["industry_join_coverage"] = (
        (
            counters["eligible_signal_grains"]
            - counters["missing_signal_industry"]
        )
        / counters["eligible_signal_grains"]
        if counters["eligible_signal_grains"]
        else None
    )
    counters["variant_signals"] = {
        variant: sum(
            variant in candidate.variants
            for candidates in output.values()
            for candidate in candidates
        )
        for variant in VARIANTS
    }
    return output, dict(counters)


def _matched_controls(
    bars: sqlite3.Connection,
    features: sqlite3.Connection,
    statuses: sqlite3.Connection,
    candidate: Candidate,
    entry_date: str,
    exit_dates: dict[int, str],
    industry_members: dict[str, list[str]],
    industry_by_symbol: dict[str, list[tuple[str, str, str]]],
    excluded: set[str],
    listing_dates: dict[str, str],
    delisting_dates: dict[str, str],
) -> dict[int, float]:
    if not candidate.industry_code:
        return {}
    members = [
        symbol
        for symbol in industry_members.get(candidate.industry_code, [])
        if symbol not in excluded
        and _industry_at(
            symbol,
            candidate.signal_date,
            industry_by_symbol,
        )
        == candidate.industry_code
    ]
    rows: list[sqlite3.Row] = []
    for start in range(0, len(members), 800):
        group = members[start : start + 800]
        if not group:
            continue
        placeholders = ",".join("?" for _ in group)
        rows.extend(
            features.execute(
                f"SELECT * FROM features WHERE trade_date=? "
                f"AND symbol IN ({placeholders}) "
                "AND pre20_return IS NOT NULL AND drawdown20 IS NOT NULL "
                "AND volatility20 IS NOT NULL AND median_turnover20>=?",
                (
                    candidate.signal_date,
                    *group,
                    MINIMUM_AVERAGE_TURNOVER20,
                ),
            ).fetchall()
        )
    scored: list[tuple[float, str]] = []
    for row in rows:
        if any(row[variant] for variant in VARIANTS):
            continue
        state = statuses.execute(
            "SELECT is_st,is_suspended FROM daily_status "
            "WHERE trade_date=? AND symbol=?",
            (candidate.signal_date, row["symbol"]),
        ).fetchone()
        if (
            not state
            or state["is_st"]
            or state["is_suspended"]
            or (
                delisting_dates.get(row["symbol"])
                and candidate.signal_date >= delisting_dates[row["symbol"]]
            )
        ):
            continue
        listed = listing_dates.get(row["symbol"])
        if (
            not listed
            or _days_listed(listed, candidate.signal_date)
            < MINIMUM_LISTING_DAYS
        ):
            continue
        distance = (
            abs(float(row["pre20_return"]) - candidate.pre20_return) / 0.05
            + abs(float(row["drawdown20"]) - candidate.drawdown20) / 0.05
            + abs(float(row["volatility20"]) - candidate.volatility20) / 0.02
            + abs(
                math.log(
                    float(row["median_turnover20"])
                    / candidate.median_turnover20
                )
            )
        )
        scored.append((distance, str(row["symbol"])))
    selected: list[str] = []
    for _, symbol in sorted(scored):
        state = statuses.execute(
            "SELECT is_st,is_suspended FROM daily_status "
            "WHERE trade_date=? AND symbol=?",
            (entry_date, symbol),
        ).fetchone()
        entry = bars.execute(
            "SELECT * FROM bars WHERE trade_date=? AND symbol=?",
            (entry_date, symbol),
        ).fetchone()
        if (
            not state
            or state["is_st"]
            or state["is_suspended"]
            or (
                delisting_dates.get(symbol)
                and entry_date >= delisting_dates[symbol]
            )
            or not entry
            or entry["is_suspended"]
            or (entry["is_limit_up"] and entry["high"] == entry["low"])
        ):
            continue
        selected.append(symbol)
        if len(selected) == MATCH_COUNT:
            break
    output: dict[int, list[float]] = defaultdict(list)
    for symbol in selected:
        entry = bars.execute(
            "SELECT open FROM bars WHERE trade_date=? AND symbol=?",
            (entry_date, symbol),
        ).fetchone()
        for horizon, exit_date in exit_dates.items():
            exit_row = bars.execute(
                "SELECT close FROM bars WHERE trade_date=? AND symbol=?",
                (exit_date, symbol),
            ).fetchone()
            if entry and exit_row:
                _, net = _return(float(entry["open"]), float(exit_row["close"]))
                output[horizon].append(net)
    return {
        horizon: mean(values)
        for horizon, values in output.items()
        if values
    }


def _evaluate(
    bar_database: Path,
    feature_database: Path,
    security_database: Path,
    dates: list[str],
    candidates: dict[str, list[Candidate]],
    market: dict[tuple[str, str], tuple[float, float]],
    industry_daily: dict[tuple[str, str], tuple[float, float]],
    industry_by_symbol: dict[str, list[tuple[str, str, str]]],
    industry_members: dict[str, list[str]],
    listing_dates: dict[str, str],
    delisting_dates: dict[str, str],
    *,
    start_date: str,
    end_date: str,
    variants: Iterable[str],
) -> tuple[list[Observation], dict[str, Any]]:
    bars = sqlite3.connect(bar_database)
    bars.row_factory = sqlite3.Row
    features = sqlite3.connect(feature_database)
    features.row_factory = sqlite3.Row
    statuses = sqlite3.connect(security_database)
    statuses.row_factory = sqlite3.Row
    date_index = {day: index for index, day in enumerate(dates)}
    regimes = _market_regimes(market, dates)
    observations: list[Observation] = []
    counters: dict[str, int] = defaultdict(int)
    selected_variants = set(variants)
    controls_cache: dict[tuple[str, str], dict[int, float]] = {}
    try:
        for signal_date in dates:
            if not start_date <= signal_date <= end_date:
                continue
            signal_index = date_index[signal_date]
            if signal_index + 1 >= len(dates):
                continue
            entry_date = dates[signal_index + 1]
            exit_dates = {
                horizon: dates[signal_index + horizon]
                for horizon in HORIZONS
                if signal_index + horizon < len(dates)
            }
            day_candidates = candidates.get(signal_date, [])
            excluded = {item.symbol for item in day_candidates}
            for candidate in day_candidates:
                for variant in candidate.variants:
                    if variant not in selected_variants:
                        continue
                    counters[f"{variant}_signals"] += 1
                    state = statuses.execute(
                        "SELECT is_st,is_suspended "
                        "FROM daily_status WHERE trade_date=? AND symbol=?",
                        (entry_date, candidate.symbol),
                    ).fetchone()
                    if not state:
                        counters["missing_entry_security_state"] += 1
                        continue
                    counters["entry_security_state_joined"] += 1
                    entry = bars.execute(
                        "SELECT * FROM bars WHERE trade_date=? AND symbol=?",
                        (entry_date, candidate.symbol),
                    ).fetchone()
                    if (
                        state["is_st"]
                        or state["is_suspended"]
                        or (
                            delisting_dates.get(candidate.symbol)
                            and entry_date >= delisting_dates[candidate.symbol]
                        )
                        or not entry
                        or entry["is_suspended"]
                    ):
                        counters["untradeable_suspended_st_or_delisting"] += 1
                        continue
                    if entry["is_limit_up"] and entry["high"] == entry["low"]:
                        counters["untradeable_one_price_limit_up"] += 1
                        continue
                    control_key = (candidate.signal_date, candidate.symbol)
                    controls = controls_cache.get(control_key)
                    if controls is None:
                        controls = _matched_controls(
                            bars,
                            features,
                            statuses,
                            candidate,
                            entry_date,
                            exit_dates,
                            industry_members,
                            industry_by_symbol,
                            excluded,
                            listing_dates,
                            delisting_dates,
                        )
                        controls_cache[control_key] = controls
                    for horizon, exit_date in exit_dates.items():
                        exit_row = bars.execute(
                            "SELECT close FROM bars "
                            "WHERE trade_date=? AND symbol=?",
                            (exit_date, candidate.symbol),
                        ).fetchone()
                        if not exit_row:
                            counters["missing_exit_bar"] += 1
                            continue
                        gross, net = _return(
                            float(entry["open"]),
                            float(exit_row["close"]),
                        )
                        market_return = _benchmark_return(
                            market,
                            MARKET_CODE,
                            entry_date,
                            exit_date,
                        )
                        industry_return = (
                            _benchmark_return(
                                industry_daily,
                                candidate.industry_code,
                                entry_date,
                                exit_date,
                            )
                            if candidate.industry_code
                            else None
                        )
                        matched_return = controls.get(horizon)
                        observations.append(
                            Observation(
                                variant=variant,
                                period=(
                                    "development"
                                    if signal_date <= DEVELOPMENT_END
                                    else "rolling_oos"
                                ),
                                horizon=horizon,
                                signal_date=signal_date,
                                entry_date=entry_date,
                                symbol=candidate.symbol,
                                offset=0,
                                industry_code=candidate.industry_code,
                                gross_return=gross,
                                net_return=net,
                                market_excess=(
                                    (1 + net) / (1 + market_return) - 1
                                    if market_return is not None
                                    and market_return > -1
                                    else None
                                ),
                                industry_excess=(
                                    (1 + net) / (1 + industry_return) - 1
                                    if industry_return is not None
                                    and industry_return > -1
                                    else None
                                ),
                                matched_excess=(
                                    (1 + net) / (1 + matched_return) - 1
                                    if matched_return is not None
                                    and matched_return > -1
                                    else None
                                ),
                                market_regime=regimes.get(
                                    signal_date,
                                    "insufficient",
                                ),
                            )
                        )
    finally:
        bars.close()
        features.close()
        statuses.close()
    total_entry = (
        counters["entry_security_state_joined"]
        + counters["missing_entry_security_state"]
    )
    total_signals = sum(counters[f"{variant}_signals"] for variant in variants)
    counters["entry_security_state_join_coverage"] = (
        counters["entry_security_state_joined"] / total_entry
        if total_entry
        else None
    )
    counters["untradeable_rate"] = (
        (
            counters["untradeable_suspended_st_or_delisting"]
            + counters["untradeable_one_price_limit_up"]
        )
        / total_signals
        if total_signals
        else None
    )
    return observations, dict(counters)


def _sensitivity(
    qfq_signals: dict[str, set[tuple[str, str]]],
    raw_signals: dict[str, set[tuple[str, str]]],
    security_database: Path | None = None,
) -> dict[str, Any]:
    statuses = sqlite3.connect(security_database) if security_database else None
    output: dict[str, Any] = {}
    try:
        for variant in VARIANTS:
            qfq = qfq_signals[variant]
            raw = raw_signals[variant]
            union = qfq | raw
            raw_only = raw - qfq
            qfq_only = qfq - raw

            def corporate_actions(values: set[tuple[str, str]]) -> int | None:
                if statuses is None:
                    return None
                return sum(
                    bool(
                        statuses.execute(
                            "SELECT 1 FROM daily_status "
                            "WHERE trade_date=? AND symbol=? "
                            "AND (is_ex_dividend=1 OR is_ex_right=1)",
                            (day, symbol),
                        ).fetchone()
                    )
                    for day, symbol in values
                )

            output[variant] = {
                "qfq_signals": len(qfq),
                "raw_signals": len(raw),
                "overlap": len(qfq & raw),
                "qfq_only": len(qfq_only),
                "raw_only": len(raw_only),
                "jaccard": len(qfq & raw) / len(union) if union else None,
                "raw_only_on_corporate_action_date": corporate_actions(raw_only),
                "qfq_only_on_corporate_action_date": corporate_actions(qfq_only),
            }
    finally:
        if statuses is not None:
            statuses.close()
    return output


def _feature_signal_sets(
    feature_database: Path,
) -> dict[str, set[tuple[str, str]]]:
    connection = sqlite3.connect(feature_database)
    output = {variant: set() for variant in VARIANTS}
    try:
        for row in connection.execute(
            "SELECT trade_date,symbol,exact_raw_buy,exact_text_prompt,"
            "text_prompt_above_ema245,text_prompt_trend_aligned "
            "FROM features WHERE exact_raw_buy=1 OR exact_text_prompt=1 "
            "OR text_prompt_above_ema245=1 "
            "OR text_prompt_trend_aligned=1"
        ):
            for index, variant in enumerate(VARIANTS, 2):
                if row[index]:
                    output[variant].add((str(row[0]), str(row[1])))
    finally:
        connection.close()
    return output


def _red_hold_diagnostic(
    candidates: dict[str, list[Candidate]],
    feature_database: Path | None = None,
    dates: list[str] | None = None,
) -> dict[str, Any]:
    features = sqlite3.connect(feature_database) if feature_database else None
    date_index = {day: index for index, day in enumerate(dates or [])}
    output: dict[str, Any] = {}
    try:
        for variant in VARIANTS:
            selected = [
                candidate
                for day_candidates in candidates.values()
                for candidate in day_candidates
                if variant in candidate.variants
            ]
            future: dict[str, dict[str, int | float | None]] = {}
            for horizon in HORIZONS:
                observed = 0
                red = 0
                if features is not None:
                    for candidate in selected:
                        index = date_index.get(candidate.signal_date)
                        if index is None or index + horizon >= len(date_index):
                            continue
                        future_date = (dates or [])[index + horizon]
                        row = features.execute(
                            "SELECT red_hold_state FROM features "
                            "WHERE trade_date=? AND symbol=?",
                            (future_date, candidate.symbol),
                        ).fetchone()
                        if row:
                            observed += 1
                            red += bool(row[0])
                future[str(horizon)] = {
                    "observations": observed,
                    "red_hold_rate": red / observed if observed else None,
                }
            output[variant] = {
                "signals": len(selected),
                "red_hold_at_signal": sum(
                    candidate.red_hold_state for candidate in selected
                ),
                "red_hold_at_signal_rate": (
                    sum(candidate.red_hold_state for candidate in selected)
                    / len(selected)
                    if selected
                    else None
                ),
                "future_red_hold_rate": future,
                "role": "separate_holding_or_exit_diagnostic_only",
            }
    finally:
        if features is not None:
            features.close()
    return output


def _development_increment(
    variants: dict[str, Any],
    base: str,
    filtered: str,
) -> dict[str, Any]:
    output: dict[str, Any] = {}
    for horizon in HORIZONS:
        base_metrics = variants[base]["horizons"][str(horizon)][
            "development"
        ]["metrics"]
        filtered_metrics = variants[filtered]["horizons"][str(horizon)][
            "development"
        ]["metrics"]
        output[str(horizon)] = {
            "median_net_return_delta": (
                filtered_metrics.get("median_net_return")
                - base_metrics.get("median_net_return")
                if filtered_metrics.get("median_net_return") is not None
                and base_metrics.get("median_net_return") is not None
                else None
            ),
            "median_matched_excess_delta": (
                filtered_metrics.get("median_matched_excess")
                - base_metrics.get("median_matched_excess")
                if filtered_metrics.get("median_matched_excess") is not None
                and base_metrics.get("median_matched_excess") is not None
                else None
            ),
        }
    return output


def _report_markdown(summary: dict[str, Any]) -> str:
    lines = [
        "# 通达信 A8 短反转首轮冻结验证",
        "",
        f"- 状态：`{summary['status']}`",
        f"- 冻结规范：`{summary['protocol']['sha256']}`",
        f"- 信号实现：`{summary['model']['signal_implementation_sha256']}`",
        f"- 验证实现：`{summary['model']['implementation_sha256']}`",
        f"- 有效训练截止：`{summary['effective_development_signal_end']}`",
        "- 2026 最终留出集：`sealed`",
        "- COST90：`data_unavailable`",
        "",
        "| 变体 | 训练信号 | 训练提名周期 | 2025最终门控 |",
        "|---|---:|---:|---|",
    ]
    for variant, result in summary["variants"].items():
        nomination = result["training_nomination"]["horizon"]
        oos = result.get("rolling_oos_final_decision", "not_opened")
        count = summary["data"]["training_scan_audit"]["variant_signals"].get(
            variant, 0
        )
        lines.append(f"| {variant} | {count} | {nomination or '-'} | {oos} |")
    lines.extend(
        [
            "",
            "## 训练门控",
            "",
            "| 变体 | 周期 | 门控 | 样本 | cohort | 中位净收益 | 中位匹配超额 |",
            "|---|---:|---|---:|---:|---:|---:|",
        ]
    )
    fmt = lambda value: "NA" if value is None else f"{value:.2%}"
    for variant, result in summary["variants"].items():
        for horizon, values in result["horizons"].items():
            metrics = values["development"]["metrics"]
            lines.append(
                f"| {variant} | {horizon} | "
                f"{values['development']['gate_decision']} | "
                f"{metrics.get('observations', 0)} | "
                f"{metrics.get('cohorts', 0)} | "
                f"{fmt(metrics.get('median_net_return'))} | "
                f"{fmt(metrics.get('median_matched_excess'))} |"
            )
    lines.extend(
        [
            "",
            "## 隔离审计",
            "",
            f"- FILTER 信号机会成本："
            f"{summary['ablation']['filter_opportunity_cost']}",
            f"- QFQ/Raw 敏感性："
            f"{summary['ablation']['raw_qfq_sensitivity']}",
            f"- VAR 持有轴：独立诊断，未进入买点或评分。",
            f"- 趋势过滤：明确属于新增变体，未改写原始买点。",
            "",
        ]
    )
    lines.extend(f"- {item}" for item in summary["caveats"])
    return "\n".join(lines) + "\n"


def run_validation(project: Path) -> dict[str, Any]:
    project = project.resolve()
    protocol = _protocol(project)
    aliases, alias_hash = _canonicalizer(
        project
        / "data"
        / "processed"
        / "security_history"
        / "security_aliases.csv"
    )
    bar_database, all_dates, bar_cache = _build_bar_database(
        project,
        aliases,
        alias_hash,
    )
    dates = [
        day for day in all_dates if "20230101" <= day <= AS_OF_TRADE_DATE
    ]
    if not dates or any(day > AS_OF_TRADE_DATE for day in dates):
        raise AssertionError("2026 行情进入 A8 冻结验证")
    calendar_path = project / "data" / "processed" / "trade_calendar.csv"
    official_dates = [
        day
        for day in _load_calendar(calendar_path)
        if "20230101" <= day <= AS_OF_TRADE_DATE
    ]
    if dates != official_dates:
        missing = sorted(set(official_dates) - set(dates))
        raise ValueError(
            "银河行情与官方交易日历不一致；"
            f"missing={missing[:10]} count={len(missing)}"
        )
    development_cutoff = _purged_development_cutoff(
        dates,
        DEVELOPMENT_END,
        60,
    )
    security_database, security_hashes = _load_security_state(project)
    (
        market,
        industry_daily,
        industry_by_symbol,
        industry_members,
        benchmark_hashes,
    ) = _load_benchmarks(project, aliases)
    security_master_path = (
        project
        / "data"
        / "processed"
        / "security_history"
        / "security_master.csv"
    )
    listing_dates = _listing_dates(security_master_path, aliases)
    delisting_dates = _delisting_dates(security_master_path, aliases)
    feature_signature = hashlib.sha256(
        json.dumps(
            {
                "bar_cache": bar_cache,
                "signal_sha256": SIGNAL_IMPLEMENTATION_SHA256,
                "end": development_cutoff,
            },
            sort_keys=True,
        ).encode()
    ).hexdigest()
    training_feature_db, training_feature_audit = _feature_database(
        project,
        bar_database,
        end_date=development_cutoff,
        source_signature=feature_signature,
    )
    training_candidates, training_scan_audit = _load_candidates(
        training_feature_db,
        security_database,
        listing_dates,
        delisting_dates,
        industry_by_symbol,
        end_date=development_cutoff,
    )
    training_observations, training_evaluation_audit = _evaluate(
        bar_database,
        training_feature_db,
        security_database,
        dates,
        training_candidates,
        market,
        industry_daily,
        industry_by_symbol,
        industry_members,
        listing_dates,
        delisting_dates,
        start_date="20230101",
        end_date=development_cutoff,
        variants=VARIANTS,
    )
    date_index = {day: index for index, day in enumerate(dates)}
    variants: dict[str, Any] = {}
    nominations: dict[str, int | None] = {}
    for variant in VARIANTS:
        development = {
            horizon: _gate(
                [
                    row
                    for row in training_observations
                    if row.variant == variant and row.horizon == horizon
                ],
                date_index,
            )
            for horizon in HORIZONS
        }
        nomination = _nominate_horizon(development)
        nominations[variant] = nomination
        variants[variant] = {
            "training_nomination": {
                "horizon": nomination,
                "selection_source": "development_2023_2024_only",
            },
            "horizons": {
                str(horizon): {"development": development[horizon]}
                for horizon in HORIZONS
            },
            "rolling_oos_final_decision": "not_opened_no_training_nomination",
        }
    nominated_variants = {
        variant for variant, horizon in nominations.items() if horizon is not None
    }
    full_feature_audit: dict[str, Any] | None = None
    oos_scan_audit: dict[str, Any] | None = None
    oos_evaluation_audit: dict[str, Any] | None = None
    if nominated_variants:
        full_signature = hashlib.sha256(
            json.dumps(
                {
                    "bar_cache": bar_cache,
                    "signal_sha256": SIGNAL_IMPLEMENTATION_SHA256,
                    "end": AS_OF_TRADE_DATE,
                },
                sort_keys=True,
            ).encode()
        ).hexdigest()
        full_feature_db, full_feature_audit = _feature_database(
            project,
            bar_database,
            end_date=AS_OF_TRADE_DATE,
            source_signature=full_signature,
        )
        full_candidates, oos_scan_audit = _load_candidates(
            full_feature_db,
            security_database,
            listing_dates,
            delisting_dates,
            industry_by_symbol,
            end_date=AS_OF_TRADE_DATE,
        )
        oos_observations, oos_evaluation_audit = _evaluate(
            bar_database,
            full_feature_db,
            security_database,
            dates,
            full_candidates,
            market,
            industry_daily,
            industry_by_symbol,
            industry_members,
            listing_dates,
            delisting_dates,
            start_date="20250101",
            end_date=AS_OF_TRADE_DATE,
            variants=nominated_variants,
        )
        for variant in nominated_variants:
            nomination = nominations[variant]
            assert nomination is not None
            development = variants[variant]["horizons"][str(nomination)][
                "development"
            ]
            oos = _gate(
                [
                    row
                    for row in oos_observations
                    if row.variant == variant and row.horizon == nomination
                ],
                date_index,
            )
            finalized = _finalize_oos(
                development,
                oos,
                nominated=True,
            )
            variants[variant]["horizons"][str(nomination)][
                "rolling_oos"
            ] = finalized
            variants[variant]["rolling_oos_final_decision"] = finalized[
                "gate_decision"
            ]
    raw_signature = hashlib.sha256(
        json.dumps(
            {
                "raw_manifest": _sha256(
                    project
                    / "data"
                    / "processed"
                    / "yinhe_daily_manifest.json"
                ),
                "aliases": alias_hash,
                "end": development_cutoff,
            },
            sort_keys=True,
        ).encode()
    ).hexdigest()
    raw_signals, raw_audit = _raw_signal_sets(
        project,
        aliases,
        [day for day in dates if day <= development_cutoff],
        end_date=development_cutoff,
        signature=raw_signature,
    )
    sensitivity = _sensitivity(
        _feature_signal_sets(training_feature_db),
        raw_signals,
        security_database,
    )
    raw_count = training_scan_audit["variant_signals"]["exact_raw_buy"]
    filtered_count = training_scan_audit["variant_signals"]["exact_text_prompt"]
    filter_cost = {
        "exact_raw_buy_signals": raw_count,
        "exact_text_prompt_signals": filtered_count,
        "signals_suppressed": raw_count - filtered_count,
        "retention_rate": filtered_count / raw_count if raw_count else None,
        "development_performance_delta": _development_increment(
            variants,
            "exact_raw_buy",
            "exact_text_prompt",
        ),
    }
    above_count = training_scan_audit["variant_signals"][
        "text_prompt_above_ema245"
    ]
    trend_count = training_scan_audit["variant_signals"][
        "text_prompt_trend_aligned"
    ]
    parameters = {
        "as_of_trade_date": AS_OF_TRADE_DATE,
        "development": ["20230101", DEVELOPMENT_END],
        "rolling_oos": ["20250101", AS_OF_TRADE_DATE],
        "purge_trading_days": 60,
        "effective_development_signal_end": development_cutoff,
        "horizons": HORIZONS,
        "variants": VARIANTS,
        "cohort_design": "all_signal_symbol_date_variant_deduplicated",
        "entry": "next_trading_day_open",
        "minimum_listing_calendar_days": MINIMUM_LISTING_DAYS,
        "minimum_average_turnover_previous_20d": (
            MINIMUM_AVERAGE_TURNOVER20
        ),
        "matched_control": {
            "same_date": True,
            "same_pit_industry": True,
            "distance_fields": [
                "pre20_return",
                "drawdown20",
                "volatility20",
                "median_turnover20",
            ],
            "maximum_controls": MATCH_COUNT,
            "exclude_any_a8_signal": True,
            "missing_fallback": None,
        },
        "cost90": "data_unavailable_no_approximation",
        "red_var_chain": "separate_diagnostic_only",
    }
    parameters_hash = hashlib.sha256(
        json.dumps(parameters, sort_keys=True).encode()
    ).hexdigest()
    status = (
        "completed_first_pass"
        if nominated_variants
        else "stopped_after_training_no_nomination"
    )
    summary = {
        "schema_version": 1,
        "status": status,
        "generated_at": datetime.now(UTC).isoformat(),
        "as_of_trade_date": AS_OF_TRADE_DATE,
        "effective_development_signal_end": development_cutoff,
        "2026_holdout_opened": False,
        "research_only": True,
        "protocol": protocol,
        "model": {
            "id": "tdx_a8_reversal_v0_1",
            "parameters": parameters,
            "parameters_sha256": parameters_hash,
            "implementation_sha256": _sha256(Path(__file__)),
            "signal_implementation_sha256": SIGNAL_IMPLEMENTATION_SHA256,
        },
        "data": {
            "security_aliases_sha256": alias_hash,
            "trade_calendar_sha256": _sha256(calendar_path),
            "security_state_hashes": security_hashes,
            "benchmark_hashes": benchmark_hashes,
            "bar_cache": bar_cache,
            "training_feature_audit": training_feature_audit,
            "training_scan_audit": training_scan_audit,
            "training_evaluation_audit": training_evaluation_audit,
            "oos_feature_audit": full_feature_audit,
            "oos_scan_audit": oos_scan_audit,
            "oos_evaluation_audit": oos_evaluation_audit,
            "raw_sensitivity_audit": raw_audit,
            "2026_rows_evaluated": 0,
        },
        "variants": variants,
        "ablation": {
            "filter_opportunity_cost": filter_cost,
            "above_ema245_increment": {
                "base_filtered_signals": filtered_count,
                "retained_signals": above_count,
                "retention_rate": (
                    above_count / filtered_count if filtered_count else None
                ),
                "is_original_formula": False,
                "development_performance_delta": _development_increment(
                    variants,
                    "exact_text_prompt",
                    "text_prompt_above_ema245",
                ),
            },
            "trend_alignment_increment": {
                "above_ema245_signals": above_count,
                "retained_signals": trend_count,
                "retention_rate": (
                    trend_count / above_count if above_count else None
                ),
                "is_original_formula": False,
                "development_performance_delta": _development_increment(
                    variants,
                    "text_prompt_above_ema245",
                    "text_prompt_trend_aligned",
                ),
            },
            "raw_qfq_sensitivity": sensitivity,
            "red_hold_axis": _red_hold_diagnostic(
                training_candidates,
                training_feature_db,
                dates,
            ),
            "cost90": {
                "status": "data_unavailable",
                "approximated": False,
            },
        },
        "caveats": [
            "2026 final holdout was not opened, scanned, or evaluated.",
            "The final 60 official trading days of 2024 are purged from training.",
            "2025 is evaluated only for a horizon nominated by 2023-2024 training.",
            "If all training variants fail, 2025 evaluation stops without winner selection.",
            "The original A8 buy, FILTER5, EMA245, and MA20>MA30 variants are isolated.",
            "EMA245, MA20>MA30, COST90, KDJ, and VAR chains do not enter the original buy.",
            "COST90 remains data_unavailable and is never approximated.",
            "VAR1-VARC is reported only as a holding/exit diagnostic.",
            "Primary signals use Galaxy qfq; raw prices are used only for corporate-action sensitivity.",
            "Missing bars reset contiguous signal history and are counted.",
            "Matched controls use same-date PIT industry and pretrend, drawdown, volatility, and liquidity.",
            "Missing official benchmarks, PIT industries, or matched controls are never zero-filled.",
            "No fundamentals, announcements, news, stop loss, selective regime model, or parameter search is used.",
        ],
    }
    output = project / "reports" / "tdx_a8_reversal_validation"
    output.mkdir(parents=True, exist_ok=True)
    json_path = output / "first_pass_summary.json"
    markdown_path = output / "report.md"
    json_path.write_text(
        json.dumps(summary, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    markdown_path.write_text(_report_markdown(summary), encoding="utf-8")
    return {
        "status": status,
        "as_of_trade_date": AS_OF_TRADE_DATE,
        "effective_development_signal_end": development_cutoff,
        "2026_holdout_opened": False,
        "protocol_sha256": protocol["sha256"],
        "parameters_sha256": parameters_hash,
        "implementation_sha256": summary["model"]["implementation_sha256"],
        "signal_implementation_sha256": SIGNAL_IMPLEMENTATION_SHA256,
        "training_observations": len(training_observations),
        "gate_decisions": {
            variant: {
                "training_nomination": result["training_nomination"]["horizon"],
                "oos": result["rolling_oos_final_decision"],
            }
            for variant, result in variants.items()
        },
        "report_json": str(json_path),
        "report_markdown": str(markdown_path),
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="通达信 A8 短反转首轮冻结验证")
    parser.add_argument("--root", default=".")
    parser.add_argument(
        "--as-of-trade-date",
        default=AS_OF_TRADE_DATE,
    )
    args = parser.parse_args()
    if _date(args.as_of_trade_date) != AS_OF_TRADE_DATE:
        raise SystemExit(
            "A8 首轮验证冻结为 as_of_trade_date=20251231；禁止打开 2026"
        )
    print(
        json.dumps(
            run_validation(Path(args.root)),
            ensure_ascii=False,
            indent=2,
        )
    )


if __name__ == "__main__":
    main()

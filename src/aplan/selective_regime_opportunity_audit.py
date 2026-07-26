from __future__ import annotations

import argparse
import csv
import hashlib
import json
import math
import sqlite3
import tomllib
from collections import defaultdict, deque
from dataclasses import dataclass
from datetime import UTC, date, datetime
from pathlib import Path
from statistics import mean, median, pstdev
from typing import Any, Iterable

from .announcement_event_validation import (
    MARKET_CODE,
    _benchmark_return,
    _canonical,
    _canonicalizer,
    _industry_at,
    _load_security_state,
    _manifest,
    _max_drawdown,
    _number,
    _quantile,
    _return,
    _sha256,
    _truthy,
)
from .daily_candidate_historical_validation import (
    _factor_snapshot,
    _score_parts,
)
from .tdx_a8_reversal import analyze_a8_prompt
from .trend_monitor_validation import _listing_dates, _technical_snapshot


PROTOCOL_SHA256 = (
    "a3e6b5afe688a65a5cb5b1d89f9c79d271695b99937b176eb15892d4e2709d3c"
)
READ_START = "20230101"
MAXIMUM_SIGNAL_DATE = "20241008"
DEVELOPMENT_DATA_END = "20241231"
FORBIDDEN_READ_START = "20250101"
HORIZONS = (5, 20)
EXPERT_ROUTES = ("residual_continuation", "residual_recovery")
LEGACY_ROUTES = (
    "price_AND_trend",
    "price_AND_a8",
    "trend_AND_a8",
    "price_AND_trend_AND_a8",
)
ROUTES = (*EXPERT_ROUTES, *LEGACY_ROUTES)
FOLDS = (
    ("20230403", "20230831"),
    ("20230901", "20231229"),
    ("20240102", "20240531"),
    ("20240603", "20241008"),
)
MINIMUM_LISTING_DAYS = 120
MINIMUM_AVERAGE_TURNOVER20 = 50_000_000.0
MATCH_COUNT = 5
TAIL_LOSS_THRESHOLD = -0.10


@dataclass(frozen=True, slots=True)
class Outcome:
    route: str
    horizon: int
    signal_date: str
    symbol: str
    industry_code: str
    score: float | None
    percentile: float | None
    selected: bool
    gross_return: float
    net_return: float
    market_excess: float | None
    industry_excess: float | None
    matched_excess: float | None


def _digits(value: Any) -> str:
    digits = "".join(character for character in str(value or "") if character.isdigit())
    return digits[:8] if len(digits) >= 8 else ""


def _protocol(project: Path) -> dict[str, Any]:
    path = project / "config" / "selective_regime_opportunity_audit.toml"
    lock_path = project / "config" / "selective_regime_opportunity_audit.lock.json"
    if not path.exists() or not lock_path.exists():
        raise ValueError("缺少 selective regime phase0 冻结协议或锁文件")
    digest = _sha256(path)
    lock = json.loads(lock_path.read_text(encoding="utf-8"))
    document = tomllib.loads(path.read_text(encoding="utf-8"))
    if digest != PROTOCOL_SHA256 or lock.get("sha256") != PROTOCOL_SHA256:
        raise ValueError("selective regime phase0 冻结协议哈希不匹配")
    if document.get("final_holdout_opened") is not False:
        raise ValueError("phase0 必须保持 final_holdout_opened=false")
    design = document.get("time_design", {})
    if (
        _digits(design.get("maximum_signal_date")) != MAXIMUM_SIGNAL_DATE
        or _digits(design.get("development_data_end")) != DEVELOPMENT_DATA_END
        or _digits(design.get("forbidden_read_start")) != FORBIDDEN_READ_START
    ):
        raise ValueError("phase0 时间边界已变化")
    budget = document.get("complexity_budget", {})
    forbidden = (
        "feature_period_search_allowed",
        "market_state_threshold_search_allowed",
        "expert_weight_search_allowed",
        "interaction_search_allowed",
        "nonlinear_model_allowed",
        "selector_allowed",
    )
    if any(budget.get(key) is not False for key in forbidden):
        raise ValueError("phase0 复杂度预算允许了冻结协议禁止的搜索")
    if tuple(document.get("legacy_interaction_audit", {}).get("routes", ())) != (
        LEGACY_ROUTES
    ):
        raise ValueError("phase0 legacy 固定交集已变化")
    return {
        "path": str(path),
        "sha256": digest,
        "lock_path": str(lock_path),
        "lock_sha256": _sha256(lock_path),
        "document": document,
    }


def _load_calendar(path: Path) -> list[str]:
    dates: list[str] = []
    with path.open(encoding="utf-8-sig", newline="") as handle:
        for row in csv.DictReader(handle):
            day = _digits(
                row.get("trade_date") or row.get("cal_date") or row.get("date")
            )
            if not day or day < READ_START:
                continue
            if day >= FORBIDDEN_READ_START:
                break
            if day <= DEVELOPMENT_DATA_END:
                dates.append(day)
    return sorted(set(dates))


def _load_benchmarks(
    project: Path,
    aliases: dict[str, str],
) -> tuple[
    dict[tuple[str, str], tuple[float, float]],
    dict[tuple[str, str], tuple[float, float]],
    dict[str, list[tuple[str, str, str]]],
    dict[str, list[str]],
    dict[str, Any],
]:
    root = project / "data" / "processed" / "benchmarks"
    manifest = _manifest(root / "manifest.json")
    if not manifest.get("point_in_time_constituents"):
        raise ValueError("官方基准不具备 PIT 行业成分")
    market: dict[tuple[str, str], tuple[float, float]] = {}
    with (root / "market_indices.csv").open(
        encoding="utf-8-sig", newline=""
    ) as handle:
        for row in csv.DictReader(handle):
            day = _digits(row.get("trade_date"))
            if day >= FORBIDDEN_READ_START:
                continue
            if (
                READ_START <= day <= DEVELOPMENT_DATA_END
                and row.get("index_code") == MARKET_CODE
            ):
                open_price = _number(row.get("open"))
                close = _number(row.get("close"))
                if open_price and close:
                    market[(MARKET_CODE, day)] = (open_price, close)
    industry_daily: dict[tuple[str, str], tuple[float, float]] = {}
    with (root / "industry_daily.csv").open(
        encoding="utf-8-sig", newline=""
    ) as handle:
        for row in csv.DictReader(handle):
            day = _digits(row.get("trade_date"))
            if day >= FORBIDDEN_READ_START:
                continue
            if READ_START <= day <= DEVELOPMENT_DATA_END:
                open_price = _number(row.get("open"))
                close = _number(row.get("close"))
                code = str(row.get("index_code") or "")
                if code and open_price and close:
                    industry_daily[(code, day)] = (open_price, close)
    by_symbol: dict[str, list[tuple[str, str, str]]] = defaultdict(list)
    members: dict[str, list[str]] = defaultdict(list)
    with (root / "industry_constituents.csv").open(
        encoding="utf-8-sig", newline=""
    ) as handle:
        for row in csv.DictReader(handle):
            symbol = _canonical(str(row.get("symbol") or ""), aliases)
            code = str(row.get("index_code") or "")
            in_date = _digits(row.get("in_date"))
            out_date = _digits(row.get("out_date")) or "99991231"
            if (
                len(symbol) == 6
                and code
                and in_date
                and in_date <= MAXIMUM_SIGNAL_DATE
            ):
                by_symbol[symbol].append((code, in_date, out_date))
                members[code].append(symbol)
    hashes = {
        "manifest_sha256": _sha256(root / "manifest.json"),
        "market_indices_sha256": _sha256(root / "market_indices.csv"),
        "industry_daily_sha256": _sha256(root / "industry_daily.csv"),
        "industry_constituents_sha256": _sha256(
            root / "industry_constituents.csv"
        ),
    }
    return (
        market,
        industry_daily,
        by_symbol,
        {key: sorted(set(value)) for key, value in members.items()},
        hashes,
    )


def _security_dates(
    path: Path,
    aliases: dict[str, str],
) -> tuple[dict[str, str], dict[str, str]]:
    listed: dict[str, str] = {}
    delisted: dict[str, str] = {}
    with path.open(encoding="utf-8-sig", newline="") as handle:
        for row in csv.DictReader(handle):
            symbol = _canonical(str(row.get("symbol") or ""), aliases)
            list_date = _digits(row.get("list_date"))
            delist_date = _digits(row.get("delist_date"))
            if len(symbol) != 6:
                continue
            if list_date:
                previous = listed.get(symbol)
                listed[symbol] = min(previous, list_date) if previous else list_date
            if delist_date:
                delisted[symbol] = delist_date
    return listed, delisted


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


def _ranks(
    values: dict[str, float],
    *,
    higher_is_better: bool = True,
) -> dict[str, float]:
    if not values:
        return {}
    ordered = sorted(
        values.items(),
        key=lambda item: (item[1], item[0]),
        reverse=not higher_is_better,
    )
    output: dict[str, float] = {}
    index = 0
    while index < len(ordered):
        stop = index + 1
        while stop < len(ordered) and ordered[stop][1] == ordered[index][1]:
            stop += 1
        average_position = (index + stop - 1) / 2
        percentile = (
            average_position / (len(ordered) - 1)
            if len(ordered) > 1
            else 0.5
        )
        for position in range(index, stop):
            output[ordered[position][0]] = percentile
        index = stop
    return output


def _pearson(left: list[float], right: list[float]) -> float | None:
    if len(left) != len(right) or len(left) < 3:
        return None
    left_mean, right_mean = mean(left), mean(right)
    numerator = sum(
        (a - left_mean) * (b - right_mean)
        for a, b in zip(left, right, strict=True)
    )
    left_scale = math.sqrt(sum((value - left_mean) ** 2 for value in left))
    right_scale = math.sqrt(sum((value - right_mean) ** 2 for value in right))
    return (
        numerator / (left_scale * right_scale)
        if left_scale and right_scale
        else None
    )


def _spearman(values: list[tuple[float, float]]) -> float | None:
    if len(values) < 10:
        return None
    left = {str(index): item[0] for index, item in enumerate(values)}
    right = {str(index): item[1] for index, item in enumerate(values)}
    left_rank = _ranks(left)
    right_rank = _ranks(right)
    return _pearson(
        [left_rank[str(index)] for index in range(len(values))],
        [right_rank[str(index)] for index in range(len(values))],
    )


def _a8_signals(
    project: Path,
    aliases: dict[str, str],
    dates: list[str],
    *,
    source_directory: str,
) -> tuple[set[tuple[str, str]], dict[str, Any]]:
    state = project / "state" / "selective_regime_opportunity_audit"
    state.mkdir(parents=True, exist_ok=True)
    label = "qfq" if source_directory.endswith("_qfq") else "raw"
    database = state / f"{label}_a8_closes_20230101_20241008.sqlite3"
    source_paths = [
        project / "data" / "processed" / source_directory / f"{day}.csv"
        for day in dates
        if day <= MAXIMUM_SIGNAL_DATE
    ]
    signature = hashlib.sha256(
        json.dumps(
            {
                "files": [
                    (path.name, path.stat().st_size, path.stat().st_mtime_ns)
                    for path in source_paths
                    if path.exists()
                ],
                "signal_sha256": _sha256(
                    project / "src" / "aplan" / "tdx_a8_reversal.py"
                ),
            },
            sort_keys=True,
        ).encode()
    ).hexdigest()
    connection = sqlite3.connect(database)
    connection.execute(
        "CREATE TABLE IF NOT EXISTS metadata "
        "(key TEXT PRIMARY KEY,value TEXT NOT NULL)"
    )
    previous = connection.execute(
        "SELECT value FROM metadata WHERE key='source_signature'"
    ).fetchone()
    table = connection.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='prices'"
    ).fetchone()
    if not (previous and previous[0] == signature and table):
        connection.executescript(
            """
            DROP TABLE IF EXISTS prices;
            DELETE FROM metadata;
            CREATE TABLE prices (
                symbol TEXT NOT NULL,
                trade_date TEXT NOT NULL,
                close REAL NOT NULL,
                PRIMARY KEY(symbol,trade_date)
            );
            """
        )
        for path in source_paths:
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
                        incoming.append((symbol, path.stem, close))
            connection.executemany(
                "INSERT OR REPLACE INTO prices VALUES (?,?,?)",
                incoming,
            )
        connection.execute(
            "INSERT INTO metadata VALUES ('source_signature',?)",
            (signature,),
        )
        connection.commit()
    date_index = {
        day: index for index, day in enumerate(day for day in dates if day <= MAXIMUM_SIGNAL_DATE)
    }
    signals: set[tuple[str, str]] = set()
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
            if snapshot.filtered_buy:
                signals.add((day, current_symbol))

    for symbol, day, close in connection.execute(
        "SELECT symbol,trade_date,close FROM prices ORDER BY symbol,trade_date"
    ):
        index = date_index[str(day)]
        if symbol != current_symbol:
            flush()
            current_symbol = str(symbol)
            run_dates, closes, last_index = [], [], None
        if last_index is not None and index != last_index + 1:
            flush()
            run_dates, closes = [], []
            resets += 1
        run_dates.append(str(day))
        closes.append(float(close))
        last_index = index
    flush()
    rows = connection.execute("SELECT COUNT(*) FROM prices").fetchone()[0]
    connection.close()
    return signals, {
        "source": label,
        "rows": rows,
        "signals": len(signals),
        "contiguous_history_resets": resets,
        "source_signature": signature,
    }


def _industry_metrics(
    industry_daily: dict[tuple[str, str], tuple[float, float]],
    code: str,
    dates: list[str],
    index: int,
) -> dict[str, float] | None:
    required = (3, 20, 60)
    if index < max(required):
        return None
    closes: list[float] = []
    for offset in range(60, -1, -1):
        value = industry_daily.get((code, dates[index - offset]))
        if value is None:
            return None
        closes.append(float(value[1]))
    current = closes[-1]
    return {
        "return3": current / closes[-4] - 1,
        "return20": current / closes[-21] - 1,
        "return60": current / closes[0] - 1,
        "drawdown20": current / max(closes[-21:-1]) - 1,
    }


def _stock_features(
    history: Iterable[tuple[float, float, float, float]],
    close: float,
    turnover: float,
) -> dict[str, float | bool] | None:
    previous = list(history)
    if len(previous) < 60:
        return None
    prior_closes = [item[0] for item in previous]
    prior_turnovers = [item[3] for item in previous]
    closes20 = [*prior_closes[-20:], close]
    returns20 = [
        math.log(closes20[index] / closes20[index - 1])
        for index in range(1, len(closes20))
        if closes20[index - 1] > 0
    ]
    turnover_days = [*prior_turnovers[-19:], turnover]
    negative_last5 = [
        turnover_days[index]
        for index in range(15, 20)
        if returns20[index] < 0
    ]
    negative_prior = [
        turnover_days[index]
        for index in range(0, 15)
        if returns20[index] < 0
    ]
    exhaustion = None
    if len(negative_last5) >= 2 and len(negative_prior) >= 2:
        baseline = mean(negative_prior)
        exhaustion = mean(negative_last5) / baseline if baseline > 0 else None
    return20 = close / prior_closes[-20] - 1
    path = sum(abs(value) for value in returns20)
    efficiency = (
        abs(math.log(close / prior_closes[-20])) / path if path > 0 else 0.0
    )
    return {
        "return3": close / prior_closes[-3] - 1,
        "return20": return20,
        "return60": close / prior_closes[-60] - 1,
        "drawdown20": close / max(prior_closes[-20:]) - 1,
        "trend_efficiency20": efficiency,
        "realized_volatility20": pstdev(returns20),
        "median_turnover20": median(prior_turnovers[-20:]),
        "turnover_exhaustion": exhaustion,
        "above_ma20": close >= mean([*prior_closes[-19:], close]),
    }


def _market_state(
    market: dict[tuple[str, str], tuple[float, float]],
    dates: list[str],
    index: int,
    breadth: dict[str, float],
    previous_states: dict[str, str],
) -> str:
    day = dates[index]
    if index < 140:
        return "other"
    closes = [
        market.get((MARKET_CODE, dates[position]))
        for position in range(index - 140, index + 1)
    ]
    if any(value is None for value in closes):
        return "other"
    values = [float(value[1]) for value in closes if value is not None]
    current = values[-1]
    ma60 = mean(values[-60:])
    return5 = current / values[-6] - 1
    return20 = current / values[-21] - 1
    log_returns = [
        math.log(values[position] / values[position - 1])
        for position in range(1, len(values))
    ]
    vol20 = pstdev(log_returns[-20:])
    trailing_volatility = [
        pstdev(log_returns[position - 20 : position])
        for position in range(20, len(log_returns) + 1)
    ]
    reference = median(trailing_volatility[-120:])
    breadth_now = breadth.get(day)
    breadth5 = breadth.get(dates[index - 5])
    if breadth_now is None or breadth5 is None:
        return "other"
    prior_stress = any(
        previous_states.get(dates[position]) == "stress"
        for position in range(max(0, index - 10), index)
    )
    if prior_stress and return5 > 0 and breadth_now - breadth5 >= 0.10:
        return "recovery"
    if (
        current < ma60
        and return20 < 0
        and breadth_now <= 0.40
        and vol20 >= reference
    ):
        return "stress"
    if current > ma60 and return20 > 0 and breadth_now >= 0.55:
        return "trend_expansion"
    return "other"


def _schema(connection: sqlite3.Connection) -> None:
    connection.executescript(
        """
        DROP TABLE IF EXISTS metadata;
        DROP TABLE IF EXISTS bars;
        DROP TABLE IF EXISTS features;
        DROP TABLE IF EXISTS states;
        CREATE TABLE metadata (key TEXT PRIMARY KEY,value TEXT NOT NULL);
        CREATE TABLE bars (
            trade_date TEXT NOT NULL,
            symbol TEXT NOT NULL,
            open REAL NOT NULL,
            high REAL NOT NULL,
            low REAL NOT NULL,
            close REAL NOT NULL,
            turnover REAL NOT NULL,
            is_suspended INTEGER NOT NULL,
            is_limit_up INTEGER NOT NULL,
            PRIMARY KEY(trade_date,symbol)
        );
        CREATE INDEX idx_phase0_bars_symbol_date ON bars(symbol,trade_date);
        CREATE TABLE features (
            trade_date TEXT NOT NULL,
            symbol TEXT NOT NULL,
            industry_code TEXT NOT NULL,
            residual_return20 REAL,
            residual_return60 REAL,
            trend_efficiency20 REAL,
            residual_drawdown20 REAL,
            residual_return3 REAL,
            turnover_exhaustion REAL,
            realized_volatility20 REAL,
            median_turnover20 REAL,
            continuation_score REAL,
            continuation_percentile REAL,
            recovery_score REAL,
            recovery_percentile REAL,
            price_signal INTEGER NOT NULL,
            trend_signal INTEGER NOT NULL,
            a8_signal INTEGER NOT NULL,
            residual_continuation INTEGER NOT NULL,
            residual_recovery INTEGER NOT NULL,
            price_AND_trend INTEGER NOT NULL,
            price_AND_a8 INTEGER NOT NULL,
            trend_AND_a8 INTEGER NOT NULL,
            price_AND_trend_AND_a8 INTEGER NOT NULL,
            PRIMARY KEY(trade_date,symbol)
        );
        CREATE INDEX idx_phase0_features_date ON features(trade_date);
        CREATE TABLE states (
            trade_date TEXT PRIMARY KEY,
            market_state TEXT NOT NULL,
            breadth REAL,
            eligible_symbols INTEGER NOT NULL
        );
        """
    )


def _build_features(
    project: Path,
    aliases: dict[str, str],
    dates: list[str],
    security_database: Path,
    listing_dates: dict[str, str],
    delisting_dates: dict[str, str],
    industry_daily: dict[tuple[str, str], tuple[float, float]],
    industry_by_symbol: dict[str, list[tuple[str, str, str]]],
    market: dict[tuple[str, str], tuple[float, float]],
    a8_signals: set[tuple[str, str]],
    *,
    source_directory: str,
    fixed_states: dict[str, str] | None = None,
) -> tuple[Path, dict[str, Any], dict[str, str]]:
    state_root = project / "state" / "selective_regime_opportunity_audit"
    state_root.mkdir(parents=True, exist_ok=True)
    label = "qfq" if source_directory.endswith("_qfq") else "raw"
    database = state_root / f"{label}_features_20230101_20241231.sqlite3"
    paths = [
        project / "data" / "processed" / source_directory / f"{day}.csv"
        for day in dates
    ]
    signature = hashlib.sha256(
        json.dumps(
            {
                "files": [
                    (path.name, path.stat().st_size, path.stat().st_mtime_ns)
                    for path in paths
                    if path.exists()
                ],
                "protocol": PROTOCOL_SHA256,
                "a8_signals": len(a8_signals),
                "fixed_states": fixed_states,
            },
            sort_keys=True,
        ).encode()
    ).hexdigest()
    connection = sqlite3.connect(database)
    existing = connection.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='metadata'"
    ).fetchone()
    if existing:
        prior = connection.execute(
            "SELECT value FROM metadata WHERE key='source_signature'"
        ).fetchone()
        count = connection.execute("SELECT COUNT(*) FROM features").fetchone()[0]
        if prior and prior[0] == signature and count:
            states = {
                row[0]: row[1]
                for row in connection.execute(
                    "SELECT trade_date,market_state FROM states"
                )
            }
            metadata = {
                key: json.loads(value)
                for key, value in connection.execute(
                    "SELECT key,value FROM metadata WHERE key!='source_signature'"
                )
            }
            connection.close()
            return database, {
                "status": "reused",
                "source": label,
                "feature_rows": count,
                "source_signature": signature,
                **metadata,
            }, states
    _schema(connection)
    connection.execute("PRAGMA journal_mode=WAL")
    connection.execute("PRAGMA synchronous=NORMAL")
    statuses = sqlite3.connect(security_database)
    statuses.row_factory = sqlite3.Row
    histories: dict[str, deque[tuple[float, float, float, float]]] = defaultdict(
        lambda: deque(maxlen=61)
    )
    breadth: dict[str, float] = {}
    states: dict[str, str] = {}
    audit: dict[str, int] = defaultdict(int)
    route_sets: dict[str, set[tuple[str, str]]] = {
        route: set() for route in ROUTES
    }
    try:
        for date_index, (day, path) in enumerate(zip(dates, paths, strict=True)):
            if not path.exists():
                audit["missing_daily_files"] += 1
                continue
            raw_rows: list[dict[str, Any]] = []
            with path.open(encoding="utf-8-sig", newline="") as handle:
                for row in csv.DictReader(handle):
                    symbol = _canonical(
                        str(row.get("symbol") or "").strip(),
                        aliases,
                    )
                    values = {
                        key: _number(row.get(key))
                        for key in (
                            "open",
                            "high",
                            "low",
                            "close",
                            "volume",
                            "turnover",
                        )
                    }
                    if (
                        len(symbol) != 6
                        or any(value is None for value in values.values())
                        or min(
                            float(values["open"]),
                            float(values["high"]),
                            float(values["low"]),
                            float(values["close"]),
                        )
                        <= 0
                    ):
                        audit["invalid_bar_rows"] += 1
                        continue
                    raw_rows.append(
                        {
                            "symbol": symbol,
                            **{key: float(value) for key, value in values.items()},
                            "is_suspended": _truthy(row.get("is_suspended")),
                            "is_limit_up": _truthy(row.get("is_limit_up")),
                            "is_limit_down": _truthy(row.get("is_limit_down")),
                        }
                    )
            connection.executemany(
                "INSERT OR REPLACE INTO bars VALUES (?,?,?,?,?,?,?,?,?)",
                [
                    (
                        day,
                        row["symbol"],
                        row["open"],
                        row["high"],
                        row["low"],
                        row["close"],
                        row["turnover"],
                        row["is_suspended"],
                        row["is_limit_up"],
                    )
                    for row in raw_rows
                ],
            )
            if day > MAXIMUM_SIGNAL_DATE:
                for row in raw_rows:
                    histories[row["symbol"]].append(
                        (
                            row["close"],
                            row["high"],
                            row["volume"],
                            row["turnover"],
                        )
                    )
                continue
            snapshots: dict[str, tuple[float, float, float, float, bool]] = {}
            technical: dict[str, bool] = {}
            stock_features: dict[str, dict[str, float | bool]] = {}
            for row in raw_rows:
                symbol = row["symbol"]
                history = histories[symbol]
                factor = _factor_snapshot(
                    [(item[0], item[3]) for item in history],
                    row["close"],
                    row["turnover"],
                )
                if factor is not None:
                    snapshots[symbol] = factor
                technical[symbol] = bool(
                    not (
                        row["is_suspended"]
                        or row["is_limit_up"]
                        or row["is_limit_down"]
                    )
                    and _technical_snapshot(
                        symbol,
                        day,
                        0,
                        history,
                        open_price=row["open"],
                        close=row["close"],
                        low=row["low"],
                        volume=row["volume"],
                    )
                    is not None
                )
                features = _stock_features(history, row["close"], row["turnover"])
                if features is not None:
                    stock_features[symbol] = features
            score_parts = _score_parts(snapshots)
            states_for_day = {
                str(row["symbol"]): (
                    int(row["is_st"]),
                    int(row["is_suspended"]),
                )
                for row in statuses.execute(
                    "SELECT symbol,is_st,is_suspended FROM daily_status "
                    "WHERE trade_date=?",
                    (day,),
                )
            }
            eligible: dict[str, dict[str, Any]] = {}
            for row in raw_rows:
                symbol = row["symbol"]
                audit["rows_scanned"] += 1
                features = stock_features.get(symbol)
                parts = score_parts.get(symbol)
                if features is None or parts is None:
                    audit["missing_base_features"] += 1
                    continue
                state = states_for_day.get(symbol)
                if state is None:
                    audit["missing_security_state"] += 1
                    continue
                audit["security_state_joined"] += 1
                if state[0] or state[1] or row["is_suspended"]:
                    audit["excluded_st_or_suspended"] += 1
                    continue
                listed = listing_dates.get(symbol)
                if (
                    not listed
                    or _days_listed(listed, day) < MINIMUM_LISTING_DAYS
                ):
                    audit["excluded_listing_age"] += 1
                    continue
                if delisting_dates.get(symbol) and day >= delisting_dates[symbol]:
                    audit["excluded_delisted"] += 1
                    continue
                if (
                    float(features["median_turnover20"])
                    < MINIMUM_AVERAGE_TURNOVER20
                ):
                    audit["below_liquidity"] += 1
                    continue
                industry = _industry_at(symbol, day, industry_by_symbol)
                if not industry:
                    audit["missing_industry"] += 1
                    continue
                audit["industry_joined"] += 1
                industry_values = _industry_metrics(
                    industry_daily,
                    industry,
                    dates,
                    date_index,
                )
                if industry_values is None:
                    audit["missing_industry_features"] += 1
                    continue
                eligible[symbol] = {
                    "row": row,
                    "industry": industry,
                    "price_score": sum(parts[:3]) + 15.0,
                    "trend_signal": technical.get(symbol, False),
                    "a8_signal": (day, symbol) in a8_signals,
                    "residual_return20": (
                        float(features["return20"])
                        - industry_values["return20"]
                    ),
                    "residual_return60": (
                        float(features["return60"])
                        - industry_values["return60"]
                    ),
                    "trend_efficiency20": (
                        float(features["trend_efficiency20"])
                        * (
                            1
                            if float(features["return20"])
                            - industry_values["return20"]
                            >= 0
                            else -1
                        )
                    ),
                    "residual_drawdown20": (
                        float(features["drawdown20"])
                        - industry_values["drawdown20"]
                    ),
                    "residual_return3": (
                        float(features["return3"])
                        - industry_values["return3"]
                    ),
                    "turnover_exhaustion": features["turnover_exhaustion"],
                    "realized_volatility20": features[
                        "realized_volatility20"
                    ],
                    "median_turnover20": features["median_turnover20"],
                    "above_ma20": features["above_ma20"],
                }
            breadth[day] = (
                sum(bool(item["above_ma20"]) for item in eligible)
                / len(eligible)
                if eligible
                else 0.0
            )
            market_state = (
                fixed_states.get(day, "other")
                if fixed_states is not None
                else _market_state(market, dates, date_index, breadth, states)
            )
            states[day] = market_state
            price_symbols = {
                symbol
                for symbol, _ in sorted(
                    (
                        (symbol, float(item["price_score"]))
                        for symbol, item in eligible.items()
                    ),
                    key=lambda item: (-item[1], item[0]),
                )[:5]
            }
            by_industry: dict[str, list[str]] = defaultdict(list)
            for symbol, item in eligible.items():
                by_industry[str(item["industry"])].append(symbol)
            for members in by_industry.values():
                continuation_fields = (
                    "residual_return20",
                    "residual_return60",
                    "trend_efficiency20",
                )
                continuation_ranks = {
                    field: _ranks(
                        {
                            symbol: float(eligible[symbol][field])
                            for symbol in members
                        }
                    )
                    for field in continuation_fields
                }
                recovery_complete = [
                    symbol
                    for symbol in members
                    if eligible[symbol]["turnover_exhaustion"] is not None
                ]
                recovery_ranks = {
                    "residual_drawdown20": _ranks(
                        {
                            symbol: -float(
                                eligible[symbol]["residual_drawdown20"]
                            )
                            for symbol in recovery_complete
                        }
                    ),
                    "residual_return3": _ranks(
                        {
                            symbol: float(
                                eligible[symbol]["residual_return3"]
                            )
                            for symbol in recovery_complete
                        }
                    ),
                    "turnover_exhaustion": _ranks(
                        {
                            symbol: -float(
                                eligible[symbol]["turnover_exhaustion"]
                            )
                            for symbol in recovery_complete
                        }
                    ),
                }
                for symbol in members:
                    eligible[symbol]["continuation_score"] = mean(
                        continuation_ranks[field][symbol]
                        for field in continuation_fields
                    )
                for symbol in recovery_complete:
                    eligible[symbol]["recovery_score"] = mean(
                        recovery_ranks[field][symbol]
                        for field in recovery_ranks
                    )
            continuation_percentiles = _ranks(
                {
                    symbol: float(item["continuation_score"])
                    for symbol, item in eligible.items()
                    if "continuation_score" in item
                }
            )
            recovery_percentiles = _ranks(
                {
                    symbol: float(item["recovery_score"])
                    for symbol, item in eligible.items()
                    if "recovery_score" in item
                }
            )
            incoming = []
            for symbol, item in eligible.items():
                price_signal = symbol in price_symbols
                trend_signal = bool(item["trend_signal"])
                a8_signal = bool(item["a8_signal"])
                continuation = bool(
                    market_state == "trend_expansion"
                    and continuation_percentiles.get(symbol, 0.0) >= 0.90
                )
                recovery = bool(
                    market_state == "recovery"
                    and recovery_percentiles.get(symbol, 0.0) >= 0.90
                )
                route_flags = {
                    "residual_continuation": continuation,
                    "residual_recovery": recovery,
                    "price_AND_trend": price_signal and trend_signal,
                    "price_AND_a8": price_signal and a8_signal,
                    "trend_AND_a8": trend_signal and a8_signal,
                    "price_AND_trend_AND_a8": (
                        price_signal and trend_signal and a8_signal
                    ),
                }
                for route, enabled in route_flags.items():
                    if enabled:
                        route_sets[route].add((day, symbol))
                incoming.append(
                    (
                        day,
                        symbol,
                        item["industry"],
                        item["residual_return20"],
                        item["residual_return60"],
                        item["trend_efficiency20"],
                        item["residual_drawdown20"],
                        item["residual_return3"],
                        item["turnover_exhaustion"],
                        item["realized_volatility20"],
                        item["median_turnover20"],
                        item.get("continuation_score"),
                        continuation_percentiles.get(symbol),
                        item.get("recovery_score"),
                        recovery_percentiles.get(symbol),
                        int(price_signal),
                        int(trend_signal),
                        int(a8_signal),
                        *(int(route_flags[route]) for route in ROUTES),
                    )
                )
            connection.executemany(
                "INSERT OR REPLACE INTO features VALUES "
                "(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                incoming,
            )
            connection.execute(
                "INSERT OR REPLACE INTO states VALUES (?,?,?,?)",
                (day, market_state, breadth[day], len(eligible)),
            )
            audit["eligible_rows"] += len(eligible)
            if (date_index + 1) % 50 == 0:
                connection.commit()
                print(
                    f"phase0 {label} 特征：{date_index + 1}/{len(dates)}，"
                    f"eligible={audit['eligible_rows']}",
                    flush=True,
                )
            for row in raw_rows:
                histories[row["symbol"]].append(
                    (
                        row["close"],
                        row["high"],
                        row["volume"],
                        row["turnover"],
                    )
                )
    finally:
        statuses.close()
    audit["security_state_join_coverage"] = (
        audit["security_state_joined"]
        / (audit["security_state_joined"] + audit["missing_security_state"])
        if audit["security_state_joined"] + audit["missing_security_state"]
        else None
    )
    audit["industry_join_coverage"] = (
        audit["industry_joined"]
        / (audit["industry_joined"] + audit["missing_industry"])
        if audit["industry_joined"] + audit["missing_industry"]
        else None
    )
    audit["route_signals"] = {
        route: len(values) for route, values in route_sets.items()
    }
    state_counts = {
        state: sum(value == state for value in states.values())
        for state in ("trend_expansion", "recovery", "stress", "other")
    }
    metadata = {
        "audit": dict(audit),
        "state_counts": state_counts,
        "route_signals": audit["route_signals"],
    }
    connection.executemany(
        "INSERT INTO metadata VALUES (?,?)",
        [
            ("source_signature", signature),
            *[
                (key, json.dumps(value, sort_keys=True))
                for key, value in metadata.items()
            ],
        ],
    )
    connection.commit()
    feature_rows = connection.execute("SELECT COUNT(*) FROM features").fetchone()[0]
    bar_rows = connection.execute("SELECT COUNT(*) FROM bars").fetchone()[0]
    connection.close()
    return database, {
        "status": "built",
        "source": label,
        "feature_rows": feature_rows,
        "bar_rows": bar_rows,
        "source_signature": signature,
        **metadata,
    }, states


def _distance(source: sqlite3.Row, control: sqlite3.Row) -> float:
    return (
        abs(
            float(source["residual_return20"])
            - float(control["residual_return20"])
        )
        / 0.05
        + abs(
            float(source["residual_drawdown20"])
            - float(control["residual_drawdown20"])
        )
        / 0.05
        + abs(
            float(source["realized_volatility20"])
            - float(control["realized_volatility20"])
        )
        / 0.02
        + abs(
            math.log(
                float(source["median_turnover20"])
                / float(control["median_turnover20"])
            )
        )
    )


def _eligible_entry(
    bar: sqlite3.Row | None,
    state: tuple[int, int] | None,
) -> bool:
    return bool(
        bar
        and state
        and not state[0]
        and not state[1]
        and not bar["is_suspended"]
        and not (bar["is_limit_up"] and bar["high"] == bar["low"])
    )


def _evaluate_day(
    bars: sqlite3.Connection,
    statuses: sqlite3.Connection,
    day_rows: dict[str, sqlite3.Row],
    route_candidates: set[str],
    dates: list[str],
    date_index: dict[str, int],
    signal_date: str,
    horizon: int,
    market: dict[tuple[str, str], tuple[float, float]],
    industry_daily: dict[tuple[str, str], tuple[float, float]],
    *,
    score_field: str | None,
    percentile_field: str | None,
    route: str,
    evaluation_symbols: Iterable[str],
) -> tuple[list[Outcome], dict[str, int]]:
    index = date_index[signal_date]
    if index + horizon >= len(dates) or index + 1 >= len(dates):
        return [], {"missing_horizon": 1}
    entry_date = dates[index + 1]
    exit_date = dates[index + horizon]
    entry_states = {
        str(row["symbol"]): (int(row["is_st"]), int(row["is_suspended"]))
        for row in statuses.execute(
            "SELECT symbol,is_st,is_suspended FROM daily_status "
            "WHERE trade_date=?",
            (entry_date,),
        )
    }
    entry_bars = {
        str(row["symbol"]): row
        for row in bars.execute(
            "SELECT * FROM bars WHERE trade_date=?",
            (entry_date,),
        )
    }
    exit_closes = {
        str(row["symbol"]): float(row["close"])
        for row in bars.execute(
            "SELECT symbol,close FROM bars WHERE trade_date=?",
            (exit_date,),
        )
    }
    by_industry: dict[str, list[sqlite3.Row]] = defaultdict(list)
    for row in day_rows.values():
        if all(
            row[field] is not None
            for field in (
                "residual_return20",
                "residual_drawdown20",
                "realized_volatility20",
                "median_turnover20",
            )
        ):
            by_industry[str(row["industry_code"])].append(row)
    output: list[Outcome] = []
    counters: dict[str, int] = defaultdict(int)
    for symbol in evaluation_symbols:
        source = day_rows.get(symbol)
        if source is None:
            continue
        entry = entry_bars.get(symbol)
        if not _eligible_entry(entry, entry_states.get(symbol)):
            counters["untradeable"] += 1
            continue
        exit_close = exit_closes.get(symbol)
        if exit_close is None:
            counters["missing_exit"] += 1
            continue
        controls = [
            row
            for row in by_industry.get(str(source["industry_code"]), [])
            if row["symbol"] != symbol and row["symbol"] not in route_candidates
        ]
        selected_controls: list[str] = []
        for _, control_symbol in sorted(
            (_distance(source, row), str(row["symbol"])) for row in controls
        ):
            control_entry = entry_bars.get(control_symbol)
            if not _eligible_entry(
                control_entry,
                entry_states.get(control_symbol),
            ):
                continue
            selected_controls.append(control_symbol)
            if len(selected_controls) == MATCH_COUNT:
                break
        control_returns: list[float] = []
        for control_symbol in selected_controls:
            control_entry = entry_bars.get(control_symbol)
            control_exit = exit_closes.get(control_symbol)
            if control_entry and control_exit is not None:
                _, value = _return(
                    float(control_entry["open"]),
                    control_exit,
                )
                control_returns.append(value)
        gross, net = _return(float(entry["open"]), exit_close)
        matched = mean(control_returns) if control_returns else None
        market_return = _benchmark_return(
            market,
            MARKET_CODE,
            entry_date,
            exit_date,
        )
        industry_return = _benchmark_return(
            industry_daily,
            str(source["industry_code"]),
            entry_date,
            exit_date,
        )
        output.append(
            Outcome(
                route=route,
                horizon=horizon,
                signal_date=signal_date,
                symbol=symbol,
                industry_code=str(source["industry_code"]),
                score=(
                    float(source[score_field])
                    if score_field and source[score_field] is not None
                    else None
                ),
                percentile=(
                    float(source[percentile_field])
                    if percentile_field
                    and source[percentile_field] is not None
                    else None
                ),
                selected=symbol in route_candidates,
                gross_return=gross,
                net_return=net,
                market_excess=(
                    (1 + net) / (1 + market_return) - 1
                    if market_return is not None and market_return > -1
                    else None
                ),
                industry_excess=(
                    (1 + net) / (1 + industry_return) - 1
                    if industry_return is not None and industry_return > -1
                    else None
                ),
                matched_excess=(
                    (1 + net) / (1 + matched) - 1
                    if matched is not None and matched > -1
                    else None
                ),
            )
        )
    return output, dict(counters)


def _distribution(values: list[float]) -> dict[str, Any]:
    if not values:
        return {"observations": 0}
    return {
        "observations": len(values),
        "mean": mean(values),
        "median": median(values),
        "win_rate": sum(value > 0 for value in values) / len(values),
        "p05": _quantile(values, 0.05),
        "p25": _quantile(values, 0.25),
        "p75": _quantile(values, 0.75),
        "p95": _quantile(values, 0.95),
        "tail_loss_rate": (
            sum(value <= TAIL_LOSS_THRESHOLD for value in values) / len(values)
        ),
    }


def _fold_for(day: str) -> str:
    for index, (start, end) in enumerate(FOLDS, 1):
        if start <= day <= end:
            return f"fold_{index}"
    return "outside_folds"


def _metrics(
    outcomes: list[Outcome],
    *,
    expert: bool,
) -> dict[str, Any]:
    selected = [row for row in outcomes if row.selected]
    matched = [
        row.matched_excess
        for row in selected
        if row.matched_excess is not None
    ]
    market = [
        row.market_excess for row in selected if row.market_excess is not None
    ]
    industry = [
        row.industry_excess
        for row in selected
        if row.industry_excess is not None
    ]
    folds: dict[str, Any] = {}
    for fold_index in range(1, 5):
        fold = f"fold_{fold_index}"
        values = [
            row.matched_excess
            for row in selected
            if _fold_for(row.signal_date) == fold
            and row.matched_excess is not None
        ]
        folds[fold] = _distribution(values)
    positive_fold_ratio = (
        sum(
            values.get("median") is not None and values["median"] > 0
            for values in folds.values()
        )
        / 4
    )
    daily_ic: dict[str, float] = {}
    top_bottom: dict[str, float] = {}
    if expert:
        for day in sorted({row.signal_date for row in outcomes}):
            day_rows = [
                row
                for row in outcomes
                if row.signal_date == day
                and row.score is not None
                and row.matched_excess is not None
            ]
            value = _spearman(
                [
                    (float(row.score), float(row.matched_excess))
                    for row in day_rows
                ]
            )
            if value is not None:
                daily_ic[day] = value
            top = [
                float(row.matched_excess)
                for row in day_rows
                if row.percentile is not None and row.percentile >= 0.90
            ]
            bottom = [
                float(row.matched_excess)
                for row in day_rows
                if row.percentile is not None and row.percentile <= 0.10
            ]
            if top and bottom:
                top_bottom[day] = mean(top) - mean(bottom)
    ic_folds: dict[str, dict[str, Any]] = {}
    for fold_index in range(1, 5):
        fold = f"fold_{fold_index}"
        values = [
            value
            for day, value in daily_ic.items()
            if _fold_for(day) == fold
        ]
        ic_folds[fold] = _distribution(values)
    positive_ic_fold_ratio = (
        sum(
            values.get("median") is not None and values["median"] > 0
            for values in ic_folds.values()
        )
        / 4
        if expert
        else None
    )
    by_day: dict[str, list[float]] = defaultdict(list)
    for row in selected:
        if row.matched_excess is not None:
            by_day[row.signal_date].append(row.matched_excess)
    industries: dict[str, int] = defaultdict(int)
    for row in selected:
        industries[row.industry_code] += 1
    cohort_symbols: dict[str, set[str]] = defaultdict(set)
    for row in selected:
        cohort_symbols[row.signal_date].add(row.symbol)
    cohort_turnover: list[float] = []
    previous_symbols: set[str] | None = None
    for day in sorted(cohort_symbols):
        current_symbols = cohort_symbols[day]
        if previous_symbols:
            cohort_turnover.append(
                1 - len(previous_symbols & current_symbols)
                / len(previous_symbols)
            )
        previous_symbols = current_symbols
    total = len(selected)
    return {
        "observations": len(selected),
        "canonical_securities": len({row.symbol for row in selected}),
        "cohorts": len({row.signal_date for row in selected}),
        "matched_control_coverage": len(matched) / total if total else 0.0,
        "market_join_coverage": len(market) / total if total else 0.0,
        "industry_join_coverage": len(industry) / total if total else 0.0,
        "median_net_return": (
            median(row.net_return for row in selected) if selected else None
        ),
        "median_market_excess": median(market) if market else None,
        "median_industry_excess": median(industry) if industry else None,
        "median_net_matched_excess": median(matched) if matched else None,
        "max_drawdown": _max_drawdown(
            [
                (day, mean(values))
                for day, values in sorted(by_day.items())
            ]
        ),
        "p05": _quantile(matched, 0.05),
        "tail_loss_rate": (
            sum(value <= TAIL_LOSS_THRESHOLD for value in matched) / len(matched)
            if matched
            else None
        ),
        "positive_fold_ratio": positive_fold_ratio,
        "folds": folds,
        "median_daily_spearman_ic": (
            median(daily_ic.values()) if daily_ic else None
        ),
        "positive_ic_fold_ratio": positive_ic_fold_ratio,
        "ic_folds": ic_folds if expert else None,
        "top_decile_minus_bottom_decile_net_matched_spread": (
            median(top_bottom.values()) if top_bottom else None
        ),
        "industry_concentration": (
            max(industries.values()) / total if total and industries else None
        ),
        "mean_one_way_cohort_turnover": (
            mean(cohort_turnover) if cohort_turnover else None
        ),
        "median_one_way_cohort_turnover": (
            median(cohort_turnover) if cohort_turnover else None
        ),
        "distributions": {
            "net_return": _distribution(
                [row.net_return for row in selected]
            ),
            "matched_excess": _distribution(matched),
        },
    }


def _expert_gate(metrics: dict[str, Any]) -> str:
    survived = bool(
        metrics["observations"] >= 1000
        and metrics["cohorts"] >= 60
        and metrics["matched_control_coverage"] >= 0.95
        and metrics["median_net_matched_excess"] is not None
        and metrics["median_net_matched_excess"] > 0
        and metrics["median_daily_spearman_ic"] is not None
        and metrics["median_daily_spearman_ic"] >= 0.01
        and metrics[
            "top_decile_minus_bottom_decile_net_matched_spread"
        ]
        is not None
        and metrics[
            "top_decile_minus_bottom_decile_net_matched_spread"
        ]
        > 0
        and metrics["positive_fold_ratio"] >= 0.75
        and metrics["positive_ic_fold_ratio"] is not None
        and metrics["positive_ic_fold_ratio"] >= 0.75
        and metrics["max_drawdown"] is not None
        and metrics["max_drawdown"] <= 0.25
        and metrics["median_market_excess"] is not None
        and metrics["median_market_excess"] > 0
        and metrics["median_industry_excess"] is not None
        and metrics["median_industry_excess"] > 0
    )
    return "pass_phase0" if survived else "reject"


def _legacy_gate(
    metrics: dict[str, Any],
    component_increment: dict[str, Any],
) -> str:
    survived = bool(
        metrics["observations"] >= 300
        and metrics["cohorts"] >= 60
        and metrics["matched_control_coverage"] >= 0.95
        and metrics["median_net_matched_excess"] is not None
        and metrics["median_net_matched_excess"] > 0
        and metrics["positive_fold_ratio"] >= 0.75
        and metrics["max_drawdown"] is not None
        and metrics["max_drawdown"] <= 0.25
        and component_increment
        and all(
            value.get("increment") is not None and value["increment"] > 0
            for value in component_increment.values()
        )
    )
    return "pass_phase0" if survived else "reject"


def _component_names(route: str) -> tuple[str, ...]:
    output = []
    if "price" in route:
        output.append("price_signal")
    if "trend" in route:
        output.append("trend_signal")
    if "a8" in route:
        output.append("a8_signal")
    return tuple(output)


def _evaluate(
    database: Path,
    security_database: Path,
    dates: list[str],
    market: dict[tuple[str, str], tuple[float, float]],
    industry_daily: dict[tuple[str, str], tuple[float, float]],
) -> tuple[dict[str, Any], dict[str, Any]]:
    connection = sqlite3.connect(database)
    connection.row_factory = sqlite3.Row
    statuses = sqlite3.connect(security_database)
    statuses.row_factory = sqlite3.Row
    date_index = {day: index for index, day in enumerate(dates)}
    results: dict[str, Any] = {}
    evaluation_audit: dict[str, int] = defaultdict(int)
    try:
        for route in ROUTES:
            expert = route in EXPERT_ROUTES
            score_field = (
                "continuation_score"
                if route == "residual_continuation"
                else "recovery_score"
                if route == "residual_recovery"
                else None
            )
            percentile_field = (
                "continuation_percentile"
                if route == "residual_continuation"
                else "recovery_percentile"
                if route == "residual_recovery"
                else None
            )
            route_results: dict[str, Any] = {}
            outcomes_by_horizon: dict[int, list[Outcome]] = defaultdict(list)
            component_outcomes: dict[
                int, dict[str, list[Outcome]]
            ] = defaultdict(lambda: defaultdict(list))
            signal_dates = [
                row[0]
                for row in connection.execute(
                    f"SELECT DISTINCT trade_date FROM features "
                    f"WHERE {route}=1 ORDER BY trade_date"
                )
            ]
            for day_number, signal_date in enumerate(signal_dates, 1):
                day_rows = {
                    str(row["symbol"]): row
                    for row in connection.execute(
                        "SELECT * FROM features WHERE trade_date=?",
                        (signal_date,),
                    )
                }
                route_symbols = {
                    symbol
                    for symbol, row in day_rows.items()
                    if row[route]
                }
                if expert:
                    state = (
                        "trend_expansion"
                        if route == "residual_continuation"
                        else "recovery"
                    )
                    observed_state = connection.execute(
                        "SELECT market_state FROM states WHERE trade_date=?",
                        (signal_date,),
                    ).fetchone()
                    if not observed_state or observed_state[0] != state:
                        raise AssertionError("专家路线出现非冻结市场状态信号")
                    evaluation_symbols = {
                        symbol
                        for symbol, row in day_rows.items()
                        if row[score_field] is not None
                    }
                else:
                    evaluation_symbols = route_symbols
                for horizon in HORIZONS:
                    rows, counters = _evaluate_day(
                        connection,
                        statuses,
                        day_rows,
                        route_symbols,
                        dates,
                        date_index,
                        signal_date,
                        horizon,
                        market,
                        industry_daily,
                        score_field=score_field,
                        percentile_field=percentile_field,
                        route=route,
                        evaluation_symbols=evaluation_symbols,
                    )
                    outcomes_by_horizon[horizon].extend(rows)
                    for key, value in counters.items():
                        evaluation_audit[key] += value
                    if not expert:
                        for component in _component_names(route):
                            component_symbols = {
                                symbol
                                for symbol, row in day_rows.items()
                                if row[component]
                            }
                            component_rows, _ = _evaluate_day(
                                connection,
                                statuses,
                                day_rows,
                                component_symbols,
                                dates,
                                date_index,
                                signal_date,
                                horizon,
                                market,
                                industry_daily,
                                score_field=None,
                                percentile_field=None,
                                route=component,
                                evaluation_symbols=component_symbols,
                            )
                            component_outcomes[horizon][component].extend(
                                component_rows
                            )
                if day_number % 50 == 0:
                    print(
                        f"phase0 评估 {route}："
                        f"{day_number}/{len(signal_dates)}",
                        flush=True,
                    )
            for horizon in HORIZONS:
                metrics = _metrics(
                    outcomes_by_horizon[horizon],
                    expert=expert,
                )
                component_increment: dict[str, Any] = {}
                if not expert:
                    route_median = metrics["median_net_matched_excess"]
                    for component, rows in component_outcomes[horizon].items():
                        component_metrics = _metrics(rows, expert=False)
                        component_median = component_metrics[
                            "median_net_matched_excess"
                        ]
                        component_increment[component] = {
                            "intersection_median": route_median,
                            "component_median_on_intersection_dates": (
                                component_median
                            ),
                            "increment": (
                                route_median - component_median
                                if route_median is not None
                                and component_median is not None
                                else None
                            ),
                            "component_observations": component_metrics[
                                "observations"
                            ],
                        }
                decision = (
                    _expert_gate(metrics)
                    if expert
                    else _legacy_gate(metrics, component_increment)
                )
                route_results[str(horizon)] = {
                    "gate_decision": decision,
                    "metrics": metrics,
                    "component_increment_same_dates": component_increment,
                }
            results[route] = {
                "route_type": "expert" if expert else "legacy_intersection",
                "primary_horizon": 20,
                "secondary_horizon": 5,
                "horizons": route_results,
                "overall_gate_decision": route_results["20"][
                    "gate_decision"
                ],
            }
    finally:
        connection.close()
        statuses.close()
    return results, dict(evaluation_audit)


def _route_sets(database: Path) -> dict[str, set[tuple[str, str]]]:
    connection = sqlite3.connect(database)
    output: dict[str, set[tuple[str, str]]] = {}
    try:
        for route in ROUTES:
            output[route] = {
                (str(day), str(symbol))
                for day, symbol in connection.execute(
                    f"SELECT trade_date,symbol FROM features WHERE {route}=1"
                )
            }
        for component in ("price_signal", "trend_signal", "a8_signal"):
            output[component] = {
                (str(day), str(symbol))
                for day, symbol in connection.execute(
                    f"SELECT trade_date,symbol FROM features "
                    f"WHERE {component}=1"
                )
            }
    finally:
        connection.close()
    return output


def _overlap(
    qfq: dict[str, set[tuple[str, str]]],
    raw: dict[str, set[tuple[str, str]]],
) -> dict[str, Any]:
    output: dict[str, Any] = {}
    for route in ROUTES:
        union = qfq[route] | raw[route]
        output[route] = {
            "qfq_signals": len(qfq[route]),
            "raw_signals": len(raw[route]),
            "overlap": len(qfq[route] & raw[route]),
            "qfq_only": len(qfq[route] - raw[route]),
            "raw_only": len(raw[route] - qfq[route]),
            "jaccard": (
                len(qfq[route] & raw[route]) / len(union)
                if union
                else None
            ),
        }
    return output


def _legacy_overlap(
    values: dict[str, set[tuple[str, str]]],
) -> dict[str, Any]:
    components = ("price_signal", "trend_signal", "a8_signal")
    universe = set().union(*(values[item] for item in components))
    output: dict[str, Any] = {}
    for index, left in enumerate(components):
        for right in components[index + 1 :]:
            left_values = values[left]
            right_values = values[right]
            union = left_values | right_values
            left_binary = [float(item in left_values) for item in universe]
            right_binary = [float(item in right_values) for item in universe]
            output[f"{left}_vs_{right}"] = {
                "left_signals": len(left_values),
                "right_signals": len(right_values),
                "intersection": len(left_values & right_values),
                "jaccard": (
                    len(left_values & right_values) / len(union)
                    if union
                    else None
                ),
                "binary_phi": _pearson(left_binary, right_binary),
            }
    return output


def _report_markdown(summary: dict[str, Any]) -> str:
    lines = [
        "# Selective Regime 训练段机会审计",
        "",
        f"- 状态：`{summary['status']}`",
        f"- 冻结协议：`{summary['protocol']['sha256']}`",
        f"- 参数哈希：`{summary['implementation']['parameters_sha256']}`",
        f"- 实现哈希：`{summary['implementation']['sha256']}`",
        f"- 最大信号日：`{summary['maximum_signal_date']}`",
        "- 2025 数据读取：`0 rows`",
        "- 2026 最终留出集：`sealed`",
        f"- 下一阶段：`{summary['decision']['next_stage']}`",
        "",
        "| 路线 | 类型 | 5日门控 | 20日门控 | 20日样本 | 20日cohort | 20日中位匹配超额 |",
        "|---|---|---|---|---:|---:|---:|",
    ]
    fmt = lambda value: "NA" if value is None else f"{value:.2%}"
    for route, result in summary["routes"].items():
        metrics = result["horizons"]["20"]["metrics"]
        lines.append(
            f"| {route} | {result['route_type']} | "
            f"{result['horizons']['5']['gate_decision']} | "
            f"{result['horizons']['20']['gate_decision']} | "
            f"{metrics['observations']} | {metrics['cohorts']} | "
            f"{fmt(metrics['median_net_matched_excess'])} |"
        )
    lines.extend(
        [
            "",
            "## 边界",
            "",
            "- 本报告是 phase0 训练段机会审计，不是模型回测。",
            "- 未训练 selector，未运行组合模拟，未搜索周期、状态阈值、权重或交互。",
            "- 2025-01-01 起的数据未读取；2026 继续封闭。",
            "- 只有20日主周期通过全部冻结门槛的路线才允许进入下一阶段。",
            "- 缺失 PIT 状态、行业、基准、特征或匹配对照均未补零。",
            "",
        ]
    )
    return "\n".join(lines)


def run_audit(project: Path) -> dict[str, Any]:
    project = project.resolve()
    protocol = _protocol(project)
    aliases, alias_hash = _canonicalizer(
        project
        / "data"
        / "processed"
        / "security_history"
        / "security_aliases.csv"
    )
    calendar_path = project / "data" / "processed" / "trade_calendar.csv"
    dates = _load_calendar(calendar_path)
    if not dates or dates[-1] != DEVELOPMENT_DATA_END:
        raise ValueError("phase0 官方交易日历未覆盖至 20241231")
    if any(day >= FORBIDDEN_READ_START for day in dates):
        raise AssertionError("phase0 读取了2025交易日")
    security_database, security_hashes = _load_security_state(project)
    (
        market,
        industry_daily,
        industry_by_symbol,
        _,
        benchmark_hashes,
    ) = _load_benchmarks(project, aliases)
    master_path = (
        project
        / "data"
        / "processed"
        / "security_history"
        / "security_master.csv"
    )
    listing_dates, delisting_dates = _security_dates(master_path, aliases)
    qfq_a8, qfq_a8_audit = _a8_signals(
        project,
        aliases,
        dates,
        source_directory="yinhe_daily_qfq",
    )
    qfq_database, qfq_audit, states = _build_features(
        project,
        aliases,
        dates,
        security_database,
        listing_dates,
        delisting_dates,
        industry_daily,
        industry_by_symbol,
        market,
        qfq_a8,
        source_directory="yinhe_daily_qfq",
    )
    raw_a8, raw_a8_audit = _a8_signals(
        project,
        aliases,
        dates,
        source_directory="yinhe_daily",
    )
    raw_database, raw_audit, _ = _build_features(
        project,
        aliases,
        dates,
        security_database,
        listing_dates,
        delisting_dates,
        industry_daily,
        industry_by_symbol,
        market,
        raw_a8,
        source_directory="yinhe_daily",
        fixed_states=states,
    )
    routes, evaluation_audit = _evaluate(
        qfq_database,
        security_database,
        dates,
        market,
        industry_daily,
    )
    qfq_sets = _route_sets(qfq_database)
    raw_sets = _route_sets(raw_database)
    passed = [
        route
        for route, result in routes.items()
        if result["overall_gate_decision"] == "pass_phase0"
    ]
    parameters = {
        "read_start": READ_START,
        "maximum_signal_date": MAXIMUM_SIGNAL_DATE,
        "development_data_end": DEVELOPMENT_DATA_END,
        "forbidden_read_start": FORBIDDEN_READ_START,
        "folds": FOLDS,
        "horizons": HORIZONS,
        "expert_routes": EXPERT_ROUTES,
        "legacy_routes": LEGACY_ROUTES,
        "matched_control_distance": (
            "abs(residual_return20_delta)/0.05 + "
            "abs(residual_drawdown20_delta)/0.05 + "
            "abs(realized_volatility20_delta)/0.02 + "
            "abs(log(turnover20_ratio))"
        ),
        "matched_control_count": MATCH_COUNT,
        "selector_training": False,
        "portfolio_simulation": False,
        "parameter_search": False,
    }
    parameters_hash = hashlib.sha256(
        json.dumps(parameters, sort_keys=True).encode()
    ).hexdigest()
    summary = {
        "schema_version": 1,
        "status": (
            "phase0_route_available"
            if passed
            else "stopped_no_route_passed"
        ),
        "generated_at": datetime.now(UTC).isoformat(),
        "protocol": protocol,
        "maximum_signal_date": MAXIMUM_SIGNAL_DATE,
        "development_data_end": DEVELOPMENT_DATA_END,
        "2025_rows_read": 0,
        "2026_holdout_opened": False,
        "phase": "phase0_training_opportunity_audit_not_model_backtest",
        "implementation": {
            "sha256": _sha256(Path(__file__)),
            "parameters": parameters,
            "parameters_sha256": parameters_hash,
            "legacy_source_hashes": {
                "price_score_only": _sha256(
                    project
                    / "src"
                    / "aplan"
                    / "daily_candidate_historical_validation.py"
                ),
                "trend_any_B1_B2_B3": _sha256(
                    project / "src" / "aplan" / "trend_monitor_validation.py"
                ),
                "a8_exact_text_prompt": _sha256(
                    project / "src" / "aplan" / "tdx_a8_reversal.py"
                ),
            },
        },
        "data": {
            "calendar_sha256": _sha256(calendar_path),
            "security_aliases_sha256": alias_hash,
            "security_state": security_hashes,
            "benchmarks": benchmark_hashes,
            "qfq_a8": qfq_a8_audit,
            "raw_a8": raw_a8_audit,
            "qfq_feature_audit": qfq_audit,
            "raw_feature_audit": raw_audit,
            "evaluation_audit": evaluation_audit,
            "state_dates": {
                state: sorted(
                    day for day, value in states.items() if value == state
                )
                for state in (
                    "trend_expansion",
                    "recovery",
                    "stress",
                    "other",
                )
            },
        },
        "routes": routes,
        "diagnostics": {
            "raw_qfq_route_overlap": _overlap(qfq_sets, raw_sets),
            "legacy_signal_overlap_and_correlation": _legacy_overlap(qfq_sets),
        },
        "decision": {
            "passing_routes": passed,
            "stop_if_no_route_passes": True,
            "next_stage": (
                "allow_task1_to_freeze_one_simple_selector_candidate"
                if passed
                else "stop_no_selector_candidate"
            ),
            "selector_trained": False,
            "model_registry_modified": False,
        },
        "caveats": [
            "This is a training-only opportunity audit, not a model backtest.",
            "No 2025 or 2026 rows were read or evaluated.",
            "No selector, portfolio, nonlinear model, weight, threshold, interaction, or horizon search was run.",
            "The 20-day primary horizon alone determines phase0 continuation; 5-day is secondary.",
            "Legacy intersections must beat zero and every component on the same signal dates.",
            "Raw prices are used only for signal-overlap corporate-action sensitivity.",
            "Missing PIT states, industries, benchmarks, features, or controls are never zero-filled.",
        ],
    }
    output = project / "reports" / "selective_regime_opportunity_audit"
    output.mkdir(parents=True, exist_ok=True)
    json_path = output / "first_pass_summary.json"
    markdown_path = output / "report.md"
    json_path.write_text(
        json.dumps(summary, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    markdown_path.write_text(_report_markdown(summary), encoding="utf-8")
    return {
        "status": summary["status"],
        "protocol_sha256": protocol["sha256"],
        "parameters_sha256": parameters_hash,
        "implementation_sha256": summary["implementation"]["sha256"],
        "maximum_signal_date": MAXIMUM_SIGNAL_DATE,
        "2025_rows_read": 0,
        "2026_holdout_opened": False,
        "route_decisions": {
            route: {
                "5": values["horizons"]["5"]["gate_decision"],
                "20": values["horizons"]["20"]["gate_decision"],
                "overall": values["overall_gate_decision"],
            }
            for route, values in routes.items()
        },
        "next_stage": summary["decision"]["next_stage"],
        "report_json": str(json_path),
        "report_markdown": str(markdown_path),
    }


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Selective regime phase0 训练段机会审计"
    )
    parser.add_argument("--root", default=".")
    parser.add_argument(
        "--maximum-signal-date",
        default=MAXIMUM_SIGNAL_DATE,
    )
    args = parser.parse_args()
    if _digits(args.maximum_signal_date) != MAXIMUM_SIGNAL_DATE:
        raise SystemExit("phase0 最大信号日冻结为20241008；禁止读取2025")
    print(
        json.dumps(
            run_audit(Path(args.root)),
            ensure_ascii=False,
            indent=2,
        )
    )


if __name__ == "__main__":
    main()

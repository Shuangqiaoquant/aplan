from __future__ import annotations

import argparse
import bisect
import csv
import hashlib
import json
import math
import sqlite3
import tomllib
from collections import defaultdict, deque
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path
from statistics import mean, median
from typing import Any, Iterable


AS_OF_TRADE_DATE = "20251231"
PROTOCOL_SHA256 = "0dd9b0aaaf6765f6952632a7667abe190add598bb63c029845e84a236a3e675d"
POSITIVE_FAMILIES = {
    "earnings_improvement",
    "share_buyback",
    "shareholder_increase",
    "major_business",
}
NEGATIVE_FAMILIES = {
    "delisting_risk",
    "regulatory_action",
    "market_risk_warning",
    "shareholder_reduction",
    "litigation",
    "earnings_warning",
    "share_unlock",
}
HORIZONS = (1, 5, 10, 20, 40, 60)
MARKET_CODE = "000300.SH"
COMMISSION = 0.0003
SLIPPAGE = 0.001
STAMP_TAX = 0.0005
MATCH_COUNT = 5
MATCH_EVENT_WINDOW = 5
MIN_TURNOVER_RATIO = 1.2
MIN_OOS_OBSERVATIONS = 100
MIN_OOS_COHORTS = 60
MIN_STABILITY_RATIO = 0.60
TAIL_LOSS_THRESHOLD = -0.10


@dataclass(frozen=True, slots=True)
class Signal:
    symbol: str
    usable_date: str
    family: str
    direction: str


@dataclass(frozen=True, slots=True)
class Observation:
    variant: str
    family: str
    period: str
    horizon: int
    usable_date: str
    entry_date: str
    symbol: str
    gross_return: float
    net_return: float
    market_excess: float | None
    industry_excess: float | None
    matched_excess: float | None
    market_regime: str
    industry_code: str


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _date(value: Any) -> str:
    digits = "".join(character for character in str(value or "") if character.isdigit())
    return digits[:8] if len(digits) >= 8 else ""


def _truthy(value: Any) -> int:
    return int(str(value or "").strip().lower() in {"1", "true", "yes", "y"})


def _number(value: Any) -> float | None:
    try:
        result = float(value)
    except (TypeError, ValueError):
        return None
    return result if math.isfinite(result) else None


def _quantile(values: list[float], probability: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    position = (len(ordered) - 1) * probability
    lower = math.floor(position)
    upper = math.ceil(position)
    if lower == upper:
        return ordered[lower]
    return ordered[lower] + (ordered[upper] - ordered[lower]) * (position - lower)


def _canonicalizer(path: Path) -> tuple[dict[str, str], str]:
    mapping: dict[str, str] = {}
    if not path.exists():
        raise ValueError(f"缺少证券代码别名表：{path}")
    with path.open(encoding="utf-8-sig", newline="") as handle:
        for row in csv.DictReader(handle):
            old = str(row.get("old_symbol") or "").strip()
            new = str(row.get("new_symbol") or "").strip()
            if len(old) == 6 and len(new) == 6:
                mapping[old] = new

    def resolve(symbol: str) -> str:
        seen: set[str] = set()
        while symbol in mapping and symbol not in seen:
            seen.add(symbol)
            symbol = mapping[symbol]
        return symbol

    return {key: resolve(key) for key in mapping}, _sha256(path)


def _canonical(symbol: str, aliases: dict[str, str]) -> str:
    return aliases.get(symbol, symbol)


def _protocol(project: Path) -> dict[str, Any]:
    path = project / "config" / "event_confirmation_validation.toml"
    if path.exists():
        digest = _sha256(path)
        if digest != PROTOCOL_SHA256:
            raise ValueError(
                "公告事件冻结规范哈希不匹配："
                f"expected={PROTOCOL_SHA256} actual={digest}"
            )
        document = tomllib.loads(path.read_text(encoding="utf-8"))
        if document.get("final_holdout_opened") is not False:
            raise ValueError("公告事件冻结规范必须保持 final_holdout_opened=false")
        as_of = _date(document.get("time_design", {}).get("research_as_of_trade_date"))
        if as_of != AS_OF_TRADE_DATE:
            raise ValueError("公告事件冻结规范 as_of_trade_date 已变化")
        return {"path": str(path), "sha256": digest, "document": document}
    return {
        "path": None,
        "sha256": PROTOCOL_SHA256,
        "document": None,
        "note": "Cloud checkout lacked the frozen TOML; embedded frozen parameters were used.",
    }


def _manifest(
    path: Path,
    *,
    expected_statuses: tuple[str, ...] = ("validated",),
) -> dict[str, Any]:
    if not path.exists():
        raise ValueError(f"缺少数据清单：{path}")
    document = json.loads(path.read_text(encoding="utf-8"))
    if document.get("status") not in expected_statuses:
        raise ValueError(
            f"数据清单未通过验收：{path}；"
            f"status={document.get('status')!r} expected={expected_statuses!r}"
        )
    return document


def _load_calendar(path: Path) -> list[str]:
    with path.open(encoding="utf-8-sig", newline="") as handle:
        dates = sorted(
            {
                _date(row.get("trade_date") or row.get("cal_date") or row.get("date"))
                for row in csv.DictReader(handle)
            }
        )
    return [day for day in dates if day and day <= AS_OF_TRADE_DATE]


def _build_bar_database(
    project: Path,
    aliases: dict[str, str],
    alias_hash: str,
) -> tuple[Path, list[str], dict[str, Any]]:
    daily_dir = project / "data" / "processed" / "yinhe_daily_qfq"
    paths = [
        path
        for path in sorted(daily_dir.glob("20??????.csv"))
        if path.stem <= AS_OF_TRADE_DATE
    ]
    if not paths:
        raise ValueError("缺少 2023-2025 银河前复权日线")
    adjustment_manifest = _manifest(
        project / "data" / "processed" / "yinhe_adj_factor" / "manifest.json",
        expected_statuses=("validated", "validated_with_quarantine"),
    )
    if (
        adjustment_manifest.get("missing_factor_rows") != 0
        or adjustment_manifest.get("continuity_breaks") != 0
    ):
        raise ValueError("银河前复权数据存在缺失因子或未解决的连续性异常")
    source_signature = hashlib.sha256(
        json.dumps(
            {
                "manifest": adjustment_manifest,
                "aliases": alias_hash,
                "as_of": AS_OF_TRADE_DATE,
            },
            sort_keys=True,
            ensure_ascii=False,
        ).encode()
    ).hexdigest()
    state = project / "state" / "announcement_event_validation"
    state.mkdir(parents=True, exist_ok=True)
    database = state / "bars_20230101_20251231.sqlite3"
    connection = sqlite3.connect(database)
    connection.execute("PRAGMA journal_mode=WAL")
    connection.execute("PRAGMA synchronous=NORMAL")
    existing = connection.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='metadata'"
    ).fetchone()
    if existing:
        signature = connection.execute(
            "SELECT value FROM metadata WHERE key='source_signature'"
        ).fetchone()
        count = connection.execute("SELECT COUNT(*) FROM bars").fetchone()[0]
        if signature and signature[0] == source_signature and count:
            connection.close()
            return database, [path.stem for path in paths], {
                "status": "reused",
                "rows": count,
                "source_signature": source_signature,
            }
    connection.executescript(
        """
        DROP TABLE IF EXISTS metadata;
        DROP TABLE IF EXISTS bars;
        CREATE TABLE metadata (key TEXT PRIMARY KEY, value TEXT NOT NULL);
        CREATE TABLE bars (
            trade_date TEXT NOT NULL,
            symbol TEXT NOT NULL,
            open REAL NOT NULL,
            high REAL NOT NULL,
            low REAL NOT NULL,
            close REAL NOT NULL,
            turnover REAL NOT NULL,
            previous_close REAL,
            pre20_return REAL,
            median_turnover20 REAL,
            is_suspended INTEGER NOT NULL,
            is_limit_up INTEGER NOT NULL,
            PRIMARY KEY (trade_date, symbol)
        );
        CREATE INDEX idx_bars_symbol_date ON bars(symbol, trade_date);
        """
    )
    histories: dict[str, tuple[deque[float], deque[float]]] = defaultdict(
        lambda: (deque(maxlen=21), deque(maxlen=20))
    )
    rows_written = 0
    for index, path in enumerate(paths, 1):
        incoming = []
        with path.open(encoding="utf-8-sig", newline="") as handle:
            for row in csv.DictReader(handle):
                raw_symbol = str(row.get("symbol") or "").strip()
                symbol = _canonical(raw_symbol, aliases)
                open_price = _number(row.get("open"))
                high = _number(row.get("high"))
                low = _number(row.get("low"))
                close = _number(row.get("close"))
                turnover = _number(row.get("turnover"))
                if (
                    len(symbol) != 6
                    or None in (open_price, high, low, close, turnover)
                    or min(open_price, high, low, close) <= 0
                ):
                    continue
                closes, turnovers = histories[symbol]
                previous_close = closes[-1] if closes else None
                pre20_return = (
                    closes[-1] / closes[-21] - 1
                    if len(closes) >= 21 and closes[-21] > 0
                    else None
                )
                median_turnover20 = median(turnovers) if len(turnovers) >= 20 else None
                incoming.append(
                    (
                        path.stem,
                        symbol,
                        open_price,
                        high,
                        low,
                        close,
                        turnover,
                        previous_close,
                        pre20_return,
                        median_turnover20,
                        _truthy(row.get("is_suspended")),
                        _truthy(row.get("is_limit_up")),
                    )
                )
                closes.append(close)
                turnovers.append(turnover)
        connection.executemany(
            "INSERT OR REPLACE INTO bars VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
            incoming,
        )
        connection.commit()
        rows_written += len(incoming)
        if index % 100 == 0 or index == len(paths):
            print(
                f"公告验证行情缓存：{index}/{len(paths)}，rows={rows_written}",
                flush=True,
            )
    connection.execute(
        "INSERT INTO metadata VALUES ('source_signature', ?)",
        (source_signature,),
    )
    connection.commit()
    connection.close()
    return database, [path.stem for path in paths], {
        "status": "built",
        "rows": rows_written,
        "source_signature": source_signature,
    }


def _load_events(
    path: Path,
    aliases: dict[str, str],
    date_index: dict[str, int],
) -> tuple[list[Signal], dict[tuple[str, str], list[int]], dict[str, Any]]:
    groups: set[tuple[str, str, str, str]] = set()
    family_indices: dict[tuple[str, str], set[int]] = defaultdict(set)
    raw_rows = 0
    relevant_rows = 0
    stopped_at_holdout = False
    previous_usable = ""
    with path.open(encoding="utf-8-sig", newline="") as handle:
        for row in csv.DictReader(handle):
            raw_rows += 1
            usable = _date(row.get("usable_from_trade_date"))
            if not usable:
                continue
            if previous_usable and usable < previous_usable:
                raise ValueError(
                    "公告事件索引未按 usable_from_trade_date 排序，"
                    "无法安全封存 2026 留出集"
                )
            previous_usable = usable
            if usable > AS_OF_TRADE_DATE:
                stopped_at_holdout = True
                break
            family = str(row.get("event_type") or "")
            impact = str(row.get("impact_hint") or "")
            direction = ""
            if family in POSITIVE_FAMILIES and impact == "positive":
                direction = "positive"
            elif family in NEGATIVE_FAMILIES:
                direction = "negative"
            if not direction:
                continue
            symbol = _canonical(str(row.get("symbol") or "").strip(), aliases)
            if len(symbol) != 6 or usable not in date_index:
                continue
            relevant_rows += 1
            groups.add((symbol, usable, family, direction))
            family_indices[(symbol, family)].add(date_index[usable])
    negative_dates = {
        (symbol, usable)
        for symbol, usable, _, direction in groups
        if direction == "negative"
    }
    signals = [
        Signal(symbol, usable, family, direction)
        for symbol, usable, family, direction in sorted(groups)
        if direction == "negative" or (symbol, usable) not in negative_dates
    ]
    return signals, {
        key: sorted(values) for key, values in family_indices.items()
    }, {
        "rows_scanned_before_holdout": raw_rows,
        "relevant_rows": relevant_rows,
        "aggregated_signal_grains": len(groups),
        "signals_after_negative_override": len(signals),
        "positive_overridden": sum(
            direction == "positive" and (symbol, usable) in negative_dates
            for symbol, usable, _, direction in groups
        ),
        "stopped_at_2026_boundary": stopped_at_holdout,
        "2026_holdout_opened": False,
    }


def _load_benchmarks(
    project: Path,
    aliases: dict[str, str],
) -> tuple[
    dict[tuple[str, str], tuple[float, float]],
    dict[tuple[str, str], tuple[float, float]],
    dict[str, list[tuple[str, str, str]]],
    dict[str, list[str]],
    dict[str, str],
]:
    root = project / "data" / "processed" / "benchmarks"
    manifest = _manifest(root / "manifest.json")
    if (
        _date(manifest.get("coverage_end")) < AS_OF_TRADE_DATE
        or not manifest.get("point_in_time_constituents")
    ):
        raise ValueError("官方基准未覆盖至 20251231 或不具备 PIT 行业成分")
    market: dict[tuple[str, str], tuple[float, float]] = {}
    with (root / "market_indices.csv").open(
        encoding="utf-8-sig", newline=""
    ) as handle:
        for row in csv.DictReader(handle):
            day = _date(row.get("trade_date"))
            if day > AS_OF_TRADE_DATE:
                continue
            if row.get("index_code") == MARKET_CODE:
                open_price, close = _number(row.get("open")), _number(row.get("close"))
                if open_price and close:
                    market[(MARKET_CODE, day)] = (open_price, close)
    industry_daily: dict[tuple[str, str], tuple[float, float]] = {}
    with (root / "industry_daily.csv").open(
        encoding="utf-8-sig", newline=""
    ) as handle:
        for row in csv.DictReader(handle):
            day = _date(row.get("trade_date"))
            if day > AS_OF_TRADE_DATE:
                continue
            open_price, close = _number(row.get("open")), _number(row.get("close"))
            code = str(row.get("index_code") or "")
            if code and day and open_price and close:
                industry_daily[(code, day)] = (open_price, close)
    by_symbol: dict[str, list[tuple[str, str, str]]] = defaultdict(list)
    members: dict[str, list[str]] = defaultdict(list)
    with (root / "industry_constituents.csv").open(
        encoding="utf-8-sig", newline=""
    ) as handle:
        for row in csv.DictReader(handle):
            symbol = _canonical(str(row.get("symbol") or ""), aliases)
            code = str(row.get("index_code") or "")
            in_date = _date(row.get("in_date"))
            out_date = _date(row.get("out_date")) or "99991231"
            if len(symbol) == 6 and code and in_date:
                by_symbol[symbol].append((code, in_date, out_date))
                members[code].append(symbol)
    return market, industry_daily, by_symbol, {
        code: sorted(set(values)) for code, values in members.items()
    }, {
        "manifest_sha256": _sha256(root / "manifest.json"),
        "market_indices_sha256": manifest.get("hashes", {}).get(
            "market_indices.csv", ""
        ),
        "industry_daily_sha256": manifest.get("hashes", {}).get(
            "industry_daily.csv", ""
        ),
        "industry_constituents_sha256": manifest.get("hashes", {}).get(
            "industry_constituents.csv", ""
        ),
    }


def _load_security_state(project: Path) -> tuple[Path, dict[str, Any]]:
    root = project / "data" / "processed" / "security_history"
    manifest_path = root / "manifest.json"
    manifest = _manifest(manifest_path)
    if (
        not manifest.get("point_in_time")
        or _date(manifest.get("coverage_end")) < AS_OF_TRADE_DATE
        or manifest.get("missing_trade_dates") != 0
    ):
        raise ValueError("银河历史证券状态未通过 PIT 完整性验收")
    database = root / "daily_status.sqlite3"
    if not database.exists():
        raise ValueError(f"缺少银河历史证券状态数据库：{database}")
    return database, {
        "manifest_sha256": _sha256(manifest_path),
        "daily_status_sha256": _sha256(database),
        "strict_availability_lag": manifest.get("strict_availability_lag"),
    }


def _industry_at(
    symbol: str,
    day: str,
    by_symbol: dict[str, list[tuple[str, str, str]]],
) -> str:
    for code, in_date, out_date in by_symbol.get(symbol, []):
        if in_date <= day <= out_date:
            return code
    return ""


def _bar(
    connection: sqlite3.Connection,
    day: str,
    symbol: str,
) -> sqlite3.Row | None:
    return connection.execute(
        "SELECT * FROM bars WHERE trade_date=? AND symbol=?",
        (day, symbol),
    ).fetchone()


def _return(
    entry_open: float,
    exit_close: float,
) -> tuple[float, float]:
    gross = exit_close / entry_open - 1
    net = (
        exit_close
        * (1 - SLIPPAGE)
        * (1 - COMMISSION - STAMP_TAX)
        / (entry_open * (1 + SLIPPAGE) * (1 + COMMISSION))
        - 1
    )
    return gross, net


def _benchmark_return(
    values: dict[tuple[str, str], tuple[float, float]],
    code: str,
    entry_date: str,
    exit_date: str,
) -> float | None:
    entry = values.get((code, entry_date))
    exit_value = values.get((code, exit_date))
    if not entry or not exit_value or entry[0] <= 0:
        return None
    return exit_value[1] / entry[0] - 1


def _event_near(
    indices: dict[tuple[str, str], list[int]],
    symbol: str,
    family: str,
    center: int,
) -> bool:
    values = indices.get((symbol, family), [])
    position = bisect.bisect_left(values, center - MATCH_EVENT_WINDOW)
    return position < len(values) and values[position] <= center + MATCH_EVENT_WINDOW


def _matched_controls(
    connection: sqlite3.Connection,
    signal: Signal,
    date_index: dict[str, int],
    entry_date: str,
    exit_dates: dict[int, str],
    industry: str,
    industry_members: dict[str, list[str]],
    industry_by_symbol: dict[str, list[tuple[str, str, str]]],
    family_indices: dict[tuple[str, str], list[int]],
) -> dict[int, float]:
    source = _bar(connection, signal.usable_date, signal.symbol)
    if (
        not source
        or source["pre20_return"] is None
        or source["median_turnover20"] in (None, 0)
        or not industry
    ):
        return {}
    members = [
        symbol
        for symbol in industry_members.get(industry, [])
        if symbol != signal.symbol
        and _industry_at(symbol, signal.usable_date, industry_by_symbol) == industry
        and not _event_near(
            family_indices,
            symbol,
            signal.family,
            date_index[signal.usable_date],
        )
    ]
    if not members:
        return {}
    placeholders = ",".join("?" for _ in members)
    rows = connection.execute(
        f"SELECT * FROM bars WHERE trade_date=? AND symbol IN ({placeholders}) "
        "AND pre20_return IS NOT NULL AND median_turnover20>0",
        (signal.usable_date, *members),
    ).fetchall()
    scored = []
    source_turnover = float(source["median_turnover20"])
    for row in rows:
        distance = (
            abs(float(row["pre20_return"]) - float(source["pre20_return"])) / 0.05
            + abs(
                math.log(
                    float(row["median_turnover20"]) / source_turnover
                )
            )
        )
        scored.append((distance, str(row["symbol"])))
    selected = [symbol for _, symbol in sorted(scored)[:MATCH_COUNT]]
    results: dict[int, list[float]] = defaultdict(list)
    for symbol in selected:
        security_state = connection.execute(
            "SELECT is_suspended FROM security_state.daily_status "
            "WHERE trade_date=? AND symbol=?",
            (entry_date, symbol),
        ).fetchone()
        if not security_state or security_state["is_suspended"]:
            continue
        entry = _bar(connection, entry_date, symbol)
        if (
            not entry
            or entry["is_suspended"]
            or (entry["is_limit_up"] and entry["high"] == entry["low"])
        ):
            continue
        for horizon, exit_date in exit_dates.items():
            exit_row = _bar(connection, exit_date, symbol)
            if not exit_row:
                continue
            _, net = _return(float(entry["open"]), float(exit_row["close"]))
            results[horizon].append(net)
    return {
        horizon: mean(values)
        for horizon, values in results.items()
        if values
    }


def _market_regimes(
    market: dict[tuple[str, str], tuple[float, float]],
    dates: list[str],
) -> dict[str, str]:
    closes = [market.get((MARKET_CODE, day), (0.0, 0.0))[1] for day in dates]
    returns = [
        closes[index] / closes[index - 1] - 1
        if index and closes[index - 1] > 0 and closes[index] > 0
        else 0.0
        for index in range(len(dates))
    ]
    volatility: dict[str, float] = {}
    for index, day in enumerate(dates):
        if index >= 20:
            values = returns[index - 19 : index + 1]
            average = mean(values)
            volatility[day] = math.sqrt(
                mean((value - average) ** 2 for value in values)
            )
    training = [
        value for day, value in volatility.items() if day <= "20241231"
    ]
    low = _quantile(training, 1 / 3) or 0.0
    high = _quantile(training, 2 / 3) or 0.0
    result: dict[str, str] = {}
    for index, day in enumerate(dates):
        if index < 140 or not closes[index]:
            result[day] = "insufficient"
            continue
        ma_now = mean(closes[index - 119 : index + 1])
        ma_prior = mean(closes[index - 139 : index - 19])
        trend = (
            "up"
            if closes[index] > ma_now and ma_now > ma_prior
            else "down"
            if closes[index] < ma_now and ma_now < ma_prior
            else "mixed"
        )
        value = volatility.get(day)
        vol = (
            "unknown"
            if value is None
            else "low"
            if value <= low
            else "high"
            if value >= high
            else "mid"
        )
        result[day] = f"{trend}_{vol}"
    return result


def _evaluate(
    database: Path,
    security_state_database: Path,
    dates: list[str],
    signals: list[Signal],
    family_indices: dict[tuple[str, str], list[int]],
    market: dict[tuple[str, str], tuple[float, float]],
    industry_daily: dict[tuple[str, str], tuple[float, float]],
    industry_by_symbol: dict[str, list[tuple[str, str, str]]],
    industry_members: dict[str, list[str]],
) -> tuple[list[Observation], dict[str, Any]]:
    connection = sqlite3.connect(database)
    connection.row_factory = sqlite3.Row
    connection.execute(
        "ATTACH DATABASE ? AS security_state",
        (str(security_state_database),),
    )
    date_index = {day: index for index, day in enumerate(dates)}
    regimes = _market_regimes(market, dates)
    observations: list[Observation] = []
    counters: dict[str, int] = defaultdict(int)
    for index, signal in enumerate(signals, 1):
        usable_index = date_index.get(signal.usable_date)
        if usable_index is None or usable_index + 1 >= len(dates):
            counters["missing_usable_or_entry_date"] += 1
            continue
        entry_date = dates[usable_index + 1]
        security_state = connection.execute(
            "SELECT is_st, is_suspended FROM security_state.daily_status "
            "WHERE trade_date=? AND symbol=?",
            (entry_date, signal.symbol),
        ).fetchone()
        if not security_state:
            counters["missing_entry_security_state"] += 1
            continue
        counters["entry_security_state_joined"] += 1
        counters["entry_is_st"] += int(bool(security_state["is_st"]))
        if security_state["is_suspended"]:
            counters["untradeable_suspended"] += 1
            continue
        entry = _bar(connection, entry_date, signal.symbol)
        if not entry:
            counters["missing_entry_bar"] += 1
            continue
        variants = (
            ["negative_risk_cohort"]
            if signal.direction == "negative"
            else ["positive_event_only"]
        )
        if signal.direction == "positive":
            confirmation = _bar(connection, signal.usable_date, signal.symbol)
            if (
                confirmation
                and confirmation["previous_close"] is not None
                and confirmation["median_turnover20"] not in (None, 0)
                and confirmation["close"] > confirmation["previous_close"]
                and confirmation["turnover"] / confirmation["median_turnover20"]
                >= MIN_TURNOVER_RATIO
            ):
                variants.append("positive_event_price_confirmed")
            else:
                counters["price_confirmation_failed"] += 1
        if entry["is_suspended"]:
            counters["untradeable_suspended"] += 1
            continue
        if entry["is_limit_up"] and entry["high"] == entry["low"]:
            counters["untradeable_one_price_limit_up"] += 1
            continue
        exit_dates = {
            horizon: dates[usable_index + horizon]
            for horizon in HORIZONS
            if usable_index + horizon < len(dates)
        }
        industry = _industry_at(
            signal.symbol,
            signal.usable_date,
            industry_by_symbol,
        )
        controls = _matched_controls(
            connection,
            signal,
            date_index,
            entry_date,
            exit_dates,
            industry,
            industry_members,
            industry_by_symbol,
            family_indices,
        )
        for horizon, exit_date in exit_dates.items():
            exit_row = _bar(connection, exit_date, signal.symbol)
            if not exit_row:
                counters["missing_exit_bar"] += 1
                continue
            gross, net = _return(float(entry["open"]), float(exit_row["close"]))
            market_return = _benchmark_return(
                market,
                MARKET_CODE,
                entry_date,
                exit_date,
            )
            industry_return = _benchmark_return(
                industry_daily,
                industry,
                entry_date,
                exit_date,
            ) if industry else None
            matched_return = controls.get(horizon)
            period = (
                "development"
                if signal.usable_date <= "20241231"
                else "rolling_oos"
            )
            for variant in variants:
                observations.append(
                    Observation(
                        variant=variant,
                        family=signal.family,
                        period=period,
                        horizon=horizon,
                        usable_date=signal.usable_date,
                        entry_date=entry_date,
                        symbol=signal.symbol,
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
                            (1 + net) / (1 + matched_return) - 1
                            if matched_return is not None and matched_return > -1
                            else None
                        ),
                        market_regime=regimes.get(
                            signal.usable_date,
                            "insufficient",
                        ),
                        industry_code=industry,
                    )
                )
        if index % 1000 == 0 or index == len(signals):
            print(
                f"公告事件验证：{index}/{len(signals)}，"
                f"observations={len(observations)}",
                flush=True,
            )
    connection.close()
    counters["tradable_signal_grains"] = len(
        {(item.variant, item.symbol, item.usable_date, item.family)
         for item in observations}
    )
    counters["input_signal_grains"] = len(signals)
    counters["security_state_join_coverage"] = (
        counters["entry_security_state_joined"] / len(signals)
        if signals else None
    )
    counters["untradeable_rate"] = (
        (
            counters["untradeable_suspended"]
            + counters["untradeable_one_price_limit_up"]
        )
        / len(signals)
        if signals else None
    )
    return observations, dict(counters)


def _max_drawdown(values: list[tuple[str, float]]) -> float | None:
    if not values:
        return None
    by_date: dict[str, list[float]] = defaultdict(list)
    for day, value in values:
        by_date[day].append(value)
    wealth = 1.0
    peak = 1.0
    drawdown = 0.0
    for day in sorted(by_date):
        wealth *= 1 + mean(by_date[day])
        peak = max(peak, wealth)
        drawdown = max(drawdown, 1 - wealth / peak)
    return drawdown


def _stats(rows: list[Observation]) -> dict[str, Any]:
    if not rows:
        return {
            "status": "insufficient_data",
            "observations": 0,
            "signals": 0,
            "canonical_securities": 0,
            "cohorts": 0,
        }
    net = [row.net_return for row in rows]
    gross = [row.gross_return for row in rows]
    market = [row.market_excess for row in rows if row.market_excess is not None]
    industry = [
        row.industry_excess for row in rows if row.industry_excess is not None
    ]
    matched = [row.matched_excess for row in rows if row.matched_excess is not None]
    signals = {
        (row.symbol, row.usable_date, row.family)
        for row in rows
    }
    def distribution(values: list[float]) -> dict[str, Any]:
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

    return {
        "status": "ok",
        "observations": len(rows),
        "signals": len(signals),
        "canonical_securities": len({row.symbol for row in rows}),
        "cohorts": len({row.usable_date for row in rows}),
        "mean_gross_return": mean(gross),
        "mean_net_return": mean(net),
        "median_net_return": median(net),
        "win_rate": sum(value > 0 for value in net) / len(net),
        "p05": _quantile(net, 0.05),
        "p25": _quantile(net, 0.25),
        "p75": _quantile(net, 0.75),
        "p95": _quantile(net, 0.95),
        "tail_loss_rate": (
            sum(value <= TAIL_LOSS_THRESHOLD for value in net) / len(net)
        ),
        "max_drawdown": _max_drawdown(
            [(row.usable_date, row.net_return) for row in rows]
        ),
        "mean_transaction_cost": mean(
            row.gross_return - row.net_return for row in rows
        ),
        "market_join_coverage": len(market) / len(rows),
        "industry_join_coverage": len(industry) / len(rows),
        "matched_control_coverage": len(matched) / len(rows),
        "median_market_excess": median(market) if market else None,
        "median_industry_excess": median(industry) if industry else None,
        "median_matched_excess": median(matched) if matched else None,
        "distributions": {
            "gross_return": distribution(gross),
            "net_return": distribution(net),
            "market_excess": distribution(market),
            "industry_excess": distribution(industry),
            "matched_control_excess": distribution(matched),
        },
    }


def _rolling_ratio(
    rows: list[Observation],
    date_index: dict[str, int],
    *,
    positive: bool,
) -> float | None:
    relevant = [row for row in rows if row.matched_excess is not None]
    if not relevant:
        return None
    values: list[bool] = []
    for start in range(0, len(date_index), 21):
        stop = start + 63
        window = [
            row.matched_excess
            for row in relevant
            if start <= date_index.get(row.usable_date, -1) < stop
        ]
        if len(window) < 20:
            continue
        result = median(window)
        values.append(result > 0 if positive else result < 0)
    return sum(values) / len(values) if values else None


def _regime_ratio(rows: list[Observation], *, positive: bool) -> float | None:
    grouped: dict[str, list[float]] = defaultdict(list)
    for row in rows:
        if row.matched_excess is not None and row.market_regime != "insufficient":
            grouped[row.market_regime].append(row.matched_excess)
    eligible = [values for values in grouped.values() if len(values) >= 30]
    if not eligible:
        return None
    decisions = [
        median(values) > 0 if positive else median(values) < 0
        for values in eligible
    ]
    return sum(decisions) / len(decisions)


def _gate(
    rows: list[Observation],
    date_index: dict[str, int],
    *,
    negative: bool,
    daily_candidate_available: bool,
) -> dict[str, Any]:
    values = _stats(rows)
    rolling = _rolling_ratio(rows, date_index, positive=not negative)
    regime = _regime_ratio(rows, positive=not negative)
    values["positive_rolling_window_ratio" if not negative else "negative_rolling_window_ratio"] = rolling
    values["positive_market_regime_ratio" if not negative else "negative_market_regime_ratio"] = regime
    sample_ready = (
        values.get("observations", 0) >= MIN_OOS_OBSERVATIONS
        and values.get("cohorts", 0) >= MIN_OOS_COHORTS
        and values.get("matched_control_coverage", 0) > 0
    )
    if negative:
        hazard = bool(
            sample_ready
            and values.get("median_matched_excess") is not None
            and values["median_matched_excess"] < 0
            and rolling is not None
            and rolling >= MIN_STABILITY_RATIO
            and regime is not None
            and regime >= MIN_STABILITY_RATIO
        )
        decision = "hazard_supported" if hazard else "reject"
        return {
            "gate_decision": decision,
            "hazard_supported": hazard,
            "risk_gate_eligible": False,
            "daily_candidate_ablation": (
                "data_available_but_ablation_not_run"
                if daily_candidate_available
                else "data_unavailable"
            ),
            "metrics": values,
        }
    survived = bool(
        sample_ready
        and values.get("median_market_excess") is not None
        and values["median_market_excess"] > 0
        and values.get("median_industry_excess") is not None
        and values["median_industry_excess"] > 0
        and values.get("median_matched_excess") is not None
        and values["median_matched_excess"] > 0
        and rolling is not None
        and rolling >= MIN_STABILITY_RATIO
        and regime is not None
        and regime >= MIN_STABILITY_RATIO
        and values.get("max_drawdown") is not None
        and values["max_drawdown"] <= 0.25
    )
    return {
        "gate_decision": "survive_first_pass" if survived else "reject",
        "metrics": values,
    }


def _apply_training_nomination(
    development: dict[str, Any],
    rolling_oos: dict[str, Any],
    *,
    negative: bool,
) -> dict[str, Any]:
    nominated = development["gate_decision"] in {
        "survive_first_pass",
        "hazard_supported",
    }
    accepted = rolling_oos["gate_decision"] in {
        "survive_first_pass",
        "hazard_supported",
    }
    output = dict(rolling_oos)
    output["training_nominated"] = nominated
    output["oos_supported"] = accepted
    if not nominated or not accepted:
        output["gate_decision"] = "reject"
    elif negative:
        output["gate_decision"] = "hazard_supported"
    else:
        output["gate_decision"] = "survive_first_pass"
    return output


def _daily_candidate_available(project: Path) -> bool:
    candidates = (
        project / "data" / "processed" / "daily_candidates",
        project / "reports" / "historical_daily_candidates",
    )
    return any(path.exists() and any(path.glob("20??????.*")) for path in candidates)


def _report_markdown(summary: dict[str, Any]) -> str:
    lines = [
        "# 公告事件首轮冻结验证",
        "",
        f"- 状态：`{summary['status']}`",
        f"- 研究截止：`{summary['as_of_trade_date']}`",
        f"- 冻结规范：`{summary['protocol']['sha256']}`",
        f"- 2026 留出集已打开：`{str(summary['2026_holdout_opened']).lower()}`",
        f"- 公告索引验收：`{summary['data']['announcement_manifest_status']}`",
        f"- 聚合信号：{summary['data']['signals_after_negative_override']}",
        "",
        "## 样本外门控",
        "",
        "| 变体 | 周期 | 决策 | 样本 | 中位市场超额 | 中位行业超额 | 中位匹配超额 |",
        "|---|---:|---|---:|---:|---:|---:|",
    ]
    for variant, horizons in summary["variants"].items():
        for horizon, result in horizons.items():
            metrics = result["rolling_oos"]["metrics"]
            fmt = lambda value: "NA" if value is None else f"{value:.2%}"
            lines.append(
                f"| {variant} | {horizon} | {result['rolling_oos']['gate_decision']} "
                f"| {metrics.get('observations', 0)} "
                f"| {fmt(metrics.get('median_market_excess'))} "
                f"| {fmt(metrics.get('median_industry_excess'))} "
                f"| {fmt(metrics.get('median_matched_excess'))} |"
            )
    lines.extend(
        [
            "",
            "## 边界",
            "",
            "- 未读取或计算 2026 最终留出集。",
            "- mixed、neutral、unknown 不作为正向信号。",
            "- 负面风险仅评估持有多头的后续危险，不构造做空收益。",
            "- 历史 daily_candidate 缺失时，负面结果最多标记 hazard_supported。",
            "- 公告全文、新闻、社区舆情和参数搜索均未启用。",
        ]
    )
    return "\n".join(lines) + "\n"


def run_validation(project: Path) -> dict[str, Any]:
    project = project.resolve()
    protocol = _protocol(project)
    announcement_root = project / "data" / "processed" / "announcements"
    announcement_manifest_path = announcement_root / "manifest.json"
    announcement_manifest = _manifest(announcement_manifest_path)
    if announcement_manifest.get("daily_files") != 1301:
        raise ValueError("公告日文件覆盖不等于 1301")
    if (
        announcement_manifest.get("missing_daily_files") != 0
        or announcement_manifest.get("missing_availability_rows") != 0
        or announcement_manifest.get("pending_availability_rows") != 2216
    ):
        raise ValueError("公告索引完整性或右边界排除数量与冻结规范不一致")
    event_path = announcement_root / "event_index.csv"
    expected_event_hash = (
        announcement_manifest.get("hashes", {}).get("event_index.csv") or ""
    )
    actual_event_hash = _sha256(event_path)
    if not expected_event_hash or actual_event_hash != expected_event_hash:
        raise ValueError("公告事件索引哈希与 manifest 不一致")
    aliases, alias_hash = _canonicalizer(
        project / "data" / "processed" / "security_history" / "security_aliases.csv"
    )
    database, dates, bar_cache = _build_bar_database(
        project,
        aliases,
        alias_hash,
    )
    security_state_database, security_state_hashes = _load_security_state(project)
    date_index = {day: index for index, day in enumerate(dates)}
    signals, family_indices, event_audit = _load_events(
        event_path,
        aliases,
        date_index,
    )
    market, industry_daily, industry_by_symbol, industry_members, benchmark_hashes = (
        _load_benchmarks(project, aliases)
    )
    observations, evaluation_audit = _evaluate(
        database,
        security_state_database,
        dates,
        signals,
        family_indices,
        market,
        industry_daily,
        industry_by_symbol,
        industry_members,
    )
    daily_candidate_available = _daily_candidate_available(project)
    variants: dict[str, dict[str, Any]] = {}
    for variant in (
        "positive_event_only",
        "positive_event_price_confirmed",
        "negative_risk_cohort",
    ):
        variants[variant] = {}
        negative = variant == "negative_risk_cohort"
        for horizon in HORIZONS:
            development = [
                row for row in observations
                if row.variant == variant
                and row.horizon == horizon
                and row.period == "development"
            ]
            oos = [
                row for row in observations
                if row.variant == variant
                and row.horizon == horizon
                and row.period == "rolling_oos"
            ]
            family_results = {}
            families = NEGATIVE_FAMILIES if negative else POSITIVE_FAMILIES
            for family in sorted(families):
                family_development = [
                    row for row in development if row.family == family
                ]
                family_oos = [row for row in oos if row.family == family]
                family_development_gate = _gate(
                    family_development,
                    date_index,
                    negative=negative,
                    daily_candidate_available=daily_candidate_available,
                )
                family_oos_gate = _gate(
                    family_oos,
                    date_index,
                    negative=negative,
                    daily_candidate_available=daily_candidate_available,
                )
                family_results[family] = {
                    "development": family_development_gate,
                    "rolling_oos": _apply_training_nomination(
                        family_development_gate,
                        family_oos_gate,
                        negative=negative,
                    ),
                }
            development_gate = _gate(
                development,
                date_index,
                negative=negative,
                daily_candidate_available=daily_candidate_available,
            )
            oos_gate = _gate(
                oos,
                date_index,
                negative=negative,
                daily_candidate_available=daily_candidate_available,
            )
            variants[variant][str(horizon)] = {
                "development": development_gate,
                "rolling_oos": _apply_training_nomination(
                    development_gate,
                    oos_gate,
                    negative=negative,
                ),
                "families": family_results,
            }
    ablation = {}
    for horizon in HORIZONS:
        first = variants["positive_event_only"][str(horizon)]["rolling_oos"]["metrics"]
        confirmed = variants["positive_event_price_confirmed"][str(horizon)][
            "rolling_oos"
        ]["metrics"]
        ablation[str(horizon)] = {
            "confirmed_minus_unconfirmed_median_matched_excess": (
                confirmed.get("median_matched_excess")
                - first.get("median_matched_excess")
                if confirmed.get("median_matched_excess") is not None
                and first.get("median_matched_excess") is not None
                else None
            ),
            "confirmed_signal_retention": (
                confirmed.get("signals", 0) / first["signals"]
                if first.get("signals")
                else None
            ),
        }
    model_parameters = {
        "as_of_trade_date": AS_OF_TRADE_DATE,
        "positive_families": sorted(POSITIVE_FAMILIES),
        "negative_families": sorted(NEGATIVE_FAMILIES),
        "horizons": HORIZONS,
        "costs": {
            "commission_each_side": COMMISSION,
            "slippage_each_side": SLIPPAGE,
            "stamp_tax_sell": STAMP_TAX,
        },
        "matched_control": {
            "count_max": MATCH_COUNT,
            "event_exclusion_window": MATCH_EVENT_WINDOW,
            "distance": "abs(pre20_return_delta)/0.05 + abs(log(turnover20_ratio))",
        },
        "confirmation_turnover_ratio": MIN_TURNOVER_RATIO,
    }
    model_hash = hashlib.sha256(
        json.dumps(model_parameters, sort_keys=True).encode()
    ).hexdigest()
    summary = {
        "schema_version": 1,
        "status": "completed_first_pass",
        "generated_at": datetime.now(UTC).isoformat(),
        "as_of_trade_date": AS_OF_TRADE_DATE,
        "2026_holdout_opened": False,
        "research_only": True,
        "protocol": protocol,
        "model": {
            "id": "announcement_event_first_pass_v0_1",
            "parameters": model_parameters,
            "parameters_sha256": model_hash,
            "implementation_sha256": _sha256(Path(__file__)),
        },
        "data": {
            "announcement_manifest_status": announcement_manifest["status"],
            "announcement_manifest_sha256": _sha256(announcement_manifest_path),
            "event_index_sha256": actual_event_hash,
            "announcement_pending_right_edge_rows": announcement_manifest.get(
                "pending_availability_rows"
            ),
            "security_aliases_sha256": alias_hash,
            "benchmark_hashes": benchmark_hashes,
            "security_state_hashes": security_state_hashes,
            "bar_cache": bar_cache,
            **event_audit,
            "evaluation": evaluation_audit,
        },
        "daily_candidate_interaction": (
            "available" if daily_candidate_available else "data_unavailable"
        ),
        "variants": variants,
        "ablation_positive_A_vs_B": ablation,
        "caveats": [
            "2026 final holdout was not opened.",
            "Matched controls are deterministic nearest neighbors, not tuned.",
            "Missing official or matched benchmarks are reported and never replaced with zero.",
            "Negative cohorts are not short portfolios.",
            "Without historical daily_candidate signals, hazard evidence cannot promote a risk gate.",
            "Historical security-state publication timestamps are not exact; states are used only as session metadata.",
            "Full text, news, community opinion, and parameter search are excluded.",
        ],
    }
    output = project / "reports" / "announcement_event_validation"
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
        "as_of_trade_date": AS_OF_TRADE_DATE,
        "2026_holdout_opened": False,
        "protocol_sha256": protocol["sha256"],
        "model_sha256": model_hash,
        "signals": event_audit["signals_after_negative_override"],
        "observations": len(observations),
        "daily_candidate_interaction": summary["daily_candidate_interaction"],
        "report_json": str(json_path),
        "report_markdown": str(markdown_path),
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="公告事件首轮冻结验证")
    parser.add_argument("--root", default=".")
    parser.add_argument("--as-of-trade-date", default=AS_OF_TRADE_DATE)
    args = parser.parse_args()
    if _date(args.as_of_trade_date) != AS_OF_TRADE_DATE:
        raise SystemExit(
            "首轮公告验证冻结为 as_of_trade_date=20251231；禁止打开 2026"
        )
    result = run_validation(Path(args.root))
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()

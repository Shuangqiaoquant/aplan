from __future__ import annotations

import bisect
import csv
import hashlib
import json
import math
import os
import sqlite3
from collections import defaultdict
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping

from .quality import file_sha256
from .yinhe_fundamental_snapshots import (
    PUBLIC_CONSOLIDATED_TYPES,
    STATEMENT_TYPE_PRIORITY,
)
from .yinhe_fundamentals import _market_code, _records


PROTOCOL_SHA256 = (
    "8c615cde9046440deaee5c2e7e54b87933a59d9355f2208f6e2d3ba1f9486b0b"
)
SOURCE_LABEL = "yinhe_pit_derived_valuation_v1"
AS_OF_TRADE_DATE = "20251231"
PRICE_SCALE = 1_000_000.0
OUTPUT_FIELDS = (
    "trade_date",
    "symbol",
    "pe_ttm",
    "pb",
    "total_mv",
    "total_mv_yuan",
    "raw_close",
    "total_share",
    "ttm_net_profit",
    "latest_equity",
    "share_effective_date",
    "share_available_date",
    "profit_available_date",
    "equity_available_date",
    "source",
    "source_hash",
    "quality_flags",
)


def _date(value: Any) -> str:
    if hasattr(value, "strftime"):
        return value.strftime("%Y%m%d")
    digits = "".join(character for character in str(value or "") if character.isdigit())
    return digits[:8] if len(digits) >= 8 else ""


def _symbol(value: Any) -> str:
    text = str(value or "").strip()
    if "." in text:
        left, right = text.split(".", 1)
        text = left if left.isdigit() else right
    digits = "".join(character for character in text if character.isdigit())
    return digits[:6] if len(digits) >= 6 else ""


def _number(value: Any) -> float | None:
    try:
        result = float(value)
    except (TypeError, ValueError):
        return None
    return result if math.isfinite(result) else None


def _field(row: Mapping[str, Any], *names: str) -> Any:
    upper = {str(key).upper(): value for key, value in row.items()}
    for name in names:
        value = upper.get(name.upper())
        if value not in (None, ""):
            return value
    return None


def _row_hash(row: Mapping[str, Any]) -> str:
    payload = json.dumps(dict(row), ensure_ascii=False, sort_keys=True, default=str)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _calendar(project: Path, start: str = "", end: str = "") -> list[str]:
    path = project / "data" / "processed" / "trade_calendar.csv"
    if not path.exists():
        raise ValueError("缺少银河官方交易日历 data/processed/trade_calendar.csv")
    with path.open(encoding="utf-8-sig", newline="") as handle:
        dates = {
            _date(row.get("trade_date") or row.get("cal_date") or row.get("date"))
            for row in csv.DictReader(handle)
            if str(row.get("is_open", "1")).lower() not in {"0", "false", "no"}
        }
    return sorted(
        day for day in dates
        if day and (not start or day >= start) and (not end or day <= end)
    )


def _next_trade_date(calendar: list[str], day: str) -> str:
    index = bisect.bisect_right(calendar, day)
    return calendar[index] if index < len(calendar) else ""


def _aliases(project: Path) -> tuple[dict[str, str], str]:
    path = (
        project / "data" / "processed" / "security_history"
        / "security_aliases.csv"
    )
    aliases: dict[str, str] = {}
    if path.exists():
        with path.open(encoding="utf-8-sig", newline="") as handle:
            for row in csv.DictReader(handle):
                source = _symbol(
                    row.get("alias_symbol")
                    or row.get("old_symbol")
                    or row.get("symbol")
                )
                target = _symbol(
                    row.get("canonical_symbol")
                    or row.get("new_symbol")
                    or row.get("target_symbol")
                )
                if source and target:
                    aliases[source] = target
    return aliases, file_sha256(path) if path.exists() else ""


def _canonical(symbol: str, aliases: Mapping[str, str]) -> str:
    value = _symbol(symbol)
    seen: set[str] = set()
    while value in aliases and value not in seen:
        seen.add(value)
        value = aliases[value]
    return value


def _symbol_pool(project: Path) -> list[str]:
    master = (
        project / "data" / "processed" / "security_history"
        / "security_master.csv"
    )
    if not master.exists():
        raise ValueError("缺少 PIT security_master.csv")
    values: set[str] = set()
    with master.open(encoding="utf-8-sig", newline="") as handle:
        for row in csv.DictReader(handle):
            symbol = _symbol(row.get("symbol"))
            list_date = _date(row.get("list_date"))
            if symbol and (not list_date or list_date <= AS_OF_TRADE_DATE):
                values.add(symbol)
    if not values:
        raise ValueError("PIT security_master.csv 没有可用股票")
    return sorted(values)


def _connect_equity(path: Path) -> sqlite3.Connection:
    path.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(path)
    connection.execute("PRAGMA journal_mode=WAL")
    connection.execute("PRAGMA synchronous=NORMAL")
    connection.execute(
        """
        CREATE TABLE IF NOT EXISTS equity_structure (
            symbol TEXT NOT NULL,
            market_code TEXT NOT NULL,
            ann_date TEXT NOT NULL,
            available_date TEXT NOT NULL,
            change_date TEXT NOT NULL,
            ex_change_date TEXT NOT NULL,
            total_share REAL NOT NULL,
            is_valid TEXT NOT NULL,
            current_sign TEXT NOT NULL,
            source_hash TEXT NOT NULL,
            downloaded_at TEXT NOT NULL,
            PRIMARY KEY (symbol, change_date, ann_date, source_hash)
        )
        """
    )
    connection.execute(
        "CREATE INDEX IF NOT EXISTS idx_equity_asof "
        "ON equity_structure(symbol, available_date, change_date)"
    )
    return connection


def normalize_equity_rows(
    value: Any,
    calendar: list[str],
    downloaded_at: str,
    aliases: Mapping[str, str] | None = None,
) -> tuple[list[dict[str, Any]], dict[str, int]]:
    aliases = aliases or {}
    output: list[dict[str, Any]] = []
    audit: dict[str, int] = defaultdict(int)
    for row in _records(value):
        symbol = _canonical(
            str(_field(row, "MARKET_CODE", "SECURITY_CODE", "SYMBOL") or ""),
            aliases,
        )
        ann_date = _date(_field(row, "ANN_DATE"))
        change_date = _date(_field(row, "CHANGE_DATE"))
        total_share = _number(_field(row, "TOT_SHARE"))
        if not symbol:
            audit["missing_symbol"] += 1
            continue
        if not ann_date:
            audit["missing_announcement_date"] += 1
            continue
        if not change_date:
            audit["missing_change_date"] += 1
            continue
        if total_share is None or total_share <= 0:
            audit["nonpositive_total_share"] += 1
            continue
        available_date = _next_trade_date(calendar, ann_date)
        if not available_date:
            audit["pending_right_edge"] += 1
            continue
        normalized = {
            "symbol": symbol,
            "market_code": str(_field(row, "MARKET_CODE") or ""),
            "ann_date": ann_date,
            "available_date": available_date,
            "change_date": change_date,
            "ex_change_date": _date(_field(row, "EX_CHANGE_DATE")),
            "total_share": total_share,
            "is_valid": str(_field(row, "IS_VALID") or ""),
            "current_sign": str(_field(row, "CURRENT_SIGN") or ""),
            "source_hash": _row_hash(row),
            "downloaded_at": downloaded_at,
        }
        output.append(normalized)
    return output, dict(audit)


def _insert_equity(
    connection: sqlite3.Connection,
    rows: Iterable[dict[str, Any]],
) -> int:
    fields = (
        "symbol", "market_code", "ann_date", "available_date", "change_date",
        "ex_change_date", "total_share", "is_valid", "current_sign",
        "source_hash", "downloaded_at",
    )
    values = [tuple(row[field] for field in fields) for row in rows]
    if not values:
        return 0
    connection.executemany(
        f"INSERT OR REPLACE INTO equity_structure ({','.join(fields)}) "
        f"VALUES ({','.join('?' for _ in fields)})",
        values,
    )
    return len(values)


def sync_equity_structure(
    project: Path,
    *,
    symbols: list[str] | None = None,
    config: Any | None = None,
    chunk_size: int = 50,
    overwrite: bool = False,
    fetcher: Callable[[list[str], str, str, Path], Any] | None = None,
) -> dict[str, Any]:
    project = project.resolve()
    cleaned = sorted({_symbol(item) for item in (symbols or _symbol_pool(project))})
    cleaned = [item for item in cleaned if item]
    if not cleaned:
        raise ValueError("银河股本历史缺少股票代码")
    aliases, aliases_hash = _aliases(project)
    calendar = _calendar(project)
    history_start, history_end = calendar[0], AS_OF_TRADE_DATE
    chunk_size = max(1, int(chunk_size))
    pool_hash = hashlib.sha256("\n".join(cleaned).encode()).hexdigest()[:12]
    state = (
        project / "state" / "yinhe_equity_structure"
        / f"{history_start}_{history_end}_{pool_hash}_{chunk_size}"
    )
    state.mkdir(parents=True, exist_ok=True)
    output = project / "data" / "processed" / "yinhe_equity_structure"
    database = output / "equity_structure.sqlite3"
    cache = project / "data" / "raw" / "yinhe" / "amazingdata_cache"
    cache.mkdir(parents=True, exist_ok=True)
    downloaded_at = datetime.now(UTC).isoformat()
    ad: Any | None = None
    if fetcher is None:
        if config is None:
            raise ValueError("银河股本历史同步缺少登录配置")
        import AmazingData as ad_module  # type: ignore[import-not-found]

        ad = ad_module
        if ad.login(
            username=config.username,
            password=config.password,
            host=config.server_vip,
            port=config.server_port,
        ) is False:
            raise ValueError("AmazingData 登录失败")
        info = ad.InfoData()
        cache_path = f"{cache.resolve()}{os.sep}"

        def fetcher(
            codes: list[str],
            first: str,
            last: str,
            unused_cache: Path,
        ) -> Any:
            return info.get_equity_structure(
                [_market_code(code) for code in codes],
                local_path=cache_path,
                is_local=False,
                begin_date=int(first),
                end_date=int(last),
            )

    chunks = [
        cleaned[index:index + chunk_size]
        for index in range(0, len(cleaned), chunk_size)
    ]
    connection = _connect_equity(database)
    completed = 0
    inserted = 0
    exclusions: dict[str, int] = defaultdict(int)
    try:
        assert fetcher is not None
        for chunk_index, codes in enumerate(chunks, 1):
            checkpoint = state / f"chunk_{chunk_index:04d}.json"
            if checkpoint.exists() and not overwrite:
                completed += 1
                continue
            value = fetcher(codes, history_start, history_end, cache)
            rows, audit = normalize_equity_rows(
                value, calendar, downloaded_at, aliases
            )
            with connection:
                count = _insert_equity(connection, rows)
            inserted += count
            for key, value_count in audit.items():
                exclusions[key] += value_count
            checkpoint.write_text(
                json.dumps(
                    {
                        "chunk": chunk_index,
                        "symbols": codes,
                        "rows": count,
                        "exclusions": audit,
                        "completed_at": datetime.now(UTC).isoformat(),
                    },
                    ensure_ascii=False,
                    indent=2,
                ) + "\n",
                encoding="utf-8",
            )
            completed += 1
            print(
                f"银河 PIT 股本：{completed}/{len(chunks)}，"
                f"symbols={len(codes)}，rows={count}",
                flush=True,
            )
        total_rows = int(
            connection.execute("SELECT COUNT(*) FROM equity_structure").fetchone()[0]
        )
        symbols_with_rows = int(
            connection.execute(
                "SELECT COUNT(DISTINCT symbol) FROM equity_structure"
            ).fetchone()[0]
        )
        future_timing = int(
            connection.execute(
                "SELECT COUNT(*) FROM equity_structure "
                "WHERE available_date<=ann_date"
            ).fetchone()[0]
        )
        connection.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    finally:
        connection.close()
        if ad is not None and hasattr(ad, "logout"):
            try:
                ad.logout()
            except Exception:
                pass
    status = (
        "validated"
        if completed == len(chunks)
        and total_rows > 0
        and symbols_with_rows > 0
        and future_timing == 0
        else "failed_validation"
    )
    manifest_path = output / "manifest.json"
    manifest = {
        "schema_version": 1,
        "status": status,
        "protocol_sha256": PROTOCOL_SHA256,
        "provider": "China Galaxy AmazingData InfoData.get_equity_structure",
        "history_query_start": history_start,
        "history_query_end": history_end,
        "requested_symbols": len(cleaned),
        "symbols_with_rows": symbols_with_rows,
        "rows": total_rows,
        "chunks": len(chunks),
        "completed_chunks": completed,
        "excluded_rows": dict(exclusions),
        "invalid_availability_rows": future_timing,
        "availability_rule": "next official trading day after ANN_DATE",
        "selection_rule": (
            "CHANGE_DATE<=trade_date and available_date<=trade_date; "
            "latest CHANGE_DATE, ANN_DATE, source_hash"
        ),
        "total_share_unit": "ten_thousand_shares",
        "security_aliases_sha256": aliases_hash,
        "database_path": str(database),
        "database_sha256": file_sha256(database),
        "generated_at": datetime.now(UTC).isoformat(),
    }
    manifest_path.write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    return {
        **manifest,
        "inserted_this_run": inserted,
        "manifest_path": str(manifest_path),
    }


def _manifest(path: Path) -> dict[str, Any]:
    if not path.exists():
        raise ValueError(f"缺少 manifest：{path}")
    return json.loads(path.read_text(encoding="utf-8"))


def _prefer_financial(
    candidate: sqlite3.Row,
    current: sqlite3.Row | None,
) -> bool:
    if current is None:
        return True
    candidate_rank = STATEMENT_TYPE_PRIORITY.get(
        str(candidate["statement_type"]), 0
    )
    current_rank = STATEMENT_TYPE_PRIORITY.get(str(current["statement_type"]), 0)
    return (
        str(candidate["actual_ann_date"]),
        str(candidate["downloaded_at"]),
        candidate_rank,
        str(candidate["source_hash"]),
    ) >= (
        str(current["actual_ann_date"]),
        str(current["downloaded_at"]),
        current_rank,
        str(current["source_hash"]),
    )


def _latest_period(
    state: Mapping[str, sqlite3.Row],
    day: str,
) -> tuple[str, sqlite3.Row] | None:
    periods = [period for period in state if period <= day]
    if not periods:
        return None
    period = max(periods)
    return period, state[period]


def derive_ttm(
    income: Mapping[str, sqlite3.Row],
    day: str,
) -> tuple[float | None, str, list[str]]:
    latest = _latest_period(income, day)
    if latest is None:
        return None, "", ["missing_profit_state"]
    period, current = latest
    current_profit = _number(current["net_profit"])
    suffix = period[4:]
    if suffix == "1231":
        if current_profit is None:
            return None, "", ["missing_annual_profit"]
        return current_profit, str(current["usable_from_trade_date"]), []
    if suffix not in {"0331", "0630", "0930"}:
        return None, "", ["single_quarter_or_unknown_report"]
    prior_year = int(period[:4]) - 1
    annual_period = f"{prior_year:04d}1231"
    prior_same_period = f"{prior_year:04d}{suffix}"
    annual = income.get(annual_period)
    prior_same = income.get(prior_same_period)
    components = (
        current_profit,
        _number(annual["net_profit"]) if annual is not None else None,
        _number(prior_same["net_profit"]) if prior_same is not None else None,
    )
    if any(value is None for value in components):
        return None, "", ["missing_ttm_component"]
    available = max(
        str(current["usable_from_trade_date"]),
        str(annual["usable_from_trade_date"]),
        str(prior_same["usable_from_trade_date"]),
    )
    return components[0] + components[1] - components[2], available, []


def _ttm_source_hashes(
    income: Mapping[str, sqlite3.Row],
    day: str,
) -> list[str]:
    latest = _latest_period(income, day)
    if latest is None:
        return []
    period, current = latest
    if period.endswith("1231"):
        return [str(current["source_hash"])]
    suffix = period[4:]
    if suffix not in {"0331", "0630", "0930"}:
        return [str(current["source_hash"])]
    prior_year = int(period[:4]) - 1
    rows = (
        current,
        income.get(f"{prior_year:04d}1231"),
        income.get(f"{prior_year:04d}{suffix}"),
    )
    return [
        str(row["source_hash"]) for row in rows if row is not None
    ]


def _load_equity_events(
    database: Path,
) -> dict[str, list[dict[str, Any]]]:
    connection = sqlite3.connect(database)
    connection.row_factory = sqlite3.Row
    events: dict[str, list[dict[str, Any]]] = defaultdict(list)
    try:
        for row in connection.execute(
            "SELECT * FROM equity_structure "
            "WHERE available_date<=? ORDER BY available_date,symbol,"
            "change_date,ann_date,source_hash",
            (AS_OF_TRADE_DATE,),
        ):
            events[str(row["available_date"])].append(dict(row))
    finally:
        connection.close()
    return events


def _load_financial_events(
    database: Path,
) -> dict[str, list[sqlite3.Row]]:
    connection = sqlite3.connect(database)
    connection.row_factory = sqlite3.Row
    events: dict[str, list[sqlite3.Row]] = defaultdict(list)
    try:
        for row in connection.execute(
            "SELECT * FROM financial_facts "
            "WHERE table_name IN ('income','balance_sheet') "
            "AND usable_from_trade_date<>'' AND usable_from_trade_date<=? "
            "ORDER BY usable_from_trade_date,symbol,period_end,table_name,"
            "actual_ann_date,downloaded_at,source_hash",
            (AS_OF_TRADE_DATE,),
        ):
            if str(row["statement_type"]) in PUBLIC_CONSOLIDATED_TYPES:
                events[str(row["usable_from_trade_date"])].append(row)
    finally:
        connection.close()
    return events


def _write_rows(path: Path, rows: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(".csv.tmp")
    with temporary.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=OUTPUT_FIELDS)
        writer.writeheader()
        writer.writerows(rows)
    temporary.replace(path)


def _daily_hashes(paths: list[Path]) -> tuple[str, dict[str, str]]:
    hashes = {path.name: file_sha256(path) for path in paths}
    aggregate = hashlib.sha256(
        "".join(f"{name}:{value}\n" for name, value in sorted(hashes.items())).encode()
    ).hexdigest()
    return aggregate, hashes


def build_derived_valuations(
    project: Path,
    *,
    start_date: str = "20230101",
    end_date: str = AS_OF_TRADE_DATE,
) -> dict[str, Any]:
    project = project.resolve()
    start, end = _date(start_date), _date(end_date)
    if not start or not end or start > end:
        raise ValueError("银河派生估值日期范围无效")
    if end > AS_OF_TRADE_DATE:
        raise ValueError("银河派生估值禁止生成或读取 2026 数据")
    protocol = project / "config" / "yinhe_derived_valuation.toml"
    if file_sha256(protocol) != PROTOCOL_SHA256:
        raise ValueError("银河派生估值冻结规范哈希不匹配")
    equity_root = project / "data" / "processed" / "yinhe_equity_structure"
    equity_manifest_path = equity_root / "manifest.json"
    equity_manifest = _manifest(equity_manifest_path)
    if equity_manifest.get("status") != "validated":
        raise ValueError("银河 PIT 股本历史未通过验收")
    financial_root = project / "data" / "processed" / "yinhe_fundamentals"
    financial_manifest_path = financial_root / "manifest.json"
    financial_manifest = _manifest(financial_manifest_path)
    if financial_manifest.get("status") != "validated":
        raise ValueError("银河 PIT 财务事实未通过验收")
    security_root = project / "data" / "processed" / "security_history"
    security_manifest_path = security_root / "manifest.json"
    security_manifest = _manifest(security_manifest_path)
    if not security_manifest.get("point_in_time"):
        raise ValueError("银河历史证券状态未通过 PIT 验收")

    calendar = _calendar(project, start, end)
    raw_root = project / "data" / "processed" / "yinhe_daily"
    missing_raw = [day for day in calendar if not (raw_root / f"{day}.csv").exists()]
    if missing_raw:
        raise ValueError(f"银河未复权日线缺少 {len(missing_raw)} 个交易日")
    aliases, aliases_hash = _aliases(project)
    equity_events = _load_equity_events(
        Path(equity_manifest["database_path"])
    )
    financial_events = _load_financial_events(
        financial_root / "financial_facts.sqlite3"
    )
    security_database = security_root / "daily_status.sqlite3"
    if not security_database.exists():
        raise ValueError("缺少银河 PIT daily_status.sqlite3")

    available_shares: dict[str, list[dict[str, Any]]] = defaultdict(list)
    income_state: dict[str, dict[str, sqlite3.Row]] = defaultdict(dict)
    equity_state: dict[str, dict[str, sqlite3.Row]] = defaultdict(dict)
    counters: dict[str, int] = defaultdict(int)
    daily_coverage: dict[str, dict[str, float | int]] = {}
    low_coverage_days: list[dict[str, Any]] = []
    output_root = project / "data" / "processed" / "yinhe_derived_valuations"
    output_root.mkdir(parents=True, exist_ok=True)
    outputs: list[Path] = []
    status_connection = sqlite3.connect(security_database)
    status_connection.row_factory = sqlite3.Row
    try:
        full_calendar = _calendar(project, end=AS_OF_TRADE_DATE)
        for day_index, day in enumerate(full_calendar, 1):
            for row in equity_events.get(day, []):
                available_shares[row["symbol"]].append(row)
            for row in financial_events.get(day, []):
                symbol = _canonical(str(row["symbol"]), aliases)
                target = (
                    income_state[symbol]
                    if row["table_name"] == "income"
                    else equity_state[symbol]
                )
                period = str(row["period_end"])
                if _prefer_financial(row, target.get(period)):
                    target[period] = row
            if day < start or day > end:
                continue
            raw_path = raw_root / f"{day}.csv"
            raw_rows: dict[str, dict[str, str]] = {}
            with raw_path.open(encoding="utf-8-sig", newline="") as handle:
                for raw in csv.DictReader(handle):
                    symbol = _canonical(str(raw.get("symbol") or ""), aliases)
                    if symbol:
                        if symbol in raw_rows:
                            counters["duplicate_keys"] += 1
                        raw_rows[symbol] = raw
            status_rows = {
                _canonical(str(row["symbol"]), aliases): row
                for row in status_connection.execute(
                    "SELECT symbol,is_st,is_suspended FROM daily_status "
                    "WHERE trade_date=?",
                    (day,),
                )
            }
            eligible_status = sum(
                not row["is_st"] and not row["is_suspended"]
                for row in status_rows.values()
            )
            output: list[dict[str, Any]] = []
            valid_raw_rows = 0
            raw_state_joined = 0
            joined_raw = 0
            joined_share = 0
            joined_pe = 0
            joined_pb = 0
            joined_mv = 0
            for symbol, raw in raw_rows.items():
                status = status_rows.get(symbol)
                raw_vendor_close = _number(raw.get("close"))
                if raw_vendor_close is None or raw_vendor_close <= 0:
                    continue
                valid_raw_rows += 1
                if status is None:
                    continue
                raw_state_joined += 1
                if status["is_st"] or status["is_suspended"]:
                    continue
                joined_raw += 1
                raw_close = raw_vendor_close / PRICE_SCALE
                candidates = [
                    row for row in available_shares.get(symbol, [])
                    if row["change_date"] <= day
                ]
                share = max(
                    candidates,
                    key=lambda row: (
                        row["change_date"], row["ann_date"], row["source_hash"]
                    ),
                    default=None,
                )
                flags: list[str] = []
                total_share = _number(share["total_share"]) if share else None
                total_mv_yuan = (
                    raw_close * total_share * 10_000
                    if total_share is not None and total_share > 0
                    else None
                )
                if total_share is None:
                    flags.append("missing_total_share")
                else:
                    joined_share += 1
                if total_mv_yuan is not None:
                    joined_mv += 1
                ttm, profit_available, ttm_flags = derive_ttm(
                    income_state.get(symbol, {}), day
                )
                flags.extend(ttm_flags)
                latest_equity = _latest_period(equity_state.get(symbol, {}), day)
                equity_value = (
                    _number(latest_equity[1]["equity"])
                    if latest_equity is not None else None
                )
                equity_available = (
                    str(latest_equity[1]["usable_from_trade_date"])
                    if latest_equity is not None else ""
                )
                pe = (
                    total_mv_yuan / ttm
                    if total_mv_yuan is not None and ttm not in (None, 0)
                    else None
                )
                pb = (
                    total_mv_yuan / equity_value
                    if total_mv_yuan is not None and equity_value not in (None, 0)
                    else None
                )
                if pe is not None and math.isfinite(pe):
                    joined_pe += 1
                    if pe < 0:
                        flags.append("negative_pe")
                        counters["negative_pe_rows"] += 1
                else:
                    pe = None
                if pb is not None and math.isfinite(pb):
                    joined_pb += 1
                    if pb < 0:
                        flags.append("negative_pb")
                        counters["negative_pb_rows"] += 1
                else:
                    pb = None
                component_hashes = [
                    str(share["source_hash"]) if share else "",
                    *_ttm_source_hashes(
                        income_state.get(symbol, {}), day
                    ),
                    str(latest_equity[1]["source_hash"])
                    if latest_equity else "",
                    day,
                    symbol,
                    f"{raw_close:.8f}",
                ]
                source_hash = hashlib.sha256(
                    "|".join(component_hashes).encode()
                ).hexdigest()
                output.append(
                    {
                        "trade_date": day,
                        "symbol": symbol,
                        "pe_ttm": pe,
                        "pb": pb,
                        "total_mv": (
                            total_mv_yuan / 10_000
                            if total_mv_yuan is not None else None
                        ),
                        "total_mv_yuan": total_mv_yuan,
                        "raw_close": raw_close,
                        "total_share": total_share,
                        "ttm_net_profit": ttm,
                        "latest_equity": equity_value,
                        "share_effective_date": (
                            share["change_date"] if share else ""
                        ),
                        "share_available_date": (
                            share["available_date"] if share else ""
                        ),
                        "profit_available_date": profit_available,
                        "equity_available_date": equity_available,
                        "source": SOURCE_LABEL,
                        "source_hash": source_hash,
                        "quality_flags": ";".join(sorted(set(flags))),
                    }
                )
            output.sort(key=lambda row: row["symbol"])
            path = output_root / f"{day}.csv"
            _write_rows(path, output)
            outputs.append(path)
            denominator = joined_raw
            coverage = {
                "eligible_status_rows": eligible_status,
                "eligible_with_raw_close": joined_raw,
                "output_rows": len(output),
                "raw_close_join_coverage": (
                    raw_state_joined / valid_raw_rows
                    if valid_raw_rows else 0.0
                ),
                "total_share_coverage": (
                    joined_share / denominator if denominator else 0.0
                ),
                "total_mv_coverage": (
                    joined_mv / denominator if denominator else 0.0
                ),
                "pe_ttm_coverage": (
                    joined_pe / denominator if denominator else 0.0
                ),
                "pb_coverage": (
                    joined_pb / denominator if denominator else 0.0
                ),
                "daily_eligible_valuation_coverage": (
                    len(output) / denominator if denominator else 0.0
                ),
            }
            daily_coverage[day] = coverage
            if (
                coverage["raw_close_join_coverage"] < 0.98
                or coverage["total_share_coverage"] < 0.95
                or coverage["total_mv_coverage"] < 0.95
                or coverage["pe_ttm_coverage"] < 0.80
                or coverage["pb_coverage"] < 0.90
                or coverage["daily_eligible_valuation_coverage"] < 0.95
            ):
                low_coverage_days.append({"trade_date": day, **coverage})
            counters["rows"] += len(output)
            counters["future_share_rows"] += sum(
                bool(row["share_available_date"])
                and (
                    row["share_available_date"] > day
                    or row["share_effective_date"] > day
                )
                for row in output
            )
            counters["future_financial_rows"] += sum(
                any(
                    value and value > day
                    for value in (
                        row["profit_available_date"],
                        row["equity_available_date"],
                    )
                )
                for row in output
            )
            counters["nonfinite_output_rows"] += sum(
                any(
                    isinstance(row[field], float)
                    and not math.isfinite(row[field])
                    for field in (
                        "pe_ttm", "pb", "total_mv", "total_mv_yuan",
                        "raw_close", "total_share", "ttm_net_profit",
                        "latest_equity",
                    )
                )
                for row in output
            )
            print(
                f"银河 PIT 派生估值：{len(outputs)}/{len(calendar)}，"
                f"{day} rows={len(output)} pe={coverage['pe_ttm_coverage']:.2%} "
                f"pb={coverage['pb_coverage']:.2%}",
                flush=True,
            )
    finally:
        status_connection.close()

    aggregate_hash, per_file_hashes = _daily_hashes(outputs)
    duplicate_keys = counters["duplicate_keys"]
    status = (
        "validated"
        if outputs
        and not low_coverage_days
        and counters["future_share_rows"] == 0
        and counters["future_financial_rows"] == 0
        and counters["nonfinite_output_rows"] == 0
        and duplicate_keys == 0
        else "failed_validation"
    )
    manifest_path = output_root / "manifest.json"
    manifest = {
        "schema_version": 1,
        "status": status,
        "protocol_id": "yinhe_pit_derived_valuation_v1",
        "protocol_sha256": PROTOCOL_SHA256,
        "coverage_start": calendar[0] if calendar else None,
        "coverage_end": calendar[-1] if calendar else None,
        "trade_dates": len(calendar),
        "daily_files": len(outputs),
        "rows": counters["rows"],
        "duplicate_keys": duplicate_keys,
        "future_share_rows": counters["future_share_rows"],
        "future_financial_rows": counters["future_financial_rows"],
        "nonfinite_output_rows": counters["nonfinite_output_rows"],
        "negative_pe_rows": counters["negative_pe_rows"],
        "negative_pb_rows": counters["negative_pb_rows"],
        "raw_close_source_scale": PRICE_SCALE,
        "raw_close_output_unit": "CNY",
        "total_share_unit": "ten_thousand_shares",
        "total_mv_unit": "ten_thousand_CNY",
        "total_mv_yuan_unit": "CNY",
        "daily_coverage": daily_coverage,
        "low_coverage_days": low_coverage_days,
        "thresholds": {
            "raw_close_join_coverage": 0.98,
            "total_share_coverage": 0.95,
            "total_mv_coverage": 0.95,
            "pe_ttm_coverage": 0.80,
            "pb_coverage": 0.90,
            "daily_eligible_valuation_coverage": 0.95,
        },
        "coverage_denominator": (
            "PIT eligible listed non-ST non-suspended securities with raw close"
        ),
        "source_manifests": {
            "equity_structure": file_sha256(equity_manifest_path),
            "financial_facts": file_sha256(financial_manifest_path),
            "security_history": file_sha256(security_manifest_path),
            "security_aliases": aliases_hash,
        },
        "source_databases": {
            "equity_structure": equity_manifest.get("database_sha256"),
            "financial_facts": financial_manifest.get("database_sha256"),
            "daily_status": security_manifest.get("daily_status_sha256"),
        },
        "daily_files_sha256": aggregate_hash,
        "daily_file_hashes": per_file_hashes,
        "cross_check": _cross_check(project, output_root),
        "availability": {
            "signal_time": "after official close on trade_date",
            "entry_time": "next official trading day open",
            "daily_exact_match_required": True,
            "daily_forward_fill": False,
        },
        "2026_rows": 0,
        "final_holdout_opened": False,
        "generated_at": datetime.now(UTC).isoformat(),
    }
    manifest_path.write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    return {**manifest, "manifest_path": str(manifest_path)}


def _relative_difference(first: float, second: float) -> float | None:
    if second == 0:
        return None
    return abs(first - second) / abs(second)


def _cross_check(project: Path, output_root: Path) -> dict[str, Any]:
    reference = project / "data" / "processed" / "valuations" / "20230103.csv"
    derived = output_root / "20230103.csv"
    if not reference.exists() or not derived.exists():
        return {"status": "data_unavailable", "merged_into_output": False}
    reference_rows: dict[str, dict[str, str]] = {}
    with reference.open(encoding="utf-8-sig", newline="") as handle:
        for row in csv.DictReader(handle):
            symbol = _symbol(row.get("symbol"))
            if symbol:
                reference_rows[symbol] = row
    differences: dict[str, list[float]] = defaultdict(list)
    joined = 0
    with derived.open(encoding="utf-8-sig", newline="") as handle:
        for row in csv.DictReader(handle):
            other = reference_rows.get(_symbol(row.get("symbol")))
            if other is None:
                continue
            joined += 1
            for derived_field, reference_field, label in (
                ("total_mv", "total_mv", "total_mv_relative_difference"),
                ("pe_ttm", "pe", "pe_ttm_relative_difference"),
                ("pb", "pb", "pb_relative_difference"),
            ):
                first = _number(row.get(derived_field))
                second = _number(
                    other.get(reference_field)
                    or (
                        other.get("pe_ttm")
                        if reference_field == "pe" else None
                    )
                )
                if first is not None and second is not None:
                    value = _relative_difference(first, second)
                    if value is not None:
                        differences[label].append(value)
    return {
        "status": "diagnostic_only" if joined >= 100 else "insufficient_sample",
        "joined_rows": joined,
        "median_relative_differences": {
            key: sorted(values)[len(values) // 2] if values else None
            for key, values in differences.items()
        },
        "reference": str(reference),
        "merged_into_output": False,
    }


def accept_derived_valuations(project: Path) -> dict[str, Any]:
    path = (
        project.resolve() / "data" / "processed"
        / "yinhe_derived_valuations" / "manifest.json"
    )
    manifest = _manifest(path)
    required = (
        manifest.get("protocol_sha256") == PROTOCOL_SHA256
        and manifest.get("status") == "validated"
        and manifest.get("coverage_end") <= AS_OF_TRADE_DATE
        and manifest.get("2026_rows") == 0
        and not manifest.get("final_holdout_opened")
    )
    return {
        "status": "validated" if required else "failed_validation",
        "protocol_sha256": manifest.get("protocol_sha256"),
        "coverage_start": manifest.get("coverage_start"),
        "coverage_end": manifest.get("coverage_end"),
        "daily_files": manifest.get("daily_files"),
        "rows": manifest.get("rows"),
        "low_coverage_days": len(manifest.get("low_coverage_days", [])),
        "future_share_rows": manifest.get("future_share_rows"),
        "future_financial_rows": manifest.get("future_financial_rows"),
        "duplicate_keys": manifest.get("duplicate_keys"),
        "daily_files_sha256": manifest.get("daily_files_sha256"),
        "manifest_sha256": file_sha256(path),
        "manifest_path": str(path),
        "2026_holdout_opened": False,
    }

from __future__ import annotations

import csv
import hashlib
import json
import sqlite3
from collections import defaultdict
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Iterable

from .quality import file_sha256


SNAPSHOT_FIELDS = (
    "symbol",
    "period_end",
    "publish_time",
    "source",
    "source_hash",
    "revenue_growth",
    "net_profit_growth",
    "roe",
    "operating_cashflow_to_profit",
    "debt_to_assets",
    "source_tables",
    "quality_flags",
)
NOTICE_FIELDS = (
    "symbol",
    "period_end",
    "publish_time",
    "notice_type",
    "notice_change_min",
    "notice_change_max",
    "summary",
    "source_hash",
)
STATEMENT_TABLES = {"balance_sheet", "income", "cash_flow"}
PUBLIC_CONSOLIDATED_TYPES = {"1", "5", "27", "28", "36", "45"}
STATEMENT_TYPE_PRIORITY = {
    "5": 10,
    "27": 20,
    "28": 30,
    "36": 40,
    "45": 50,
    "1": 100,
}


def _number(value: Any) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if number == number else None


def _ratio(numerator: Any, denominator: Any) -> float | None:
    first, second = _number(numerator), _number(denominator)
    if first is None or second in (None, 0):
        return None
    return first / second


def _growth(current: Any, previous: Any) -> float | None:
    ratio = _ratio(current, previous)
    return None if ratio is None else ratio - 1.0


def _percent(value: Any) -> float | None:
    number = _number(value)
    return None if number is None else number / 100.0


def _previous_year(period: str) -> str:
    if len(period) != 8 or not period.isdigit():
        return ""
    return f"{int(period[:4]) - 1:04d}{period[4:]}"


def _next_year(period: str) -> str:
    if len(period) != 8 or not period.isdigit():
        return ""
    return f"{int(period[:4]) + 1:04d}{period[4:]}"


def _publish_time(trade_date: str) -> str:
    return (
        f"{trade_date[:4]}-{trade_date[4:6]}-{trade_date[6:8]}"
        "T09:30:00+08:00"
    )


def _period_date(period: str) -> str:
    return f"{period[:4]}-{period[4:6]}-{period[6:8]}"


def _eligible(row: sqlite3.Row) -> bool:
    table = str(row["table_name"])
    if table not in STATEMENT_TABLES:
        return table == "profit_express"
    return str(row["statement_type"]) in PUBLIC_CONSOLIDATED_TYPES


def _prefer(candidate: sqlite3.Row, current: sqlite3.Row | None) -> bool:
    if current is None:
        return True
    candidate_rank = STATEMENT_TYPE_PRIORITY.get(
        str(candidate["statement_type"]), 0
    )
    current_rank = STATEMENT_TYPE_PRIORITY.get(
        str(current["statement_type"]), 0
    )
    if candidate_rank != current_rank:
        return candidate_rank > current_rank
    return (
        str(candidate["actual_ann_date"]),
        str(candidate["downloaded_at"]),
        str(candidate["source_hash"]),
    ) >= (
        str(current["actual_ann_date"]),
        str(current["downloaded_at"]),
        str(current["source_hash"]),
    )


def _snapshot(
    symbol: str,
    period: str,
    usable_date: str,
    state: dict[str, dict[str, sqlite3.Row]],
) -> dict[str, Any] | None:
    current = state.get(period, {})
    if not current:
        return None
    income = current.get("income")
    balance = current.get("balance_sheet")
    cash = current.get("cash_flow")
    express = current.get("profit_express")
    previous_income = state.get(_previous_year(period), {}).get("income")

    revenue_growth = (
        _percent(express["revenue_yoy"])
        if express and express["revenue_yoy"] is not None
        else _growth(
            income["revenue"] if income else None,
            previous_income["revenue"] if previous_income else None,
        )
    )
    net_profit_growth = (
        _percent(express["net_profit_yoy"])
        if express and express["net_profit_yoy"] is not None
        else _growth(
            income["net_profit"] if income else None,
            previous_income["net_profit"] if previous_income else None,
        )
    )
    roe = _percent(express["roe"]) if express else None
    cashflow_to_profit = _ratio(
        cash["operating_cashflow"] if cash else None,
        income["net_profit"] if income else None,
    )
    debt_to_assets = _ratio(
        balance["total_liabilities"] if balance else None,
        balance["total_assets"] if balance else None,
    )
    metrics = (
        revenue_growth,
        net_profit_growth,
        roe,
        cashflow_to_profit,
        debt_to_assets,
    )
    if all(value is None for value in metrics):
        return None

    source_rows = [
        row for row in (income, balance, cash, express, previous_income)
        if row is not None
    ]
    source_tables = sorted({str(row["table_name"]) for row in source_rows})
    source_hash = hashlib.sha256(
        "|".join(sorted(str(row["source_hash"]) for row in source_rows)).encode()
    ).hexdigest()
    flags = []
    if previous_income is None:
        flags.append("missing_prior_year_income")
    if express is None:
        flags.append("no_profit_express")
    if balance is None:
        flags.append("missing_balance_sheet")
    if cash is None:
        flags.append("missing_cash_flow")
    return {
        "symbol": symbol,
        "period_end": _period_date(period),
        "publish_time": _publish_time(usable_date),
        "source": "yinhe:point_in_time_financial_facts:v1",
        "source_hash": source_hash,
        "revenue_growth": revenue_growth,
        "net_profit_growth": net_profit_growth,
        "roe": roe,
        "operating_cashflow_to_profit": cashflow_to_profit,
        "debt_to_assets": debt_to_assets,
        "source_tables": "|".join(source_tables),
        "quality_flags": "|".join(flags),
    }


def _groups(
    rows: Iterable[sqlite3.Row],
) -> Iterable[tuple[str, list[sqlite3.Row]]]:
    current_date = ""
    group: list[sqlite3.Row] = []
    for row in rows:
        usable_date = str(row["usable_from_trade_date"])
        if group and usable_date != current_date:
            yield current_date, group
            group = []
        current_date = usable_date
        group.append(row)
    if group:
        yield current_date, group


def build_fundamental_snapshots(project: Path) -> dict[str, Any]:
    source_dir = project / "data" / "processed" / "yinhe_fundamentals"
    source_database = source_dir / "financial_facts.sqlite3"
    source_manifest = source_dir / "manifest.json"
    if not source_database.exists() or not source_manifest.exists():
        raise ValueError("缺少已验收的银河时点财务事实库")
    source_info = json.loads(source_manifest.read_text(encoding="utf-8"))
    if source_info.get("status") != "validated":
        raise ValueError("银河时点财务事实库尚未通过验收")

    snapshot_path = source_dir / "fundamental_snapshots.csv"
    notice_path = source_dir / "profit_notice_events.csv"
    snapshot_tmp = snapshot_path.with_suffix(".csv.tmp")
    notice_tmp = notice_path.with_suffix(".csv.tmp")
    connection = sqlite3.connect(source_database)
    connection.row_factory = sqlite3.Row
    symbols = [
        str(row[0])
        for row in connection.execute(
            "SELECT DISTINCT symbol FROM financial_facts ORDER BY symbol"
        )
    ]
    snapshot_count = 0
    notice_count = 0
    duplicate_keys = 0
    metric_counts: dict[str, int] = defaultdict(int)
    seen_keys: set[tuple[str, str, str]] = set()
    try:
        with snapshot_tmp.open("w", encoding="utf-8", newline="") as handle:
            writer = csv.DictWriter(handle, fieldnames=SNAPSHOT_FIELDS)
            writer.writeheader()
            for index, symbol in enumerate(symbols, 1):
                rows = connection.execute(
                    "SELECT * FROM financial_facts "
                    "WHERE symbol=? AND usable_from_trade_date<>'' "
                    "AND table_name<>'profit_notice' "
                    "ORDER BY usable_from_trade_date, period_end, table_name, "
                    "actual_ann_date, downloaded_at, source_hash",
                    (symbol,),
                )
                state: dict[str, dict[str, sqlite3.Row]] = defaultdict(dict)
                for usable_date, event_rows in _groups(rows):
                    affected: set[str] = set()
                    for row in event_rows:
                        if not _eligible(row):
                            continue
                        period = str(row["period_end"])
                        table = str(row["table_name"])
                        if _prefer(row, state[period].get(table)):
                            state[period][table] = row
                            affected.add(period)
                            following = _next_year(period)
                            if following in state:
                                affected.add(following)
                    for period in sorted(affected):
                        item = _snapshot(symbol, period, usable_date, state)
                        if item is None:
                            continue
                        key = (
                            item["symbol"],
                            item["period_end"],
                            item["publish_time"],
                        )
                        if key in seen_keys:
                            duplicate_keys += 1
                            continue
                        seen_keys.add(key)
                        writer.writerow(item)
                        snapshot_count += 1
                        for field in (
                            "revenue_growth",
                            "net_profit_growth",
                            "roe",
                            "operating_cashflow_to_profit",
                            "debt_to_assets",
                        ):
                            metric_counts[field] += int(item[field] is not None)
                if index % 500 == 0 or index == len(symbols):
                    print(
                        f"银河财务快照：{index}/{len(symbols)}，"
                        f"snapshots={snapshot_count}"
                    )

        with notice_tmp.open("w", encoding="utf-8", newline="") as handle:
            writer = csv.DictWriter(handle, fieldnames=NOTICE_FIELDS)
            writer.writeheader()
            for row in connection.execute(
                "SELECT symbol, period_end, usable_from_trade_date, "
                "notice_type, notice_change_min, notice_change_max, summary, "
                "source_hash FROM financial_facts "
                "WHERE table_name='profit_notice' "
                "AND usable_from_trade_date<>'' "
                "ORDER BY usable_from_trade_date, symbol, period_end"
            ):
                writer.writerow(
                    {
                        "symbol": row["symbol"],
                        "period_end": _period_date(row["period_end"]),
                        "publish_time": _publish_time(
                            row["usable_from_trade_date"]
                        ),
                        "notice_type": row["notice_type"],
                        "notice_change_min": row["notice_change_min"],
                        "notice_change_max": row["notice_change_max"],
                        "summary": row["summary"],
                        "source_hash": row["source_hash"],
                    }
                )
                notice_count += 1
    finally:
        connection.close()

    snapshot_tmp.replace(snapshot_path)
    notice_tmp.replace(notice_path)
    status = (
        "validated"
        if snapshot_count > 0 and notice_count > 0 and duplicate_keys == 0
        else "failed_validation"
    )
    manifest_path = source_dir / "snapshot_manifest.json"
    manifest = {
        "schema_version": 1,
        "status": status,
        "provider": "China Galaxy AmazingData",
        "source_database": str(source_database),
        "source_database_sha256": source_info.get("database_sha256"),
        "symbols": len(symbols),
        "snapshots": snapshot_count,
        "profit_notice_events": notice_count,
        "duplicate_keys": duplicate_keys,
        "metric_non_null": dict(metric_counts),
        "statement_type_policy": {
            "included": sorted(PUBLIC_CONSOLIDATED_TYPES),
            "excluded": (
                "single-quarter, adjusted-prior-period, parent-only, "
                "vendor-derived unpublished, and special-purpose statements"
            ),
        },
        "availability_rule": source_info.get("availability_rule"),
        "strict_availability_lag": True,
        "paths": {
            "fundamental_snapshots": str(snapshot_path),
            "profit_notice_events": str(notice_path),
        },
        "hashes": {
            snapshot_path.name: file_sha256(snapshot_path),
            notice_path.name: file_sha256(notice_path),
        },
        "generated_at": datetime.now(UTC).isoformat(),
    }
    manifest_path.write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    return {**manifest, "manifest_path": str(manifest_path)}

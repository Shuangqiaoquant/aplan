from __future__ import annotations

import hashlib
import json
import os
import sqlite3
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping

from .quality import file_sha256


TABLES = (
    "balance_sheet",
    "income",
    "cash_flow",
    "profit_express",
    "profit_notice",
)
METRIC_FIELDS = {
    "balance_sheet": {
        "total_assets": ("TOTAL_ASSETS",),
        "total_liabilities": ("TOTAL_LIAB",),
        "equity": (
            "TOT_SHARE_EQUITY_EXCL_MIN_INT",
            "TOT_SHARE_EQU_EXCL_MIN_INT",
        ),
    },
    "income": {
        "revenue": ("TOT_OPERA_REV",),
        "net_profit": ("NET_PRO_EXCL_MIN_INT_INC",),
    },
    "cash_flow": {
        "operating_cashflow": ("NET_CASH_FLOWS_OPERA_ACT",),
    },
    "profit_express": {
        "total_assets": ("TOTAL_ASSETS",),
        "equity": (
            "TOT_SHARE_EQU_EXCL_MIN_INT",
            "TOT_SHARE_EQUITY_EXCL_MIN_INT",
        ),
        "revenue": ("TOT_OPERA_REV",),
        "net_profit": ("NET_PRO_EXCL_MIN_INT_INC",),
        "roe": ("ROE_WEIGHTED",),
        "revenue_yoy": ("YOY_GR_GROSS_REV",),
        "net_profit_yoy": ("YOY_GR_NET_PROFIT_PARENT",),
    },
    "profit_notice": {
        "notice_change_min": ("P_CHANGE_MIN",),
        "notice_change_max": ("P_CHANGE_MAX",),
    },
}


def _date(value: Any) -> str:
    if hasattr(value, "strftime"):
        return value.strftime("%Y%m%d")
    digits = "".join(character for character in str(value or "") if character.isdigit())
    return digits[:8] if len(digits) >= 8 else ""


def _symbol(value: Any) -> str:
    digits = "".join(character for character in str(value or "") if character.isdigit())
    return digits[:6] if len(digits) >= 6 else ""


def _market_code(value: Any) -> str:
    symbol = _symbol(value)
    if not symbol:
        return ""
    suffix = (
        "SH"
        if symbol.startswith(("600", "601", "603", "605", "688", "689"))
        else "SZ"
    )
    return f"{symbol}.{suffix}"


def _field(row: Mapping[str, Any], *names: str) -> Any:
    upper = {str(key).upper(): value for key, value in row.items()}
    for name in names:
        value = upper.get(name.upper())
        if value not in (None, ""):
            return value
    return None


def _number(value: Any) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if number == number else None


def _records(value: Any, hint: str = "") -> list[dict[str, Any]]:
    if value is None:
        return []
    if hasattr(value, "reset_index") and hasattr(value, "to_dict"):
        output = []
        for row in value.reset_index().to_dict(orient="records"):
            item = dict(row)
            if hint and not _field(item, "MARKET_CODE", "SECURITY_CODE"):
                item["MARKET_CODE"] = hint
            output.append(item)
        return output
    if isinstance(value, Mapping):
        if {
            "MARKET_CODE", "REPORTING_PERIOD", "ANN_DATE", "ACTUAL_ANN_DATE"
        }.intersection(str(key).upper() for key in value):
            return [dict(value)]
        output: list[dict[str, Any]] = []
        for key, nested in value.items():
            output.extend(_records(nested, str(key) or hint))
        return output
    if isinstance(value, (list, tuple)):
        if (
            len(value) == 2
            and isinstance(value[0], (Mapping, list, tuple))
            and isinstance(value[1], list)
        ):
            if value[1]:
                raise ValueError(f"银河财务接口返回错误：{value[1]}")
            return _records(value[0], hint)
        output = []
        for nested in value:
            output.extend(_records(nested, hint))
        return output
    return []


def _calendar(project: Path) -> list[str]:
    path = project / "data" / "processed" / "trade_calendar.csv"
    if not path.exists():
        raise ValueError("缺少 data/processed/trade_calendar.csv")
    import csv

    with path.open(encoding="utf-8-sig", newline="") as handle:
        dates = {
            _date(row.get("trade_date") or row.get("cal_date") or row.get("date"))
            for row in csv.DictReader(handle)
            if str(row.get("is_open", "1")).lower() not in {"0", "false", "no"}
        }
    return sorted(day for day in dates if day)


def _next_trade_date(calendar: list[str], day: str) -> str:
    return next((candidate for candidate in calendar if candidate > day), "")


def _hash(row: Mapping[str, Any]) -> str:
    payload = json.dumps(dict(row), ensure_ascii=False, sort_keys=True, default=str)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _normalize(
    table_name: str,
    value: Any,
    calendar: list[str],
    downloaded_at: str,
) -> list[dict[str, Any]]:
    output = []
    for row in _records(value):
        symbol = _symbol(_field(row, "MARKET_CODE", "SECURITY_CODE"))
        period_end = _date(_field(row, "REPORTING_PERIOD", "REPORT_DATE"))
        if not symbol or not period_end:
            continue
        if table_name == "profit_notice":
            first_ann = _date(_field(row, "FIRST_ANN_DATE", "ANN_DATE"))
            actual_ann = _date(_field(row, "ANN_DATE")) or first_ann
        else:
            first_ann = _date(_field(row, "ANN_DATE", "ACTUAL_ANN_DATE"))
            actual_ann = _date(_field(row, "ACTUAL_ANN_DATE", "ANN_DATE")) or first_ann
        if not actual_ann:
            continue
        item: dict[str, Any] = {
            "table_name": table_name,
            "symbol": symbol,
            "market_code": str(_field(row, "MARKET_CODE") or ""),
            "period_end": period_end,
            "report_type": str(_field(row, "REPORT_TYPE") or ""),
            "statement_type": str(_field(row, "STATEMENT_TYPE") or ""),
            "first_ann_date": first_ann or actual_ann,
            "actual_ann_date": actual_ann,
            "usable_from_trade_date": _next_trade_date(calendar, actual_ann),
            "notice_type": str(_field(row, "P_TYPECODE") or ""),
            "summary": str(
                _field(
                    row,
                    "PERFORMANCE_SUMMARY",
                    "P_SUMMARY",
                    "P_REASON",
                    "MEMO",
                )
                or ""
            ),
            "source_hash": _hash(row),
            "downloaded_at": downloaded_at,
        }
        for output_field, aliases in METRIC_FIELDS[table_name].items():
            item[output_field] = _number(_field(row, *aliases))
        output.append(item)
    return output


def _connect(path: Path) -> sqlite3.Connection:
    path.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(path)
    connection.execute("PRAGMA journal_mode=WAL")
    connection.execute(
        """
        CREATE TABLE IF NOT EXISTS financial_facts (
            table_name TEXT NOT NULL,
            symbol TEXT NOT NULL,
            market_code TEXT NOT NULL,
            period_end TEXT NOT NULL,
            report_type TEXT NOT NULL,
            statement_type TEXT NOT NULL,
            first_ann_date TEXT NOT NULL,
            actual_ann_date TEXT NOT NULL,
            usable_from_trade_date TEXT NOT NULL,
            total_assets REAL,
            total_liabilities REAL,
            equity REAL,
            revenue REAL,
            net_profit REAL,
            operating_cashflow REAL,
            roe REAL,
            revenue_yoy REAL,
            net_profit_yoy REAL,
            notice_type TEXT NOT NULL,
            notice_change_min REAL,
            notice_change_max REAL,
            summary TEXT NOT NULL,
            source_hash TEXT NOT NULL,
            downloaded_at TEXT NOT NULL,
            PRIMARY KEY (
                table_name, symbol, period_end, statement_type,
                actual_ann_date, source_hash
            )
        )
        """
    )
    connection.execute(
        "CREATE INDEX IF NOT EXISTS idx_financial_facts_asof "
        "ON financial_facts(symbol, usable_from_trade_date, period_end)"
    )
    return connection


def _insert(connection: sqlite3.Connection, rows: Iterable[dict[str, Any]]) -> int:
    fields = (
        "table_name", "symbol", "market_code", "period_end", "report_type",
        "statement_type", "first_ann_date", "actual_ann_date",
        "usable_from_trade_date", "total_assets", "total_liabilities", "equity",
        "revenue", "net_profit", "operating_cashflow", "roe", "revenue_yoy",
        "net_profit_yoy", "notice_type", "notice_change_min",
        "notice_change_max", "summary", "source_hash", "downloaded_at",
    )
    values = [tuple(row.get(field) for field in fields) for row in rows]
    if not values:
        return 0
    placeholders = ",".join("?" for _ in fields)
    connection.executemany(
        f"INSERT OR REPLACE INTO financial_facts ({','.join(fields)}) "
        f"VALUES ({placeholders})",
        values,
    )
    return len(values)


def sync_fundamentals(
    project: Path,
    *,
    start_date: str,
    end_date: str,
    symbols: list[str],
    config: Any | None = None,
    chunk_size: int = 50,
    overwrite: bool = False,
    tables: Iterable[str] | None = None,
    fetcher: Callable[[str, list[str], str, str, Path], Any] | None = None,
) -> dict[str, Any]:
    start, end = _date(start_date), _date(end_date)
    if not start or not end or start > end:
        raise ValueError("银河财务数据起止日期无效")
    cleaned = sorted({symbol for value in symbols if (symbol := _symbol(value))})
    if not cleaned:
        raise ValueError("银河财务数据缺少股票代码")
    selected_tables = tuple(tables or TABLES)
    if (
        not selected_tables
        or len(set(selected_tables)) != len(selected_tables)
        or any(table not in TABLES for table in selected_tables)
    ):
        raise ValueError("银河财务数据请求表无效")
    chunk_size = max(1, chunk_size)
    calendar = _calendar(project)
    database = (
        project / "data" / "processed" / "yinhe_fundamentals"
        / "financial_facts.sqlite3"
    )
    pool_hash = hashlib.sha256(
        "\n".join(cleaned).encode("utf-8")
    ).hexdigest()[:12]
    state_dir = (
        project / "state" / "yinhe_fundamentals"
        / f"{start}_{end}_{pool_hash}_{chunk_size}"
    )
    state_dir.mkdir(parents=True, exist_ok=True)
    cache = project / "data" / "raw" / "yinhe" / "amazingdata_cache"
    cache.mkdir(parents=True, exist_ok=True)
    downloaded_at = datetime.now(UTC).isoformat()

    ad: Any | None = None
    if fetcher is None:
        if config is None:
            raise ValueError("银河财务同步缺少登录配置")
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
            table_name: str,
            codes: list[str],
            first: str,
            last: str,
            unused_cache: Path,
        ) -> Any:
            method = getattr(info, f"get_{table_name}")
            vendor_codes = [_market_code(code) for code in codes]
            return method(
                vendor_codes,
                local_path=cache_path,
                is_local=False,
                begin_date=int(first),
                end_date=int(last),
            )

    connection = _connect(database)
    completed = 0
    inserted = 0
    chunks = [
        cleaned[index:index + chunk_size]
        for index in range(0, len(cleaned), chunk_size)
    ]
    try:
        assert fetcher is not None
        for chunk_index, codes in enumerate(chunks, 1):
            checkpoint = state_dir / f"chunk_{chunk_index:04d}.json"
            if checkpoint.exists() and not overwrite:
                completed += 1
                continue
            counts: dict[str, int] = {}
            with connection:
                for table_name in selected_tables:
                    value = fetcher(table_name, codes, start, end, cache)
                    rows = _normalize(
                        table_name, value, calendar, downloaded_at
                    )
                    counts[table_name] = _insert(connection, rows)
                    inserted += counts[table_name]
            checkpoint.write_text(
                json.dumps(
                    {
                        "chunk": chunk_index,
                        "symbols": codes,
                        "counts": counts,
                        "completed_at": datetime.now(UTC).isoformat(),
                    },
                    ensure_ascii=False,
                    indent=2,
                )
                + "\n",
                encoding="utf-8",
            )
            completed += 1
            print(
                f"银河时点财务：{completed}/{len(chunks)}，"
                f"symbols={len(codes)}，rows={sum(counts.values())}"
            )

        counts_by_table = dict(
            connection.execute(
                "SELECT table_name, COUNT(*) FROM financial_facts "
                "GROUP BY table_name"
            ).fetchall()
        )
        total_rows = int(
            connection.execute(
                "SELECT COUNT(*) FROM financial_facts"
            ).fetchone()[0]
        )
        invalid_timing = int(
            connection.execute(
                "SELECT COUNT(*) FROM financial_facts "
                "WHERE usable_from_trade_date <> '' "
                "AND usable_from_trade_date <= actual_ann_date"
            ).fetchone()[0]
        )
        pending_availability = int(
            connection.execute(
                "SELECT COUNT(*) FROM financial_facts "
                "WHERE usable_from_trade_date = ''"
            ).fetchone()[0]
        )
        correction_versions = int(
            connection.execute(
                "SELECT COUNT(*) FROM financial_facts "
                "WHERE first_ann_date <> actual_ann_date"
            ).fetchone()[0]
        )
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
        and all(counts_by_table.get(table, 0) > 0 for table in selected_tables)
        and invalid_timing == 0
        else "failed_validation"
    )
    manifest_path = database.parent / "manifest.json"
    manifest = {
        "schema_version": 1,
        "status": status,
        "provider": "China Galaxy AmazingData",
        "coverage_reporting_period_start": start,
        "coverage_reporting_period_end": end,
        "requested_tables": list(selected_tables),
        "symbols": len(cleaned),
        "symbol_pool_sha256": pool_hash,
        "chunks": len(chunks),
        "completed_chunks": completed,
        "rows": total_rows,
        "rows_by_table": counts_by_table,
        "correction_versions": correction_versions,
        "invalid_timing_rows": invalid_timing,
        "pending_availability_rows": pending_availability,
        "strict_availability_lag": True,
        "availability_field": "usable_from_trade_date",
        "availability_rule": (
            "Conservative next official trading day after ACTUAL_ANN_DATE; "
            "FIRST_ANN_DATE and every correction version are preserved."
        ),
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

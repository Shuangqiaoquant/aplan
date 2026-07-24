from __future__ import annotations

import csv
import hashlib
import json
import os
import re
import sqlite3
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Callable, Iterable


MASTER_FIELDS = (
    "ts_code",
    "symbol",
    "name",
    "industry",
    "list_date",
    "delist_date",
)
NAME_HISTORY_FIELDS = (
    "ts_code",
    "name",
    "start_date",
    "end_date",
    "change_reason",
)
ROW_KEYS = {
    "MARKET_CODE",
    "SECURITY_CODE",
    "TRADE_DATE",
    "LISTDATE",
    "DELISTDATE",
    "IS_ST_SEC",
    "IS_SUSP_SEC",
}


def _date_key(value: Any) -> str:
    if hasattr(value, "strftime"):
        return value.strftime("%Y%m%d")
    digits = "".join(character for character in str(value or "") if character.isdigit())
    return digits[:8] if len(digits) >= 8 else ""


def _symbol_key(value: Any) -> str:
    match = re.search(r"(?<!\d)(\d{6})(?!\d)", str(value or ""))
    return match.group(1) if match else ""


def _vendor_code(symbol: str) -> str:
    suffix = "SH" if symbol.startswith(("600", "601", "603", "605", "688", "689")) else "SZ"
    return f"{symbol}.{suffix}"


def _first(row: dict[str, Any], *keys: str) -> Any:
    upper = {str(key).upper(): value for key, value in row.items()}
    for key in keys:
        value = upper.get(key.upper())
        if value not in (None, ""):
            return value
    return None


def _records(value: Any) -> list[dict[str, Any]]:
    if value is None:
        return []
    if hasattr(value, "to_dict"):
        try:
            records = value.to_dict(orient="records")
        except TypeError:
            records = value.to_dict()
        if isinstance(records, list):
            return [dict(row) for row in records if isinstance(row, dict)]
        return _records(records)
    if isinstance(value, dict):
        if ROW_KEYS.intersection(str(key).upper() for key in value):
            return [dict(value)]
        rows: list[dict[str, Any]] = []
        for nested in value.values():
            rows.extend(_records(nested))
        return rows
    if isinstance(value, (list, tuple)):
        rows = []
        for nested in value:
            rows.extend(_records(nested))
        return rows
    return []


def _flag(value: Any) -> int:
    return int(str(value or "").strip().lower() in {"1", "true", "yes", "y"})


def _connect(database: Path) -> sqlite3.Connection:
    database.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(database)
    connection.execute("PRAGMA journal_mode=WAL")
    connection.execute("PRAGMA synchronous=NORMAL")
    connection.execute(
        """
        CREATE TABLE IF NOT EXISTS daily_status (
            trade_date TEXT NOT NULL,
            symbol TEXT NOT NULL,
            is_st INTEGER NOT NULL,
            is_suspended INTEGER NOT NULL,
            is_ex_dividend INTEGER NOT NULL,
            is_ex_right INTEGER NOT NULL,
            pre_close REAL,
            high_limit REAL,
            low_limit REAL,
            available_date TEXT NOT NULL,
            PRIMARY KEY (trade_date, symbol)
        )
        """
    )
    connection.execute(
        "CREATE INDEX IF NOT EXISTS idx_daily_status_symbol_date "
        "ON daily_status(symbol, trade_date)"
    )
    return connection


def _status_rows(value: Any, start: str, end: str) -> Iterable[tuple[Any, ...]]:
    for row in _records(value):
        symbol = _symbol_key(_first(row, "MARKET_CODE", "SECURITY_CODE", "SYMBOL"))
        trade_date = _date_key(_first(row, "TRADE_DATE", "DATE"))
        if not symbol or not start <= trade_date <= end:
            continue
        yield (
            trade_date,
            symbol,
            _flag(_first(row, "IS_ST_SEC", "IS_ST")),
            _flag(_first(row, "IS_SUSP_SEC", "IS_SUSPENDED")),
            _flag(_first(row, "IS_WD_SEC", "IS_EX_DIVIDEND")),
            _flag(_first(row, "IS_XR_SEC", "IS_EX_RIGHT")),
            _number(_first(row, "PRECLOSE", "PRE_CLOSE")),
            _number(_first(row, "HIGH_LIMITED", "HIGH_LIMIT")),
            _number(_first(row, "LOW_LIMITED", "LOW_LIMIT")),
            trade_date,
        )


def _number(value: Any) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _master_rows(value: Any, requested_codes: list[str]) -> list[dict[str, str]]:
    rows: dict[str, dict[str, str]] = {}
    for row in _records(value):
        symbol = _symbol_key(
            _first(row, "MARKET_CODE", "SECURITY_CODE", "SYMBOL", "SECUCODE")
        )
        if not symbol:
            continue
        rows[symbol] = {
            "ts_code": _vendor_code(symbol),
            "symbol": symbol,
            "name": str(_first(row, "SECURITY_NAME", "SYMBOL_NAME", "NAME") or symbol),
            "industry": str(_first(row, "INDUSTRY", "INDUSTRY_NAME") or "未知"),
            "list_date": _date_key(_first(row, "LISTDATE", "LIST_DATE")),
            "delist_date": _date_key(_first(row, "DELISTDATE", "DELIST_DATE")),
        }
    for code in requested_codes:
        symbol = _symbol_key(code)
        if symbol and symbol not in rows:
            rows[symbol] = {
                "ts_code": _vendor_code(symbol),
                "symbol": symbol,
                "name": symbol,
                "industry": "未知",
                "list_date": "",
                "delist_date": "",
            }
    return [rows[symbol] for symbol in sorted(rows)]


def _write_csv(path: Path, fields: tuple[str, ...], rows: Iterable[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(".tmp")
    with temporary.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        writer.writerows(rows)
    temporary.replace(path)


def _write_st_intervals(
    connection: sqlite3.Connection,
    path: Path,
    names: dict[str, str],
) -> int:
    intervals: list[dict[str, str]] = []
    active_symbol = ""
    active_start = ""
    previous_date = ""
    for symbol, trade_date, is_st in connection.execute(
        "SELECT symbol, trade_date, is_st FROM daily_status ORDER BY symbol, trade_date"
    ):
        if symbol != active_symbol:
            if active_symbol and active_start:
                intervals.append(
                    {
                        "ts_code": _vendor_code(active_symbol),
                        "name": f"ST {names.get(active_symbol, active_symbol)}",
                        "start_date": active_start,
                        "end_date": previous_date,
                        "change_reason": "galaxy_is_st_sec",
                    }
                )
            active_symbol = symbol
            active_start = ""
        if is_st and not active_start:
            active_start = trade_date
        elif not is_st and active_start:
            intervals.append(
                {
                    "ts_code": _vendor_code(symbol),
                    "name": f"ST {names.get(symbol, symbol)}",
                    "start_date": active_start,
                    "end_date": previous_date,
                    "change_reason": "galaxy_is_st_sec",
                }
            )
            active_start = ""
        previous_date = trade_date
    if active_symbol and active_start:
        intervals.append(
            {
                "ts_code": _vendor_code(active_symbol),
                "name": f"ST {names.get(active_symbol, active_symbol)}",
                "start_date": active_start,
                "end_date": previous_date,
                "change_reason": "galaxy_is_st_sec",
            }
        )
    _write_csv(path, NAME_HISTORY_FIELDS, intervals)
    return len(intervals)


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _calendar_dates(path: Path, start: str, end: str) -> set[str]:
    if not path.exists():
        return set()
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        return {
            day
            for row in csv.DictReader(handle)
            if (day := _date_key(row.get("trade_date") or row.get("date")))
            and start <= day <= end
            and str(row.get("is_open") or "1").lower() not in {"0", "false", "no"}
        }


def sync_security_history(
    project: Path,
    *,
    start_date: str,
    end_date: str,
    config: Any | None = None,
    chunk_size: int = 50,
    overwrite: bool = False,
    code_fetcher: Callable[[str, str, str], list[str]] | None = None,
    basic_fetcher: Callable[[list[str]], Any] | None = None,
    history_fetcher: Callable[[list[str], str, str, str], Any] | None = None,
) -> dict[str, Any]:
    start, end = _date_key(start_date), _date_key(end_date)
    if not start or not end or start > end:
        raise ValueError("历史证券状态的开始和结束日期无效")
    if chunk_size <= 0:
        raise ValueError("chunk_size 必须大于 0")

    output = project / "data" / "processed" / "security_history"
    raw_cache = project / "data" / "raw" / "yinhe" / "amazingdata_cache"
    checkpoint_dir = project / "state" / "yinhe_security_history" / f"{start}_{end}"
    database = output / "daily_status.sqlite3"
    master_path = output / "security_master.csv"
    names_path = output / "name_history.csv"
    manifest_path = output / "manifest.json"
    raw_cache.mkdir(parents=True, exist_ok=True)
    checkpoint_dir.mkdir(parents=True, exist_ok=True)
    if overwrite:
        database.unlink(missing_ok=True)
        for path in checkpoint_dir.glob("chunk_*.json"):
            path.unlink()
    database_existed = database.exists()

    ad: Any | None = None
    connection: sqlite3.Connection | None = None
    try:
        if code_fetcher is None or basic_fetcher is None or history_fetcher is None:
            if config is None:
                raise ValueError("历史证券状态同步缺少银河登录配置")
            try:
                import AmazingData as ad_module  # type: ignore[import-not-found]
            except Exception as exc:  # noqa: BLE001
                raise ValueError("未能导入 AmazingData SDK") from exc
            ad = ad_module
            logged_in = ad.login(
                username=config.username,
                password=config.password,
                host=config.server_vip,
                port=config.server_port,
            )
            if logged_in is False:
                raise ValueError("AmazingData 登录失败")
            base_data = ad.BaseData()
            info_data = ad.InfoData()
            code_fetcher = lambda first, last, cache: base_data.get_hist_code_list(
                security_type="EXTRA_STOCK_A_SH_SZ",
                start_date=int(first),
                end_date=int(last),
                local_path=cache,
            )
            basic_fetcher = info_data.get_stock_basic
            history_fetcher = lambda codes, first, last, cache: info_data.get_history_stock_status(
                codes,
                local_path=cache,
                is_local=False,
                begin_date=int(first),
                end_date=int(last),
            )

        cache = f"{raw_cache.resolve()}{os.sep}"
        assert code_fetcher is not None
        assert basic_fetcher is not None
        assert history_fetcher is not None
        codes = sorted(
            {
                _vendor_code(symbol)
                for symbol in (_symbol_key(code) for code in code_fetcher(start, end, cache))
                if symbol
            }
        )
        if not codes:
            raise ValueError("银河历史代码表返回为空")
        master = _master_rows(basic_fetcher(codes), codes)
        _write_csv(master_path, MASTER_FIELDS, master)
        missing_list_dates = sum(not row["list_date"] for row in master)

        connection = _connect(database)
        chunks = [codes[index : index + chunk_size] for index in range(0, len(codes), chunk_size)]
        completed = 0
        inserted = 0
        for chunk_index, chunk in enumerate(chunks, 1):
            checkpoint = checkpoint_dir / f"chunk_{chunk_index:04d}.json"
            if checkpoint.exists() and database_existed and not overwrite:
                metadata = json.loads(checkpoint.read_text(encoding="utf-8"))
                completed += 1
                inserted += int(metadata.get("status_rows") or 0)
                print(
                    f"银河历史证券状态：{chunk_index}/{len(chunks)}，"
                    f"checkpoint=复用，symbols={len(chunk)}"
                )
                continue
            rows = list(_status_rows(history_fetcher(chunk, start, end, cache), start, end))
            with connection:
                connection.executemany(
                    """
                    INSERT OR REPLACE INTO daily_status (
                        trade_date, symbol, is_st, is_suspended, is_ex_dividend,
                        is_ex_right, pre_close, high_limit, low_limit, available_date
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    rows,
                )
            metadata = {
                "chunk_index": chunk_index,
                "symbols": chunk,
                "status_rows": len(rows),
                "completed_at": datetime.now(UTC).isoformat(),
            }
            checkpoint.write_text(
                json.dumps(metadata, ensure_ascii=False, indent=2) + "\n",
                encoding="utf-8",
            )
            completed += 1
            inserted += len(rows)
            print(
                f"银河历史证券状态：{chunk_index}/{len(chunks)}，"
                f"symbols={len(chunk)}，rows={len(rows)}"
            )

        names = {row["symbol"]: row["name"] for row in master}
        st_intervals = _write_st_intervals(connection, names_path, names)
        connection.commit()
        connection.close()
        connection = None
        verification = sqlite3.connect(database)
        try:
            status_rows = verification.execute(
                "SELECT COUNT(*) FROM daily_status"
            ).fetchone()[0]
            status_symbols = {
                row[0]
                for row in verification.execute(
                    "SELECT DISTINCT symbol FROM daily_status"
                )
            }
            status_dates = {
                row[0]
                for row in verification.execute(
                    "SELECT DISTINCT trade_date FROM daily_status"
                )
            }
        finally:
            verification.close()
        requested_symbols = {row["symbol"] for row in master}
        missing_status_symbols = sorted(requested_symbols - status_symbols)
        expected_dates = _calendar_dates(
            project / "data" / "processed" / "trade_calendar.csv",
            start,
            end,
        )
        missing_trade_dates = sorted(expected_dates - status_dates)
        point_in_time = bool(
            status_rows
            and not missing_list_dates
            and not missing_status_symbols
            and expected_dates
            and not missing_trade_dates
        )
        manifest = {
            "schema_version": 1,
            "status": "validated" if point_in_time else "failed_validation",
            "provider": "China Galaxy AmazingData",
            "contract": "aplan_security_history_v1",
            "point_in_time": point_in_time,
            "coverage_start": start,
            "coverage_end": end,
            "security_count": len(master),
            "status_rows": status_rows,
            "status_symbols": len(status_symbols),
            "st_intervals": st_intervals,
            "missing_list_dates": missing_list_dates,
            "missing_status_symbols": len(missing_status_symbols),
            "missing_status_symbol_samples": missing_status_symbols[:20],
            "calendar_verified": bool(expected_dates),
            "expected_trade_dates": len(expected_dates),
            "observed_trade_dates": len(status_dates),
            "missing_trade_dates": len(missing_trade_dates),
            "missing_trade_date_samples": missing_trade_dates[:20],
            "status_fields": [
                "listing",
                "delisting",
                "st",
                "suspension",
                "price_limits",
                "ex_dividend",
                "ex_right",
            ],
            "availability_timestamp_field": "available_date",
            "strict_availability_lag": False,
            "availability_note": (
                "Same-day security state is treated as session metadata; "
                "supplier documentation does not provide an exact publication timestamp."
            ),
            "paths": {
                "security_master": str(master_path),
                "name_history": str(names_path),
                "daily_status": str(database),
            },
            "hashes": {
                "security_master_sha256": _sha256(master_path),
                "name_history_sha256": _sha256(names_path),
                "daily_status_sha256": _sha256(database),
            },
            "generated_at": datetime.now(UTC).isoformat(),
        }
        manifest_path.write_text(
            json.dumps(manifest, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        return {
            **manifest,
            "chunks": len(chunks),
            "completed_chunks": completed,
            "downloaded_status_rows": inserted,
            "manifest_path": str(manifest_path),
        }
    finally:
        if connection is not None:
            connection.close()
        if ad is not None and hasattr(ad, "logout"):
            try:
                ad.logout()
            except Exception:
                pass

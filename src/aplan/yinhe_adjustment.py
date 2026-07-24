from __future__ import annotations

import csv
import json
import math
import os
import sqlite3
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Callable, Iterable


DAILY_FIELDS = (
    "symbol",
    "trade_date",
    "open",
    "high",
    "low",
    "close",
    "volume",
    "turnover",
    "is_suspended",
    "is_limit_up",
    "is_limit_down",
)
CONTINUITY_THRESHOLD = 0.205
MAX_QUARANTINE_RATIO = 0.001
MAX_QUARANTINE_SYMBOLS = 5


def _date_key(value: Any) -> str:
    if hasattr(value, "strftime"):
        return value.strftime("%Y%m%d")
    return "".join(character for character in str(value or "") if character.isdigit())[:8]


def _symbol_key(value: Any) -> str:
    text = str(value or "").strip().upper()
    for separator in (".", "_"):
        if separator in text:
            text = text.split(separator, 1)[0]
    return text.zfill(6) if text.isdigit() else text


def _vendor_code(symbol: str) -> str:
    cleaned = _symbol_key(symbol)
    suffix = "SH" if cleaned.startswith(("600", "601", "603", "605", "688", "689")) else "SZ"
    return f"{cleaned}.{suffix}"


def _connect(database: Path) -> sqlite3.Connection:
    database.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(database)
    connection.execute("PRAGMA journal_mode=WAL")
    connection.execute("PRAGMA synchronous=NORMAL")
    connection.execute(
        """
        CREATE TABLE IF NOT EXISTS backward_factors (
            trade_date TEXT NOT NULL,
            symbol TEXT NOT NULL,
            factor REAL NOT NULL,
            PRIMARY KEY (trade_date, symbol)
        )
        """
    )
    connection.execute(
        "CREATE INDEX IF NOT EXISTS idx_backward_factors_symbol_date "
        "ON backward_factors(symbol, trade_date)"
    )
    return connection


def _factor_rows(frame: Any, start: str, end: str) -> Iterable[tuple[str, str, float]]:
    if not hasattr(frame, "columns"):
        raise ValueError(f"银河复权因子返回类型无法解析：{type(frame).__name__}")
    for column in frame.columns:
        symbol = _symbol_key(column)
        if len(symbol) != 6 or not symbol.isdigit():
            continue
        series = frame[column]
        iterator = series.items() if hasattr(series, "items") else []
        for index, value in iterator:
            trade_date = _date_key(index)
            if not start <= trade_date <= end:
                continue
            try:
                factor = float(value)
            except (TypeError, ValueError):
                continue
            if math.isfinite(factor) and factor > 0:
                yield trade_date, symbol, factor


def _write_calendar(path: Path, calendar: Iterable[Any]) -> dict[str, Any]:
    dates = sorted({_date_key(value) for value in calendar if len(_date_key(value)) == 8})
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(".tmp")
    with temporary.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=("trade_date", "is_open"))
        writer.writeheader()
        writer.writerows({"trade_date": day, "is_open": 1} for day in dates)
    temporary.replace(path)
    return {
        "calendar_rows": len(dates),
        "calendar_first": min(dates, default=None),
        "calendar_last": max(dates, default=None),
        "calendar_path": str(path),
    }


def sync_backward_factors(
    project: Path,
    *,
    start_date: str,
    end_date: str,
    symbols: list[str],
    config: Any | None = None,
    chunk_size: int = 50,
    overwrite: bool = False,
    calendar: list[Any] | None = None,
    factor_fetcher: Callable[[list[str], str], Any] | None = None,
) -> dict[str, Any]:
    start = _date_key(start_date)
    end = _date_key(end_date)
    if not start or not end or start > end:
        raise ValueError("复权因子的开始和结束日期无效")
    cleaned_symbols = sorted({_symbol_key(symbol) for symbol in symbols})
    cleaned_symbols = [
        symbol for symbol in cleaned_symbols if len(symbol) == 6 and symbol.isdigit()
    ]
    if not cleaned_symbols:
        raise ValueError("复权因子同步需要至少一个六位股票代码")
    if chunk_size <= 0:
        raise ValueError("chunk_size 必须大于 0")

    factor_dir = project / "data" / "processed" / "yinhe_adj_factor"
    database = factor_dir / "backward_factors.sqlite3"
    checkpoint_dir = project / "state" / "yinhe_adjustment" / f"{start}_{end}"
    cache_dir = project / "data" / "raw" / "yinhe" / "amazingdata_cache"
    calendar_path = project / "data" / "processed" / "trade_calendar.csv"
    checkpoint_dir.mkdir(parents=True, exist_ok=True)
    cache_dir.mkdir(parents=True, exist_ok=True)
    if overwrite:
        database.unlink(missing_ok=True)
        for path in checkpoint_dir.glob("chunk_*.json"):
            path.unlink()
    database_existed = database.exists()

    ad: Any | None = None
    base_data: Any | None = None
    connection: sqlite3.Connection | None = None
    try:
        if factor_fetcher is None:
            if config is None:
                raise ValueError("复权因子同步缺少银河登录配置")
            try:
                import AmazingData as ad_module  # type: ignore[import-not-found]
            except Exception as exc:  # noqa: BLE001 - 供应商包的平台错误不稳定
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
            if calendar is None:
                calendar = list(base_data.get_calendar())
        if calendar is None:
            raise ValueError("未能获取银河官方交易日历")
        calendar_summary = _write_calendar(calendar_path, calendar)
        connection = _connect(database)
        chunks = [
            cleaned_symbols[index : index + chunk_size]
            for index in range(0, len(cleaned_symbols), chunk_size)
        ]
        completed = 0
        inserted = 0
        for chunk_index, chunk in enumerate(chunks, 1):
            checkpoint = checkpoint_dir / f"chunk_{chunk_index:04d}.json"
            if checkpoint.exists() and database_existed and not overwrite:
                metadata = json.loads(checkpoint.read_text(encoding="utf-8"))
                completed += 1
                inserted += int(metadata.get("factor_rows") or 0)
                print(
                    f"银河复权因子：{chunk_index}/{len(chunks)}，"
                    f"checkpoint=复用，symbols={len(chunk)}"
                )
                continue
            vendor_codes = [_vendor_code(symbol) for symbol in chunk]
            if factor_fetcher is not None:
                frame = factor_fetcher(vendor_codes, str(cache_dir))
            else:
                assert base_data is not None
                frame = base_data.get_backward_factor(
                    vendor_codes,
                    local_path=f"{cache_dir.resolve()}{os.sep}",
                    is_local=False,
                )
            rows = list(_factor_rows(frame, start, end))
            with connection:
                connection.executemany(
                    "INSERT OR REPLACE INTO backward_factors "
                    "(trade_date, symbol, factor) VALUES (?, ?, ?)",
                    rows,
                )
            metadata = {
                "chunk_index": chunk_index,
                "symbols": chunk,
                "factor_rows": len(rows),
                "first_date": min((row[0] for row in rows), default=None),
                "last_date": max((row[0] for row in rows), default=None),
                "completed_at": datetime.now(UTC).isoformat(),
            }
            checkpoint.write_text(
                json.dumps(metadata, ensure_ascii=False, indent=2) + "\n",
                encoding="utf-8",
            )
            completed += 1
            inserted += len(rows)
            print(
                f"银河复权因子：{chunk_index}/{len(chunks)}，"
                f"symbols={len(chunk)}，rows={len(rows)}"
            )
        return {
            "status": "downloaded",
            "start_date": start,
            "end_date": end,
            "symbols": len(cleaned_symbols),
            "chunks": len(chunks),
            "completed_chunks": completed,
            "factor_rows": inserted,
            "database_path": str(database),
            **calendar_summary,
        }
    finally:
        if connection is not None:
            connection.close()
        if ad is not None and hasattr(ad, "logout"):
            try:
                ad.logout()
            except Exception:
                pass


def _float(value: Any) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0


def _quarantine_allowed(issue_symbols: set[str], total_symbols: int) -> bool:
    return bool(
        issue_symbols
        and total_symbols > 0
        and len(issue_symbols) <= MAX_QUARANTINE_SYMBOLS
        and len(issue_symbols) / total_symbols <= MAX_QUARANTINE_RATIO
    )


def _remove_quarantined_symbols(paths: list[Path], symbols: set[str]) -> int:
    removed = 0
    for path in paths:
        with path.open("r", encoding="utf-8-sig", newline="") as handle:
            rows = list(csv.DictReader(handle))
        kept = [row for row in rows if _symbol_key(row.get("symbol")) not in symbols]
        removed += len(rows) - len(kept)
        temporary = path.with_suffix(".tmp")
        with temporary.open("w", encoding="utf-8", newline="") as handle:
            writer = csv.DictWriter(handle, fieldnames=DAILY_FIELDS)
            writer.writeheader()
            writer.writerows(kept)
        temporary.replace(path)
    return removed


def build_forward_adjusted_daily(
    project: Path,
    *,
    start_date: str,
    end_date: str,
) -> dict[str, Any]:
    start = _date_key(start_date)
    end = _date_key(end_date)
    factor_dir = project / "data" / "processed" / "yinhe_adj_factor"
    database = factor_dir / "backward_factors.sqlite3"
    if not database.exists():
        raise ValueError(f"未找到银河复权因子数据库：{database}")
    raw_daily = project / "data" / "processed" / "yinhe_daily"
    output_daily = project / "data" / "processed" / "yinhe_daily_qfq"
    paths = [
        path
        for path in sorted(raw_daily.glob("20??????.csv"))
        if start <= path.stem <= end
    ]
    if not paths:
        raise ValueError(f"未找到待复权银河日线：{raw_daily}")
    output_daily.mkdir(parents=True, exist_ok=True)
    calendar_path = project / "data" / "processed" / "trade_calendar.csv"
    if not calendar_path.exists():
        raise ValueError(f"未找到银河官方交易日历：{calendar_path}")
    with calendar_path.open("r", encoding="utf-8-sig", newline="") as handle:
        open_dates = [
            _date_key(row.get("trade_date"))
            for row in csv.DictReader(handle)
            if str(row.get("is_open") or "1").lower() not in {"0", "false", "no"}
            and start <= _date_key(row.get("trade_date")) <= end
        ]
    date_index = {day: index for index, day in enumerate(open_dates)}
    connection = _connect(database)
    latest_factor = {
        symbol: factor
        for symbol, factor in connection.execute(
            """
            SELECT factors.symbol, factors.factor
            FROM backward_factors AS factors
            JOIN (
                SELECT symbol, MAX(trade_date) AS latest_date
                FROM backward_factors
                WHERE trade_date <= ?
                GROUP BY symbol
            ) AS latest
            ON factors.symbol = latest.symbol
            AND factors.trade_date = latest.latest_date
            """,
            (end,),
        )
    }
    previous: dict[str, tuple[str, float, float, float]] = {}
    factor_events = 0
    continuity_breaks = 0
    continuity_worsened_events = 0
    continuity_issues: list[dict[str, Any]] = []
    missing_factor_rows = 0
    adjusted_rows = 0

    try:
        for file_index, path in enumerate(paths, 1):
            factors = {
                symbol: factor
                for symbol, factor in connection.execute(
                    "SELECT symbol, factor FROM backward_factors WHERE trade_date = ?",
                    (path.stem,),
                )
            }
            with path.open("r", encoding="utf-8-sig", newline="") as handle:
                rows = list(csv.DictReader(handle))
            output_rows: list[dict[str, Any]] = []
            for row in rows:
                symbol = _symbol_key(row.get("symbol"))
                factor = factors.get(symbol)
                reference = latest_factor.get(symbol)
                if not factor or not reference:
                    missing_factor_rows += 1
                    continue
                multiplier = factor / reference
                adjusted = dict(row)
                adjusted["symbol"] = symbol
                for field in ("open", "high", "low", "close"):
                    adjusted[field] = round(_float(row.get(field)) * multiplier, 8)
                output_rows.append(adjusted)
                adjusted_rows += 1

                prior = previous.get(symbol)
                raw_close = _float(row.get("close"))
                adjusted_close = _float(adjusted["close"])
                if prior is not None:
                    prior_day, prior_raw, prior_adjusted, prior_factor = prior
                    consecutive = date_index.get(path.stem, -2) == date_index.get(prior_day, -1) + 1
                    factor_changed = abs(factor / prior_factor - 1) > 1e-10
                    if consecutive and factor_changed and prior_adjusted > 0:
                        factor_events += 1
                        raw_return = raw_close / prior_raw - 1 if prior_raw > 0 else None
                        adjusted_return = adjusted_close / prior_adjusted - 1
                        if abs(adjusted_return) > CONTINUITY_THRESHOLD:
                            continuity_breaks += 1
                            worsened = bool(
                                raw_return is not None
                                and abs(adjusted_return) > abs(raw_return) + 1e-8
                            )
                            continuity_worsened_events += int(worsened)
                            continuity_issues.append(
                                {
                                    "symbol": symbol,
                                    "previous_date": prior_day,
                                    "trade_date": path.stem,
                                    "previous_raw_close": prior_raw,
                                    "raw_close": raw_close,
                                    "previous_adjusted_close": prior_adjusted,
                                    "adjusted_close": adjusted_close,
                                    "previous_factor": prior_factor,
                                    "factor": factor,
                                    "factor_ratio": factor / prior_factor,
                                    "raw_return": raw_return,
                                    "adjusted_return": adjusted_return,
                                    "adjustment_worsened_jump": worsened,
                                }
                            )
                previous[symbol] = (path.stem, raw_close, adjusted_close, factor)

            output_path = output_daily / path.name
            temporary = output_path.with_suffix(".tmp")
            with temporary.open("w", encoding="utf-8", newline="") as handle:
                writer = csv.DictWriter(handle, fieldnames=DAILY_FIELDS)
                writer.writeheader()
                writer.writerows(output_rows)
            temporary.replace(output_path)
            if file_index % 100 == 0 or file_index == len(paths):
                print(
                    f"银河前复权日线：{file_index}/{len(paths)}，"
                    f"累计rows={adjusted_rows}，missing={missing_factor_rows}"
                )
    finally:
        connection.close()

    detected_continuity_breaks = continuity_breaks
    issue_symbols = {str(issue["symbol"]) for issue in continuity_issues}
    quarantine_applied = bool(
        continuity_issues
        and all(issue["adjustment_worsened_jump"] for issue in continuity_issues)
        and _quarantine_allowed(issue_symbols, len(latest_factor))
    )
    quarantined_rows = 0
    if quarantine_applied:
        output_paths = [output_daily / path.name for path in paths]
        quarantined_rows = _remove_quarantined_symbols(output_paths, issue_symbols)
        adjusted_rows -= quarantined_rows
        continuity_breaks = 0

    issue_path = factor_dir / "continuity_issues.json"
    issue_path.write_text(
        json.dumps(continuity_issues, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    manifest = {
        "schema_version": 1,
        "status": (
            "validated"
            if missing_factor_rows == 0 and detected_continuity_breaks == 0
            else (
                "validated_with_quarantine"
                if missing_factor_rows == 0 and quarantine_applied
                else "failed_validation"
            )
        ),
        "mode": "forward_adjusted_from_backward_factor",
        "coverage_start": paths[0].stem,
        "coverage_end": paths[-1].stem,
        "raw_prices_preserved": True,
        "source": "China Galaxy AmazingData BaseData.get_backward_factor",
        "factor_database": str(database),
        "output_directory": str(output_daily),
        "daily_files": len(paths),
        "adjusted_rows": adjusted_rows,
        "missing_factor_rows": missing_factor_rows,
        "factor_change_events": factor_events,
        "continuity_threshold": CONTINUITY_THRESHOLD,
        "continuity_breaks": continuity_breaks,
        "detected_continuity_breaks": detected_continuity_breaks,
        "continuity_worsened_events": continuity_worsened_events,
        "continuity_issues_path": str(issue_path),
        "continuity_issue_samples": continuity_issues[:20],
        "quarantine_applied": quarantine_applied,
        "quarantined_symbols": sorted(issue_symbols) if quarantine_applied else [],
        "quarantined_rows": quarantined_rows,
        "quarantine_max_ratio": MAX_QUARANTINE_RATIO,
        "quarantine_reason": (
            "factor-date alignment anomaly; symbol removed from adjusted research layer"
            if quarantine_applied
            else None
        ),
        "generated_at": datetime.now(UTC).isoformat(),
    }
    manifest_path = factor_dir / "manifest.json"
    manifest_path.write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    return {**manifest, "manifest_path": str(manifest_path)}


def sync_and_build_adjustment(
    project: Path,
    *,
    start_date: str,
    end_date: str,
    symbols: list[str],
    config: Any,
    chunk_size: int = 50,
    overwrite: bool = False,
) -> dict[str, Any]:
    download = sync_backward_factors(
        project,
        start_date=start_date,
        end_date=end_date,
        symbols=symbols,
        config=config,
        chunk_size=chunk_size,
        overwrite=overwrite,
    )
    build = build_forward_adjusted_daily(
        project,
        start_date=start_date,
        end_date=end_date,
    )
    return {"download": download, "build": build}

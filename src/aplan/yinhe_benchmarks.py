from __future__ import annotations

import csv
import hashlib
import json
import os
import re
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping


MARKET_INDICES = {
    "000001.SH": "SSE Composite",
    "000016.SH": "SSE 50",
    "000300.SH": "CSI 300",
    "000688.SH": "STAR 50",
    "000852.SH": "CSI 1000",
    "000905.SH": "CSI 500",
    "000906.SH": "CSI 800",
    "399001.SZ": "Shenzhen Component",
    "399006.SZ": "ChiNext",
}
MARKET_FIELDS = (
    "index_code", "index_name", "trade_date", "open", "high", "low", "close",
    "pre_close", "volume", "turnover",
)
INDUSTRY_MASTER_FIELDS = (
    "index_code", "industry_code", "level_type", "level1_name", "level2_name",
    "level3_name", "is_published", "change_reason",
)
INDUSTRY_CONSTITUENT_FIELDS = (
    "index_code", "index_name", "symbol", "in_date", "out_date",
)
INDUSTRY_DAILY_FIELDS = (
    "index_code", "trade_date", "open", "high", "low", "close", "pre_close",
    "volume", "turnover", "pe", "pb", "total_cap", "a_float_cap",
)


def _date(value: Any) -> str:
    if hasattr(value, "strftime"):
        return value.strftime("%Y%m%d")
    digits = "".join(character for character in str(value or "") if character.isdigit())
    return digits[:8] if len(digits) >= 8 else ""


def _field(row: Mapping[str, Any], *names: str) -> Any:
    upper = {str(key).upper(): value for key, value in row.items()}
    for name in names:
        value = upper.get(name.upper())
        if value not in (None, ""):
            return value
    return None


def _code(value: Any) -> str:
    match = re.search(r"(\d{6})(?:[._](SH|SZ|SI))?", str(value or "").upper())
    if not match:
        return ""
    return f"{match.group(1)}.{match.group(2)}" if match.group(2) else match.group(1)


def _symbol(value: Any) -> str:
    match = re.search(r"(?<!\d)(\d{6})(?!\d)", str(value or ""))
    return match.group(1) if match else ""


def _number(value: Any) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _records(value: Any, hint: str = "") -> list[dict[str, Any]]:
    if value is None:
        return []
    if hasattr(value, "reset_index") and hasattr(value, "to_dict"):
        output = []
        for row in value.reset_index().to_dict(orient="records"):
            item = dict(row)
            if hint and not _field(item, "INDEX_CODE", "CODE", "SECURITY_CODE"):
                item["INDEX_CODE"] = hint
            output.append(item)
        return output
    if isinstance(value, Mapping):
        row_fields = {
            "INDEX_CODE", "INDUSTRY_CODE", "CON_CODE", "TRADE_DATE", "KLINE_TIME",
        }
        if row_fields.intersection(str(key).upper() for key in value):
            return [dict(value)]
        output: list[dict[str, Any]] = []
        for key, nested in value.items():
            output.extend(_records(nested, _code(key) or hint))
        return output
    if isinstance(value, (list, tuple)):
        if (
            len(value) == 2
            and isinstance(value[0], Mapping)
            and isinstance(value[1], list)
        ):
            if value[1]:
                raise ValueError(f"银河基准接口返回错误：{value[1]}")
            return _records(value[0], hint)
        output = []
        for nested in value:
            output.extend(_records(nested, hint))
        return output
    return []


def _write(path: Path, fields: tuple[str, ...], rows: Iterable[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(".tmp")
    with temporary.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        writer.writerows(rows)
    temporary.replace(path)


def _read(path: Path) -> list[dict[str, str]]:
    if not path.exists():
        return []
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        return list(csv.DictReader(handle))


def _merge(
    existing: list[dict[str, Any]],
    incoming: list[dict[str, Any]],
    keys: tuple[str, ...],
) -> list[dict[str, Any]]:
    rows = {
        tuple(str(row.get(key) or "") for key in keys): row
        for row in existing
    }
    for row in incoming:
        rows[tuple(str(row.get(key) or "") for key in keys)] = row
    return [rows[key] for key in sorted(rows)]


def _missing_start(
    path: Path,
    start: str,
    end: str,
    expected_codes: Iterable[str],
) -> str | None:
    latest_by_code: dict[str, str] = {}
    for row in _read(path):
        code = str(row.get("index_code") or "")
        day = _date(row.get("trade_date"))
        if code and day and start <= day <= end:
            latest_by_code[code] = max(day, latest_by_code.get(code, ""))
    missing_starts = []
    for code in expected_codes:
        latest = latest_by_code.get(code)
        if not latest:
            missing_starts.append(start)
        elif latest < end:
            next_day = datetime.strptime(latest, "%Y%m%d") + timedelta(days=1)
            missing_starts.append(max(start, next_day.strftime("%Y%m%d")))
    return min(missing_starts) if missing_starts else None


def _coverage_by_code(
    rows: Iterable[Mapping[str, Any]],
    expected_codes: Iterable[str],
) -> dict[str, dict[str, Any]]:
    dates_by_code: dict[str, list[str]] = {
        code: [] for code in sorted(set(expected_codes))
    }
    for row in rows:
        code = str(row.get("index_code") or "")
        day = _date(row.get("trade_date"))
        if code in dates_by_code and day:
            dates_by_code[code].append(day)
    return {
        code: {
            "rows": len(dates),
            "first_date": min(dates) if dates else None,
            "last_date": max(dates) if dates else None,
        }
        for code, dates in dates_by_code.items()
    }


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _market_rows(value: Any, start: str, end: str) -> list[dict[str, Any]]:
    output = []
    for row in _records(value):
        code = _code(_field(row, "INDEX_CODE", "CODE", "SECURITY_CODE"))
        day = _date(_field(row, "TRADE_DATE", "KLINE_TIME", "DATE", "INDEX"))
        if code not in MARKET_INDICES or not start <= day <= end:
            continue
        output.append({
            "index_code": code,
            "index_name": MARKET_INDICES[code],
            "trade_date": day,
            "open": _number(_field(row, "OPEN", "OPEN_PRICE")),
            "high": _number(_field(row, "HIGH", "HIGH_PRICE")),
            "low": _number(_field(row, "LOW", "LOW_PRICE")),
            "close": _number(_field(row, "CLOSE", "CLOSE_PRICE")),
            "pre_close": _number(_field(row, "PRE_CLOSE", "PRE_CLOSE_PRICE")),
            "volume": _number(_field(row, "VOLUME", "VOLUME_TRADE")),
            "turnover": _number(_field(row, "AMOUNT", "VALUE_TRADE", "TURNOVER")),
        })
    return output


def _master_rows(value: Any) -> list[dict[str, Any]]:
    output = []
    for row in _records(value):
        code = _code(_field(row, "INDEX_CODE"))
        if str(_field(row, "LEVEL_TYPE") or "") != "1":
            continue
        if str(_field(row, "IS_PUB") or "") != "1" or not code:
            continue
        output.append({
            "index_code": code,
            "industry_code": str(_field(row, "INDUSTRY_CODE") or ""),
            "level_type": "1",
            "level1_name": str(_field(row, "LEVEL1_NAME") or ""),
            "level2_name": str(_field(row, "LEVEL2_NAME") or ""),
            "level3_name": str(_field(row, "LEVEL3_NAME") or ""),
            "is_published": "1",
            "change_reason": str(_field(row, "CHANGE_REASON") or ""),
        })
    return sorted(output, key=lambda row: row["index_code"])


def _constituent_rows(value: Any) -> list[dict[str, Any]]:
    output = []
    for row in _records(value):
        code = _code(_field(row, "INDEX_CODE"))
        symbol = _symbol(_field(row, "CON_CODE", "SECURITY_CODE"))
        in_date = _date(_field(row, "INDATE", "IN_DATE"))
        out_date = _date(_field(row, "OUTDATE", "OUT_DATE"))
        if code and symbol and in_date:
            output.append({
                "index_code": code,
                "index_name": str(_field(row, "INDEX_NAME") or ""),
                "symbol": symbol,
                "in_date": in_date,
                "out_date": out_date,
            })
    return output


def _daily_rows(value: Any, start: str, end: str) -> list[dict[str, Any]]:
    output = []
    for row in _records(value):
        code = _code(_field(row, "INDEX_CODE"))
        day = _date(_field(row, "TRADE_DATE", "DATE", "INDEX"))
        if not code or not start <= day <= end:
            continue
        output.append({
            "index_code": code,
            "trade_date": day,
            "open": _number(_field(row, "OPEN")),
            "high": _number(_field(row, "HIGH")),
            "low": _number(_field(row, "LOW")),
            "close": _number(_field(row, "CLOSE")),
            "pre_close": _number(_field(row, "PRE_CLOSE")),
            "volume": _number(_field(row, "VOLUME")),
            "turnover": _number(_field(row, "AMOUNT")),
            "pe": _number(_field(row, "PE")),
            "pb": _number(_field(row, "PB")),
            "total_cap": _number(_field(row, "TOTAL_CAP")),
            "a_float_cap": _number(_field(row, "A_FLOAT_CAP")),
        })
    return output


def sync_benchmarks(
    project: Path,
    *,
    start_date: str,
    end_date: str,
    config: Any | None = None,
    refresh_reference: bool = False,
    market_fetcher: Callable[[str, str], Any] | None = None,
    base_fetcher: Callable[[], Any] | None = None,
    constituent_fetcher: Callable[[list[str]], Any] | None = None,
    daily_fetcher: Callable[[list[str], str, str], Any] | None = None,
) -> dict[str, Any]:
    start, end = _date(start_date), _date(end_date)
    if not start or not end or start > end:
        raise ValueError("银河基准数据的开始和结束日期无效")
    output = project / "data" / "processed" / "benchmarks"
    market_path = output / "market_indices.csv"
    master_path = output / "industry_master.csv"
    constituent_path = output / "industry_constituents.csv"
    daily_path = output / "industry_daily.csv"
    manifest_path = output / "manifest.json"
    cache = project / "data" / "raw" / "yinhe" / "amazingdata_cache"
    cache.mkdir(parents=True, exist_ok=True)

    ad: Any | None = None
    if not all((market_fetcher, base_fetcher, constituent_fetcher, daily_fetcher)):
        if config is None:
            raise ValueError("银河基准同步缺少登录配置")
        import AmazingData as ad_module  # type: ignore[import-not-found]

        ad = ad_module
        if ad.login(
            username=config.username,
            password=config.password,
            host=config.server_vip,
            port=config.server_port,
        ) is False:
            raise ValueError("AmazingData 登录失败")
        base_data = ad.BaseData()
        info_data = ad.InfoData()
        market_data = ad.MarketData(base_data.get_calendar())
        cache_path = f"{cache.resolve()}{os.sep}"
        market_fetcher = lambda first, last: market_data.query_kline(
            list(MARKET_INDICES),
            begin_date=int(first),
            end_date=int(last),
            period=ad.constant.Period.day.value,
        )
        base_fetcher = lambda: info_data.get_industry_base_info(
            local_path=cache_path, is_local=False
        )
        constituent_fetcher = lambda codes: info_data.get_industry_constituent(
            codes, local_path=cache_path, is_local=False
        )
        daily_fetcher = lambda codes, first, last: info_data.get_industry_daily(
            codes, local_path=cache_path, is_local=False,
            begin_date=int(first), end_date=int(last),
        )

    try:
        assert market_fetcher and base_fetcher and constituent_fetcher and daily_fetcher
        market_start = _missing_start(market_path, start, end, MARKET_INDICES)
        market_new = (
            _market_rows(market_fetcher(market_start, end), market_start, end)
            if market_start else []
        )
        market_rows = _merge(
            _read(market_path), market_new, ("index_code", "trade_date")
        )
        _write(market_path, MARKET_FIELDS, market_rows)

        if refresh_reference or not master_path.exists():
            master_rows = _master_rows(base_fetcher())
            _write(master_path, INDUSTRY_MASTER_FIELDS, master_rows)
        else:
            master_rows = _read(master_path)
        industry_codes = sorted({row["index_code"] for row in master_rows})
        if not industry_codes:
            raise ValueError("银河未返回已发布的一级行业指数")

        if refresh_reference or not constituent_path.exists():
            constituents = _merge(
                [], _constituent_rows(constituent_fetcher(industry_codes)),
                ("index_code", "symbol", "in_date"),
            )
            _write(constituent_path, INDUSTRY_CONSTITUENT_FIELDS, constituents)
        else:
            constituents = _read(constituent_path)

        industry_start = _missing_start(
            daily_path, start, end, industry_codes
        )
        daily_new = (
            _daily_rows(
                daily_fetcher(industry_codes, industry_start, end),
                industry_start, end,
            )
            if industry_start else []
        )
        daily_rows = _merge(
            _read(daily_path), daily_new, ("index_code", "trade_date")
        )
        _write(daily_path, INDUSTRY_DAILY_FIELDS, daily_rows)

        market_codes = {row["index_code"] for row in market_rows}
        daily_codes = {row["index_code"] for row in daily_rows}
        market_coverage = _coverage_by_code(market_rows, MARKET_INDICES)
        industry_coverage = _coverage_by_code(daily_rows, industry_codes)
        invalid_intervals = sum(
            bool(row.get("out_date"))
            and _date(row["out_date"]) < _date(row.get("in_date"))
            for row in constituents
        )
        status = (
            "validated"
            if set(MARKET_INDICES).issubset(market_codes)
            and set(industry_codes).issubset(daily_codes)
            and constituents
            and not invalid_intervals
            else "failed_validation"
        )
        manifest = {
            "schema_version": 1,
            "status": status,
            "provider": "China Galaxy AmazingData",
            "coverage_start": start,
            "coverage_end": end,
            "market_index_count": len(market_codes),
            "market_rows": len(market_rows),
            "market_coverage_by_index": market_coverage,
            "industry_level1_count": len(industry_codes),
            "industry_constituent_rows": len(constituents),
            "industry_daily_rows": len(daily_rows),
            "industry_coverage_by_index": industry_coverage,
            "point_in_time_constituents": bool(constituents and not invalid_intervals),
            "invalid_constituent_intervals": invalid_intervals,
            "daily_weight_downloaded": False,
            "daily_weight_note": (
                "Equal-weight industry benchmarks use constituent in/out dates and "
                "stock daily prices; daily constituent weights are intentionally omitted."
            ),
            "paths": {
                "market_indices": str(market_path),
                "industry_master": str(master_path),
                "industry_constituents": str(constituent_path),
                "industry_daily": str(daily_path),
            },
            "hashes": {
                path.name: _sha256(path)
                for path in (market_path, master_path, constituent_path, daily_path)
            },
            "generated_at": datetime.now(UTC).isoformat(),
        }
        manifest_path.write_text(
            json.dumps(manifest, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        return {**manifest, "manifest_path": str(manifest_path)}
    finally:
        if ad is not None and hasattr(ad, "logout"):
            try:
                ad.logout()
            except Exception:
                pass

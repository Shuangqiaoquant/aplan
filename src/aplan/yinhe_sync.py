from __future__ import annotations

import argparse
import csv
import hashlib
import json
import math
import multiprocessing
import os
import queue
import time
from collections.abc import Mapping
from dataclasses import asdict, dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Callable


class YinheUnavailable(RuntimeError):
    pass


class YinheUpstreamError(RuntimeError):
    pass


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

SECURITY_FIELDS = (
    "symbol",
    "name",
    "list_date",
    "industry",
    "is_st",
    "is_delisting_risk",
    "market",
    "security_type",
    "security_status",
)

SHANGHAI_A_SHARE_PREFIXES = ("600", "601", "603", "605", "688", "689")
SHENZHEN_A_SHARE_PREFIXES = ("000", "001", "002", "003", "300", "301")
A_SHARE_PREFIXES = SHANGHAI_A_SHARE_PREFIXES + SHENZHEN_A_SHARE_PREFIXES
A_SHARE_SECURITY_TYPES = {"02001", "02003", "02004", "02009"}

SNAPSHOT_FIELDS = (
    "symbol",
    "trade_date",
    "orig_time",
    "pre_close",
    "open",
    "high",
    "low",
    "last",
    "close",
    "volume",
    "turnover",
    "trading_phase_code",
)

MARKET_ALIASES = {
    "sh": "kSSE",
    "shse": "kSSE",
    "sse": "kSSE",
    "sz": "kSZSE",
    "szse": "kSZSE",
}

KLINE_ALIASES = {
    "1": "k1KLine",
    "1m": "k1KLine",
    "3": "k3KLine",
    "3m": "k3KLine",
    "5": "k5KLine",
    "5m": "k5KLine",
    "10": "k10KLine",
    "10m": "k10KLine",
    "15": "k15KLine",
    "15m": "k15KLine",
    "30": "k30KLine",
    "30m": "k30KLine",
    "60": "k60KLine",
    "60m": "k60KLine",
    "120": "k120KLine",
    "120m": "k120KLine",
    "day": "kDayKline",
    "daily": "kDayKline",
    "d": "kDayKline",
    "week": "kWeekKline",
    "weekly": "kWeekKline",
    "month": "kMonthKline",
    "monthly": "kMonthKline",
    "season": "kSeasonKline",
    "quarter": "kSeasonKline",
    "year": "kYearKline",
    "yearly": "kYearKline",
}

SNAPSHOT_DATA_TYPE_ALIASES = {
    "snapshot": "kSnapshot",
    "stock": "kSnapshot",
    "stock_snapshot": "kSnapshot",
    "l1": "kSnapshot",
    "snapshot_l1": "kSnapshot",
    "l2": "kSnapshotL2",
    "snapshot_l2": "kSnapshotL2",
    "index": "kIndexSnapshot",
    "index_snapshot": "kIndexSnapshot",
}


def _write_csv(path: Path, rows: list[dict[str, Any]], fields: tuple[str, ...]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)


def _write_snapshot(path: Path, api_name: str, rows: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    document = {
        "api_name": api_name,
        "downloaded_at": datetime.now(UTC).isoformat(),
        "row_count": len(rows),
        "rows": rows,
    }
    path.write_text(json.dumps(document, ensure_ascii=False, indent=2, default=str), encoding="utf-8")


def _number(value: Any) -> float:
    if value in (None, "", "-", "None", "nan"):
        return 0.0
    try:
        number = float(value)
    except (TypeError, ValueError):
        return 0.0
    return number if math.isfinite(number) else 0.0


def _daily_turnover(row: dict[str, Any]) -> float:
    value_trade = row.get("value_trade")
    if value_trade not in (None, ""):
        return _number(value_trade) * 1_000
    return _number(_first_value(row, "成交额", "turnover", "amount"))


def _rows(dataframe: Any) -> list[dict[str, Any]]:
    if dataframe is None:
        return []
    if hasattr(dataframe, "to_dict"):
        return list(dataframe.to_dict(orient="records"))
    return list(dataframe)


def _strip_suffix(code: str) -> str:
    value = str(code or "").strip()
    if "." in value:
        left, right = value.split(".", 1)
        value = left if left.isdigit() else right
    return value.zfill(6)


def _format_amazing_data_code(symbol: str, code_format: str = "suffix") -> str:
    raw = str(symbol or "").strip()
    if "." in raw:
        left, right = raw.split(".", 1)
        if left.isdigit():
            return f"{left.zfill(6)}.{right.upper()}"
        return f"{right.zfill(6)}.{left.upper()}"
    cleaned = _strip_suffix(raw)
    if len(cleaned) != 6 or not cleaned.isdigit():
        raise ValueError(f"股票代码必须是六位数字：{symbol}")
    market = "SH" if _infer_market(cleaned) == "sse" else "SZ"
    fmt = code_format.strip().lower()
    if fmt == "raw":
        return cleaned
    if fmt == "prefix":
        return f"{market}.{cleaned}"
    if fmt == "suffix":
        return f"{cleaned}.{market}"
    raise ValueError(f"未知 AmazingData 代码格式：{code_format}")


def _read_symbols(symbols: str | None, symbols_file: str | None = None) -> list[str]:
    values: list[str] = []
    if symbols:
        values.extend(part.strip() for part in symbols.split(","))
    if symbols_file:
        for line in Path(symbols_file).read_text(encoding="utf-8").splitlines():
            values.extend(part.strip() for part in line.split(","))
    output: list[str] = []
    seen: set[str] = set()
    for value in values:
        symbol = _strip_suffix(value)
        if len(symbol) == 6 and symbol.isdigit() and symbol not in seen:
            seen.add(symbol)
            output.append(symbol)
    return output


def _weekdays_between(start_date: str, end_date: str) -> list[str]:
    start = datetime.strptime(str(start_date).replace("-", ""), "%Y%m%d").date()
    end = datetime.strptime(str(end_date).replace("-", ""), "%Y%m%d").date()
    dates: list[str] = []
    current = start
    while current <= end:
        if current.weekday() < 5:
            dates.append(current.strftime("%Y%m%d"))
        current += timedelta(days=1)
    return dates


def _infer_market(symbol: str) -> str:
    cleaned = _strip_suffix(symbol)
    if cleaned.startswith(("5", "6", "9")):
        return "sse"
    return "szse"


def _market_value(tgw: Any, market: str | int) -> int:
    if isinstance(market, int):
        return market
    attr = MARKET_ALIASES.get(str(market).strip().lower())
    if not attr:
        raise ValueError(f"未知银河市场：{market}")
    return int(getattr(tgw.MarketType, attr))


def _kline_value(tgw: Any, interval: str | int) -> int:
    if isinstance(interval, int):
        return interval
    attr = KLINE_ALIASES.get(str(interval).strip().lower())
    if not attr:
        raise ValueError(f"未知银河 K 线周期：{interval}")
    return int(getattr(tgw.MDDatatype, attr))


def _snapshot_data_type_value(tgw: Any, data_type: str | int) -> int:
    if isinstance(data_type, int):
        return data_type
    key = str(data_type).strip().lower().replace("-", "_")
    if key.isdigit():
        return int(key)
    attr = SNAPSHOT_DATA_TYPE_ALIASES.get(key, data_type)
    subscribe_data_type = getattr(tgw, "SubscribeDataType", None)
    if subscribe_data_type is not None and hasattr(subscribe_data_type, attr):
        return int(getattr(subscribe_data_type, attr))
    if hasattr(tgw.MDDatatype, attr):
        return int(getattr(tgw.MDDatatype, attr))
    raise ValueError(f"银河 SDK 不支持快照数据类型：{data_type}")


def _parse_date(value: Any) -> str:
    if value is None:
        return "1900-01-01"
    if isinstance(value, datetime):
        return value.date().isoformat()
    text = str(value).strip()
    if not text:
        return "1900-01-01"
    for formatter in ("%Y-%m-%d", "%Y%m%d", "%Y/%m/%d"):
        try:
            return datetime.strptime(text[:10], formatter).date().isoformat()
        except ValueError:
            continue
    try:
        return datetime.fromisoformat(text).date().isoformat()
    except ValueError:
        return "1900-01-01"


def _date_key(value: Any) -> str:
    if value in (None, ""):
        return ""
    if isinstance(value, datetime):
        return value.strftime("%Y%m%d")
    text = str(value).strip()
    for formatter, width in (("%Y-%m-%d %H:%M:%S", 19), ("%Y-%m-%d", 10), ("%Y%m%d", 8)):
        try:
            return datetime.strptime(text[:width], formatter).strftime("%Y%m%d")
        except ValueError:
            continue
    if len(text) >= 8 and text[:8].isdigit():
        return text[:8]
    return ""


def _time_key(value: Any) -> str:
    if value in (None, ""):
        return ""
    if isinstance(value, datetime):
        return value.strftime("%H%M%S%f")[:9].lstrip("0") or "0"
    text = str(value).strip()
    if " " in text:
        text = text.split(" ", 1)[1]
    if ":" in text:
        try:
            parsed = datetime.strptime(text[:15], "%H:%M:%S.%f")
        except ValueError:
            try:
                parsed = datetime.strptime(text[:8], "%H:%M:%S")
            except ValueError:
                return ""
        return parsed.strftime("%H%M%S%f")[:9].lstrip("0") or "0"
    return ""


def _first_value(row: dict[str, Any], *names: str) -> Any:
    for name in names:
        if name in row and row[name] not in (None, ""):
            return row[name]
    lowered = {str(key).lower().replace(" ", ""): value for key, value in row.items()}
    for name in names:
        key = str(name).lower().replace(" ", "")
        for candidate, value in lowered.items():
            if key == candidate or key in candidate:
                if value not in (None, ""):
                    return value
    return None


def _observed_at(as_of: str | None) -> datetime:
    if not as_of:
        return datetime.now(UTC)
    observed = datetime.fromisoformat(as_of)
    if observed.tzinfo is None:
        return observed.replace(tzinfo=UTC)
    return observed.astimezone(UTC)


def normalize_security_rows(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    selected: dict[str, dict[str, Any]] = {}
    for row in rows:
        symbol = _strip_suffix(
            _first_value(
                row,
                "证券代码",
                "A股代码",
                "code",
                "security_code",
                "symbol",
            )
        )
        if len(symbol) != 6 or not symbol.isdigit():
            continue
        name = str(
            _first_value(row, "证券简称", "A股简称", "name", "security_name", "symbol") or ""
        ).strip()
        if not name:
            continue
        market = str(_first_value(row, "_query_market", "market_type", "market") or "").strip()
        item = {
            "symbol": symbol,
            "name": name,
            "list_date": _parse_date(_first_value(row, "上市日期", "A股上市日期", "list_date")),
            "industry": str(_first_value(row, "所属行业", "industry") or "未知").strip() or "未知",
            "is_st": "1" if "ST" in name.upper() else "0",
            "is_delisting_risk": "1" if "退" in name else "0",
            "market": market,
            "security_type": str(
                _first_value(row, "证券类型", "security_type", "securityType") or ""
            ).strip(),
            "security_status": str(
                _first_value(row, "证券状态", "security_status", "securityStatus") or ""
            ).strip(),
        }
        previous = selected.get(symbol)
        expected_market = _infer_market(symbol)
        item_score = (
            item["security_type"] in A_SHARE_SECURITY_TYPES,
            market == expected_market,
        )
        previous_score = (
            previous is not None and previous.get("security_type") in A_SHARE_SECURITY_TYPES,
            previous is not None and previous.get("market") == expected_market,
        )
        if previous is None or item_score > previous_score:
            selected[symbol] = item
    return sorted(selected.values(), key=lambda item: item["symbol"])


def build_symbol_pool(
    root: Path,
    *,
    securities_path: Path | None = None,
    output_path: Path | None = None,
    include_st: bool = False,
    include_delisting: bool = False,
) -> dict[str, Any]:
    source = securities_path or root / "data" / "processed" / "yinhe_securities.csv"
    destination = output_path or root / "data" / "processed" / "yinhe_symbols.txt"
    if not source.exists():
        raise ValueError(f"未找到银河证券清单：{source}；请先运行 aplan-yinhe securities")

    selected: list[str] = []
    total_rows = 0
    with source.open("r", encoding="utf-8-sig", newline="") as handle:
        reader = csv.DictReader(handle)
        if not reader.fieldnames or "security_type" not in reader.fieldnames:
            raise ValueError("银河证券清单缺少 security_type；请先重新运行 aplan-yinhe securities")
        for row in reader:
            total_rows += 1
            symbol = _strip_suffix(row.get("symbol", ""))
            if len(symbol) != 6 or not symbol.isdigit() or not symbol.startswith(A_SHARE_PREFIXES):
                continue
            if str(row.get("security_type", "")).strip() not in A_SHARE_SECURITY_TYPES:
                continue
            if not include_st and str(row.get("is_st", "0")).strip().lower() in {"1", "true", "yes"}:
                continue
            if not include_delisting and str(row.get("is_delisting_risk", "0")).strip().lower() in {
                "1",
                "true",
                "yes",
            }:
                continue
            selected.append(symbol)

    symbols = sorted(set(selected))
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text("".join(f"{symbol}\n" for symbol in symbols), encoding="utf-8")
    return {
        "source_rows": total_rows,
        "symbols": len(symbols),
        "excluded_rows": total_rows - len(symbols),
        "output_path": str(destination),
        "include_st": include_st,
        "include_delisting": include_delisting,
    }


def _daily_coverage(
    root: Path,
    trade_date: str,
    symbols: list[str],
    daily: list[dict[str, Any]],
) -> dict[str, Any]:
    requested = sorted(set(symbols))
    observed = {
        _strip_suffix(row.get("symbol", ""))
        for row in daily
        if len(_strip_suffix(row.get("symbol", ""))) == 6
    }
    missing = [symbol for symbol in requested if symbol not in observed]
    missing_path = root / "data" / "processed" / "yinhe_daily_missing" / f"{trade_date}.txt"
    missing_path.parent.mkdir(parents=True, exist_ok=True)
    missing_path.write_text("".join(f"{symbol}\n" for symbol in missing), encoding="utf-8")

    requested_by_prefix: dict[str, int] = {}
    missing_by_prefix: dict[str, int] = {}
    for symbol in requested:
        prefix = symbol[:3]
        requested_by_prefix[prefix] = requested_by_prefix.get(prefix, 0) + 1
    for symbol in missing:
        prefix = symbol[:3]
        missing_by_prefix[prefix] = missing_by_prefix.get(prefix, 0) + 1

    requested_count = len(requested)
    observed_count = len(set(requested) & observed)
    return {
        "requested_symbols": requested_count,
        "observed_symbols": observed_count,
        "missing_symbols": len(missing),
        "coverage_rate": round(observed_count / requested_count, 6) if requested_count else 0.0,
        "missing_path": str(missing_path),
        "requested_by_prefix": requested_by_prefix,
        "missing_by_prefix": missing_by_prefix,
    }


def audit_daily_coverage(root: Path, trade_date: str, symbols: list[str]) -> dict[str, Any]:
    daily_path = root / "data" / "processed" / "yinhe_daily" / f"{trade_date}.csv"
    if not daily_path.exists():
        raise ValueError(f"未找到银河日线文件：{daily_path}")
    with daily_path.open("r", encoding="utf-8-sig", newline="") as handle:
        daily = list(csv.DictReader(handle))
    requested = set(symbols)
    observed = {
        _strip_suffix(row.get("symbol", ""))
        for row in daily
        if len(_strip_suffix(row.get("symbol", ""))) == 6
    }
    outside = sorted(observed - requested)
    result = {
        "trade_date": trade_date,
        "daily_rows": len(daily),
        "daily_path": str(daily_path),
        "observed_outside_pool": len(outside),
    }
    result.update(_daily_coverage(root, trade_date, symbols, daily))

    securities_path = root / "data" / "processed" / "yinhe_securities.csv"
    if outside and securities_path.exists():
        with securities_path.open("r", encoding="utf-8-sig", newline="") as handle:
            security_rows = {
                _strip_suffix(row.get("symbol", "")): row
                for row in csv.DictReader(handle)
            }
        outside_by_type: dict[str, int] = {}
        outside_samples: list[dict[str, str]] = []
        for symbol in outside:
            security = security_rows.get(symbol, {})
            security_type = str(security.get("security_type", "") or "unknown")
            outside_by_type[security_type] = outside_by_type.get(security_type, 0) + 1
            if len(outside_samples) < 20:
                outside_samples.append(
                    {
                        "symbol": symbol,
                        "name": str(security.get("name", "")),
                        "market": str(security.get("market", "")),
                        "security_type": security_type,
                        "security_status": str(security.get("security_status", "")),
                    }
                )
        result["outside_by_security_type"] = outside_by_type
        result["outside_samples"] = outside_samples
    return result


def normalize_daily_rows(rows: list[dict[str, Any]], trade_date: str) -> list[dict[str, Any]]:
    output: list[dict[str, Any]] = []
    fallback_date = _date_key(trade_date)
    for row in rows:
        symbol = _strip_suffix(_first_value(row, "证券代码", "security_code", "symbol", "code"))
        if len(symbol) != 6 or not symbol.isdigit():
            continue
        row_date = _date_key(
            _first_value(
                row,
                "交易日期",
                "trade_date",
                "date",
                "kline_time",
                "orig_time",
                "K线时间",
            )
        ) or fallback_date
        output.append(
            {
                "symbol": symbol,
                "trade_date": row_date,
                "open": _number(_first_value(row, "开盘价", "open", "open_price")),
                "high": _number(_first_value(row, "最高价", "high", "high_price")),
                "low": _number(_first_value(row, "最低价", "low", "low_price")),
                "close": _number(_first_value(row, "收盘价", "close", "close_price")),
                "volume": _number(_first_value(row, "成交量", "volume", "volume_trade")),
                "turnover": _daily_turnover(row),
                "is_suspended": "1"
                if str(_first_value(row, "是否停牌", "is_suspended", "trading_status") or "").strip()
                in {"1", "true", "True", "停牌"}
                else "0",
                "is_limit_up": "1"
                if str(_first_value(row, "涨停", "is_limit_up") or "").strip() in {"1", "true", "True"}
                else "0",
                "is_limit_down": "1"
                if str(_first_value(row, "跌停", "is_limit_down") or "").strip() in {"1", "true", "True"}
                else "0",
            }
        )
    return sorted(output, key=lambda item: (item["trade_date"], item["symbol"]))


def normalize_snapshot_rows(rows: list[dict[str, Any]], trade_date: str) -> list[dict[str, Any]]:
    output: list[dict[str, Any]] = []
    for row in rows:
        symbol = _strip_suffix(_first_value(row, "证券代码", "security_code", "symbol", "code"))
        if len(symbol) != 6 or not symbol.isdigit():
            continue
        trade_time = _first_value(row, "trade_time", "datetime", "timestamp")
        output.append(
            {
                "symbol": symbol,
                "trade_date": str(
                    _first_value(row, "交易日期", "trade_date", "date") or _date_key(trade_time) or trade_date
                ).replace("-", ""),
                "orig_time": str(_first_value(row, "orig_time", "时间", "行情时间") or _time_key(trade_time) or ""),
                "pre_close": _number(_first_value(row, "pre_close_price", "昨收价", "pre_close")),
                "open": _number(_first_value(row, "open_price", "开盘价", "open")),
                "high": _number(_first_value(row, "high_price", "最高价", "high")),
                "low": _number(_first_value(row, "low_price", "最低价", "low")),
                "last": _number(_first_value(row, "last_price", "最新价", "last")),
                "close": _number(_first_value(row, "close_price", "收盘价", "close")),
                "volume": _number(_first_value(row, "total_volume_trade", "成交量", "volume")),
                "turnover": _number(_first_value(row, "total_value_trade", "成交额", "turnover", "amount")),
                "trading_phase_code": str(_first_value(row, "trading_phase_code", "交易阶段") or ""),
            }
        )
    return sorted(output, key=lambda item: item["symbol"])


def build_kline_request(
    tgw: Any,
    symbol: str,
    *,
    begin_date: str | int,
    end_date: str | int,
    market: str | int | None = None,
    interval: str | int = "day",
    begin_time: int = 0,
    end_time: int = 0,
    cq_flag: int = 0,
    cq_date: int = 0,
    qj_flag: int = 0,
    auto_complete: int = 1,
    cyc_def: int = 0,
) -> Any:
    req = tgw.ReqKline()
    cleaned = _strip_suffix(symbol)
    if len(cleaned) != 6 or not cleaned.isdigit():
        raise ValueError(f"股票代码必须是六位数字：{symbol}")
    req.security_code = cleaned
    req.market_type = _market_value(tgw, market or _infer_market(cleaned))
    req.cq_flag = cq_flag
    req.cq_date = int(cq_date)
    req.qj_flag = qj_flag
    req.cyc_type = _kline_value(tgw, interval)
    req.cyc_def = int(cyc_def)
    req.auto_complete = int(auto_complete)
    req.begin_date = int(str(begin_date).replace("-", ""))
    req.end_date = int(str(end_date).replace("-", ""))
    req.begin_time = int(begin_time)
    req.end_time = int(end_time)
    return req


def build_snapshot_request(
    tgw: Any,
    symbol: str,
    *,
    trade_date: str | int,
    market: str | int | None = None,
    begin_time: int = 0,
    end_time: int = 0,
    level_type: int = 1,
    data_type: str | int = "snapshot",
) -> Any:
    req = tgw.ReqDefault()
    cleaned = _strip_suffix(symbol)
    if len(cleaned) != 6 or not cleaned.isdigit():
        raise ValueError(f"股票代码必须是六位数字：{symbol}")
    req.security_code = cleaned
    req.market_type = _market_value(tgw, market or _infer_market(cleaned))
    req.date = int(str(trade_date).replace("-", ""))
    req.begin_time = int(begin_time)
    req.end_time = int(end_time)
    req.data_type = _snapshot_data_type_value(tgw, data_type)
    req.level_type = int(level_type)
    return req


def build_security_info_request(tgw: Any, market: str | int, symbol: str = "") -> Any:
    req = tgw.SubCodeTableItem()
    req.market = _market_value(tgw, market)
    req.security_code = _strip_suffix(symbol) if symbol else ""
    return req


def _ensure_supported_sdk() -> Any:
    try:
        import tgw  # type: ignore[import-not-found]
    except Exception as exc:  # noqa: BLE001 - SDK 在不支持的平台上会直接抛异常
        raise YinheUnavailable(
            "未能导入银河 `tgw` SDK；该包仅支持 Linux/Windows x64 的 Python 3.6/3.8-3.14。"
        ) from exc
    return tgw


def _ensure_amazing_data_sdk() -> Any:
    try:
        import AmazingData as ad  # type: ignore[import-not-found]
    except Exception as exc:  # noqa: BLE001 - SDK 在不支持的平台上会直接抛异常
        raise YinheUnavailable(
            "未能导入银河 `AmazingData` SDK；请先安装 data/raw/yinhe/星耀数智/AmazingData/AmazingData-*.whl"
        ) from exc
    return ad


def _amazing_data_code_hint(value: Any) -> str | None:
    text = str(value or "").strip()
    cleaned = _strip_suffix(text)
    return text if len(cleaned) == 6 and cleaned.isdigit() else None


def _amazing_data_row(row: Any) -> dict[str, Any]:
    if isinstance(row, Mapping):
        return dict(row)
    if hasattr(row, "_asdict"):
        value = row._asdict()
        if isinstance(value, Mapping):
            return dict(value)
    if hasattr(row, "__dict__"):
        value = {
            key: item
            for key, item in vars(row).items()
            if not str(key).startswith("_")
        }
        if value:
            return value
    raise YinheUpstreamError(
        "AmazingData 快照行结构无法解析："
        f"type={type(row).__name__}；请确认云端使用 AmazingData 1.1.7 兼容版适配器"
    )


def _looks_like_amazing_data_snapshot_row(value: Mapping[Any, Any]) -> bool:
    fields = {str(key).strip().lower() for key in value}
    return bool(
        fields
        & {
            "code",
            "security_code",
            "symbol",
            "证券代码",
            "trade_time",
            "交易时间",
            "last",
            "last_price",
            "最新价",
            "pre_close",
            "昨收价",
        }
    )


def _flatten_amazing_data_result(snapshot_result: Any) -> list[dict[str, Any]]:
    # AmazingData 1.1.7 returns [data, errors], where data is commonly
    # {trade_date: {code: DataFrame}}. Older releases returned {code: DataFrame}.
    # Accept both shapes, but never silently ignore an upstream error list.
    snapshot_dict = snapshot_result
    if isinstance(snapshot_result, (list, tuple)) and len(snapshot_result) == 2:
        candidate, errors = snapshot_result
        if isinstance(candidate, Mapping):
            if errors:
                error_text = str(errors)
                if len(error_text) > 500:
                    error_text = f"{error_text[:497]}..."
                raise YinheUpstreamError(f"AmazingData 历史快照返回错误：{error_text}")
            snapshot_dict = candidate

    rows: list[dict[str, Any]] = []
    def visit(value: Any, code_hint: str | None = None) -> None:
        if value is None:
            return
        if hasattr(value, "reset_index") and hasattr(value, "to_dict"):
            frame = value.reset_index()
            frame_rows = frame.to_dict(orient="records")
            for row in frame_rows:
                item = _amazing_data_row(row)
                if code_hint and not _first_value(
                    item, "code", "security_code", "symbol", "证券代码"
                ):
                    item["code"] = code_hint
                rows.append(item)
            return
        if isinstance(value, Mapping) and _looks_like_amazing_data_snapshot_row(value):
            item = dict(value)
            if code_hint and not _first_value(
                item, "code", "security_code", "symbol", "证券代码"
            ):
                item["code"] = code_hint
            rows.append(item)
            return
        if isinstance(value, Mapping):
            for key, nested in value.items():
                visit(nested, _amazing_data_code_hint(key) or code_hint)
            return
        if isinstance(value, (list, tuple)):
            for row in value:
                visit(row, code_hint)
            return
        item = _amazing_data_row(value)
        if code_hint and not _first_value(
            item, "code", "security_code", "symbol", "证券代码"
        ):
            item["code"] = code_hint
        rows.append(item)

    visit(snapshot_dict)
    if snapshot_dict is not None and not rows:
        raise YinheUpstreamError(
            "AmazingData 历史快照返回为空；请确认交易日、代码和历史快照权限"
        )
    for item in rows:
        if not _first_value(item, "code", "security_code", "symbol", "证券代码"):
            raise YinheUpstreamError(
                "AmazingData 历史快照记录缺少证券代码，无法安全标准化"
            )
    return rows


def _fetch_amazing_data_snapshots_direct(
    config: YinheConfig,
    symbols: list[str],
    trade_date: str,
    *,
    begin_time: int = 0,
    end_time: int = 0,
    code_format: str = "suffix",
) -> list[dict[str, Any]]:
    ad = _ensure_amazing_data_sdk()
    login_result = ad.login(
        username=config.username,
        password=config.password,
        host=config.server_vip,
        port=config.server_port,
    )
    if login_result is False:
        raise YinheUpstreamError("AmazingData 登录失败，请检查账号、密码、IP、端口和账号权限")
    code_list = [_format_amazing_data_code(symbol, code_format) for symbol in symbols]
    trade_date_int = int(str(trade_date).replace("-", ""))
    try:
        base_data = ad.BaseData()
        calendar = base_data.get_calendar()
    except Exception:
        calendar = None
    if not calendar:
        calendar = [trade_date_int]
    market_data = ad.MarketData(calendar)
    kwargs: dict[str, Any] = {
        "begin_date": trade_date_int,
        "end_date": trade_date_int,
    }
    if begin_time:
        kwargs["begin_time"] = int(begin_time)
    if end_time:
        kwargs["end_time"] = int(end_time)
    try:
        result = market_data.query_snapshot(code_list, **kwargs)
    except Exception as exc:  # noqa: BLE001 - 供应商 SDK 抛出的异常类型不稳定
        raise YinheUpstreamError(f"AmazingData 快照查询失败：{exc}") from exc
    return _flatten_amazing_data_result(result)


def _fetch_amazing_data_snapshots_worker(
    result_queue: multiprocessing.Queue,
    config_values: dict[str, Any],
    symbols: list[str],
    trade_date: str,
    begin_time: int,
    end_time: int,
    code_format: str,
) -> None:
    try:
        rows = _fetch_amazing_data_snapshots_direct(
            YinheConfig(**config_values),
            symbols,
            trade_date,
            begin_time=begin_time,
            end_time=end_time,
            code_format=code_format,
        )
    except Exception as exc:  # noqa: BLE001 - 子进程需要把供应商异常序列化回主进程
        result_queue.put({"ok": False, "error": str(exc)})
    else:
        result_queue.put({"ok": True, "rows": rows})


@dataclass(slots=True)
class YinheConfig:
    server_vip: str
    server_port: int
    username: str
    password: str
    api_mode: str = "internet"
    path: str = ""
    force_logout: bool = True

    @classmethod
    def from_env(cls, env_path: str | Path = ".env") -> "YinheConfig":
        load_env(env_path)
        server_vip = os.environ.get("YINHE_SERVER_VIP", "").strip()
        server_port = int(os.environ.get("YINHE_SERVER_PORT", "0").strip() or 0)
        username = os.environ.get("YINHE_USERNAME", "").strip()
        password = os.environ.get("YINHE_PASSWORD", "").strip()
        api_mode = os.environ.get("YINHE_API_MODE", "internet").strip() or "internet"
        path = os.environ.get("YINHE_PATH", "").strip()
        force_logout = os.environ.get("YINHE_FORCE_LOGOUT", "1").strip().lower() not in {"0", "false", "no"}
        if not server_vip or not server_port or not username or not password:
            raise YinheUnavailable(
                "未找到银河连接参数，请配置 YINHE_SERVER_VIP / YINHE_SERVER_PORT / YINHE_USERNAME / YINHE_PASSWORD"
            )
        return cls(
            server_vip=server_vip,
            server_port=server_port,
            username=username,
            password=password,
            api_mode=api_mode,
            path=path,
            force_logout=force_logout,
        )


def load_env(path: str | Path = ".env") -> None:
    env_path = Path(path)
    if not env_path.exists():
        return
    for raw_line in env_path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip().strip("'\""))


class YinheClient:
    def __init__(self, config: YinheConfig) -> None:
        self.config = config
        self._tgw: Any | None = None

    def _sdk(self) -> Any:
        if self._tgw is None:
            self._tgw = _ensure_supported_sdk()
        return self._tgw

    def login(self) -> bool:
        tgw = self._sdk()
        cfg = tgw.Cfg()
        cfg.server_vip = self.config.server_vip
        cfg.server_port = self.config.server_port
        cfg.username = self.config.username
        cfg.password = self.config.password
        cfg.force_logout = bool(self.config.force_logout)
        mode = (
            tgw.ApiMode.kColocationMode
            if self.config.api_mode.lower() in {"colocation", "co", "colo"}
            else tgw.ApiMode.kInternetMode
        )
        return bool(tgw.Login(cfg, mode, self.config.path))

    def close(self) -> None:
        if self._tgw is not None:
            try:
                self._tgw.Close()
            finally:
                self._tgw = None

    def query_kline(self, request: Any) -> list[dict[str, Any]]:
        tgw = self._sdk()
        result, err = tgw.QueryKline(request, return_df_format=False)
        if err:
            raise YinheUpstreamError(f"银河 K 线查询失败：{tgw.GetErrorMsg(err)}")
        return _rows(result)

    def query_snapshot(self, request: Any) -> list[dict[str, Any]]:
        tgw = self._sdk()
        result, err = tgw.QuerySnapshot(request, return_df_format=False)
        if err:
            raise YinheUpstreamError(f"银河快照查询失败：{tgw.GetErrorMsg(err)}")
        return _rows(result)

    def query_securities_info(self, request: Any) -> list[dict[str, Any]]:
        tgw = self._sdk()
        result, err = tgw.QuerySecuritiesInfo(request, return_df_format=False)
        if err:
            raise YinheUpstreamError(f"银河证券信息查询失败：{tgw.GetErrorMsg(err)}")
        return _rows(result)

    def fetch_daily(
        self,
        symbols: list[str],
        trade_date: str,
        *,
        interval: str | int = "day",
    ) -> list[dict[str, Any]]:
        tgw = self._sdk()
        rows: list[dict[str, Any]] = []
        empty_count = 0
        successful_count = 0
        total = len(symbols)
        for index, symbol in enumerate(symbols, 1):
            request = build_kline_request(
                tgw,
                symbol,
                begin_date=trade_date,
                end_date=trade_date,
                interval=interval,
            )
            empty_error = False
            try:
                symbol_rows = self.query_kline(request)
            except YinheUpstreamError as exc:
                if "数据为空" in str(exc):
                    empty_error = True
                    empty_count += 1
                    symbol_rows = []
                else:
                    raise
            if symbol_rows:
                successful_count += 1
                rows.extend(symbol_rows)
            elif not empty_error:
                empty_count += 1
            if total >= 100 and (index % 500 == 0 or index == total):
                print(
                    f"银河日线查询进度：{index}/{total}，"
                    f"有数据={successful_count}，空数据={empty_count}"
                )
        return rows

    def fetch_daily_range(
        self,
        symbols: list[str],
        start_date: str,
        end_date: str,
        *,
        interval: str | int = "day",
        retries: int = 3,
        retry_delay_seconds: float = 5.0,
    ) -> list[dict[str, Any]]:
        if retries < 0:
            raise ValueError("retries 不能小于 0")
        if retry_delay_seconds < 0:
            raise ValueError("retry_delay_seconds 不能小于 0")
        tgw = self._sdk()
        rows: list[dict[str, Any]] = []
        empty_count = 0
        successful_count = 0
        total = len(symbols)
        for index, symbol in enumerate(symbols, 1):
            request = build_kline_request(
                tgw,
                symbol,
                begin_date=start_date,
                end_date=end_date,
                interval=interval,
            )
            attempt = 0
            while True:
                try:
                    symbol_rows = self.query_kline(request)
                    break
                except YinheUpstreamError as exc:
                    message = str(exc)
                    if "数据为空" in message:
                        symbol_rows = []
                        break
                    retryable = "超时" in message or "timeout" in message.lower()
                    if not retryable or attempt >= retries:
                        raise
                    attempt += 1
                    wait_seconds = retry_delay_seconds * attempt
                    print(
                        f"银河 K 线查询超时：symbol={symbol}，"
                        f"重试={attempt}/{retries}，等待={wait_seconds:g}秒"
                    )
                    if wait_seconds > 0:
                        time.sleep(wait_seconds)
            if symbol_rows:
                successful_count += 1
                rows.extend(symbol_rows)
            else:
                empty_count += 1
            if total >= 100 and (index % 500 == 0 or index == total):
                print(
                    f"银河区间日线查询进度：{index}/{total}，"
                    f"有数据={successful_count}，空数据={empty_count}，累计K线={len(rows)}"
                )
        return rows

    def fetch_snapshots(
        self,
        symbols: list[str],
        trade_date: str,
        *,
        level_type: int = 1,
        begin_time: int = 0,
        end_time: int = 0,
        data_type: str | int = "snapshot",
    ) -> list[dict[str, Any]]:
        tgw = self._sdk()
        rows: list[dict[str, Any]] = []
        for symbol in symbols:
            request = build_snapshot_request(
                tgw,
                symbol,
                trade_date=trade_date,
                level_type=level_type,
                begin_time=begin_time,
                end_time=end_time,
                data_type=data_type,
            )
            rows.extend(self.query_snapshot(request))
        return rows

    def fetch_securities(self, markets: tuple[str, ...] = ("sse", "szse")) -> list[dict[str, Any]]:
        tgw = self._sdk()
        rows: list[dict[str, Any]] = []
        for market in markets:
            request = build_security_info_request(tgw, market)
            market_rows = self.query_securities_info(request)
            for row in market_rows:
                item = dict(row)
                item.setdefault("_query_market", market)
                rows.append(item)
        return rows


def _with_logged_in_client(config: YinheConfig, action: Callable[[YinheClient], list[dict[str, Any]]]) -> list[dict[str, Any]]:
    client = YinheClient(config)
    if not client.login():
        raise YinheUpstreamError("银河登录失败，请检查账号、密码、IP、端口和账号权限")
    try:
        return action(client)
    finally:
        client.close()


def sync_securities(
    root: Path,
    *,
    as_of: str | None = None,
    config: YinheConfig | None = None,
    fetcher: Callable[[], list[dict[str, Any]]] | None = None,
) -> dict[str, Any]:
    observed_at = _observed_at(as_of)
    date_key = observed_at.strftime("%Y%m%d")
    rows = fetcher() if fetcher else _with_logged_in_client(config or YinheConfig.from_env(), lambda client: client.fetch_securities())
    securities = normalize_security_rows(rows)
    _write_snapshot(
        root / "data" / "raw" / "yinhe" / date_key / "securities.json",
        "QuerySecuritiesInfo",
        rows,
    )
    processed_path = root / "data" / "processed" / "yinhe_securities.csv"
    _write_csv(processed_path, securities, SECURITY_FIELDS)
    return {
        "observed_date": date_key,
        "raw_rows": len(rows),
        "security_rows": len(securities),
        "processed_path": str(processed_path),
    }


def sync_daily(
    root: Path,
    trade_date: str,
    *,
    symbols: list[str] | None = None,
    config: YinheConfig | None = None,
    interval: str | int = "day",
    fetcher: Callable[[], list[dict[str, Any]]] | None = None,
) -> dict[str, Any]:
    if fetcher:
        rows = fetcher()
    else:
        if not symbols:
            raise ValueError("请通过 --symbols 或 --symbols-file 提供至少一个六位股票代码")
        rows = _with_logged_in_client(
            config or YinheConfig.from_env(),
            lambda client: client.fetch_daily(symbols, trade_date, interval=interval),
        )
    daily = normalize_daily_rows(rows, trade_date)
    _write_snapshot(
        root / "data" / "raw" / "yinhe" / trade_date / "daily.json",
        "QueryKline",
        rows,
    )
    processed_path = root / "data" / "processed" / "yinhe_daily" / f"{trade_date}.csv"
    _write_csv(processed_path, daily, DAILY_FIELDS)
    result = {
        "trade_date": trade_date,
        "raw_rows": len(rows),
        "daily_rows": len(daily),
        "processed_path": str(processed_path),
    }
    if symbols:
        result.update(_daily_coverage(root, trade_date, symbols, daily))
    return result


def backfill_daily(
    root: Path,
    start_date: str,
    end_date: str,
    *,
    symbols: list[str],
    config: YinheConfig | None = None,
    interval: str | int = "day",
    max_days: int | None = None,
    delay_seconds: float = 0.0,
    overwrite: bool = False,
    fetcher: Callable[[str], list[dict[str, Any]]] | None = None,
) -> dict[str, Any]:
    if not symbols:
        raise ValueError("请通过 --symbols 或 --symbols-file 提供至少一个六位股票代码")
    dates = _weekdays_between(start_date, end_date)
    pending = [
        day
        for day in dates
        if overwrite or not (root / "data" / "processed" / "yinhe_daily" / f"{day}.csv").exists()
    ]
    if max_days is not None:
        pending = pending[:max_days]

    summary: dict[str, Any] = {
        "start_date": start_date,
        "end_date": end_date,
        "symbols": len(symbols),
        "trade_dates": len(dates),
        "pending": len(pending),
        "completed": 0,
        "failed": 0,
    }

    def write_day(day: str, rows: list[dict[str, Any]]) -> dict[str, Any]:
        daily = normalize_daily_rows(rows, day)
        _write_snapshot(
            root / "data" / "raw" / "yinhe" / day / "daily.json",
            "QueryKline",
            rows,
        )
        processed_path = root / "data" / "processed" / "yinhe_daily" / f"{day}.csv"
        _write_csv(processed_path, daily, DAILY_FIELDS)
        result = {
            "trade_date": day,
            "raw_rows": len(rows),
            "daily_rows": len(daily),
            "processed_path": str(processed_path),
        }
        result.update(_daily_coverage(root, day, symbols, daily))
        return result

    if fetcher:
        for index, day in enumerate(pending, 1):
            try:
                result = write_day(day, fetcher(day))
            except Exception as exc:  # noqa: BLE001 - CLI 批处理需要保留断点
                summary["failed"] += 1
                print(f"[{index}/{len(pending)}] {day}：failed={exc}")
                break
            summary["completed"] += 1
            print(
                f"[{index}/{len(pending)}] {day}：daily={result['daily_rows']}，"
                f"coverage={result['coverage_rate']:.2%}"
            )
            if delay_seconds > 0:
                import time

                time.sleep(delay_seconds)
        return summary

    client = YinheClient(config or YinheConfig.from_env())
    if not client.login():
        raise YinheUpstreamError("银河登录失败，请检查账号、密码、IP、端口和账号权限")
    try:
        for index, day in enumerate(pending, 1):
            try:
                rows = client.fetch_daily(symbols, day, interval=interval)
                result = write_day(day, rows)
            except Exception as exc:  # noqa: BLE001 - 供应商 SDK 异常类型不稳定
                summary["failed"] += 1
                print(f"[{index}/{len(pending)}] {day}：failed={exc}")
                break
            summary["completed"] += 1
            summary["last_coverage_rate"] = result["coverage_rate"]
            summary["last_missing_symbols"] = result["missing_symbols"]
            print(
                f"[{index}/{len(pending)}] {day}：daily={result['daily_rows']}，"
                f"coverage={result['coverage_rate']:.2%}"
            )
            if delay_seconds > 0:
                import time

                time.sleep(delay_seconds)
    finally:
        client.close()
    return summary


def backfill_daily_range(
    root: Path,
    start_date: str,
    end_date: str,
    *,
    symbols: list[str],
    config: YinheConfig | None = None,
    interval: str | int = "day",
    chunk_size: int = 250,
    query_retries: int = 3,
    retry_delay_seconds: float = 5.0,
    overwrite: bool = False,
    fetcher: Callable[[], list[dict[str, Any]]] | None = None,
) -> dict[str, Any]:
    if not symbols:
        raise ValueError("请通过 --symbols 或 --symbols-file 提供至少一个六位股票代码")
    start_key = _date_key(start_date)
    end_key = _date_key(end_date)
    if not start_key or not end_key or start_key > end_key:
        raise ValueError("开始和结束日期必须有效，且开始日期不能晚于结束日期")
    if chunk_size <= 0:
        raise ValueError("chunk_size 必须大于 0")

    pool_key = hashlib.sha256(
        f"{start_key}|{end_key}|{interval}|{chunk_size}|{'|'.join(symbols)}".encode()
    ).hexdigest()[:12]
    checkpoint_dir = (
        root
        / "data"
        / "raw"
        / "yinhe"
        / "ranges"
        / f"{start_key}_{end_key}_{pool_key}"
    )
    checkpoint_dir.mkdir(parents=True, exist_ok=True)
    chunks = (
        [symbols]
        if fetcher is not None
        else [
            symbols[offset : offset + chunk_size]
            for offset in range(0, len(symbols), chunk_size)
        ]
    )
    returned_dates: set[str] = set()
    merged_dates: set[str] = set()
    raw_rows = 0
    daily_rows = 0
    query_count = 0
    reused_chunks = 0

    def merge_chunk(
        rows: list[dict[str, Any]],
        chunk_symbols: list[str],
        chunk_index: int,
    ) -> dict[str, Any]:
        daily = [
            row
            for row in normalize_daily_rows(rows, end_key)
            if start_key <= row["trade_date"] <= end_key
        ]
        daily_by_date: dict[str, list[dict[str, Any]]] = {}
        for row in daily:
            daily_by_date.setdefault(row["trade_date"], []).append(row)

        for day, day_rows in sorted(daily_by_date.items()):
            processed_path = root / "data" / "processed" / "yinhe_daily" / f"{day}.csv"
            existing_rows: list[dict[str, Any]] = []
            if processed_path.exists():
                with processed_path.open("r", encoding="utf-8-sig", newline="") as handle:
                    existing_rows = list(csv.DictReader(handle))
            merged = {
                _strip_suffix(row.get("symbol", "")): row
                for row in existing_rows
                if len(_strip_suffix(row.get("symbol", ""))) == 6
            }
            for row in day_rows:
                symbol = _strip_suffix(row.get("symbol", ""))
                if overwrite or symbol not in merged:
                    merged[symbol] = row
            _write_csv(
                processed_path,
                sorted(merged.values(), key=lambda item: item["symbol"]),
                DAILY_FIELDS,
            )

        raw_path = checkpoint_dir / f"chunk_{chunk_index:04d}.json"
        _write_snapshot(raw_path, "QueryKlineRange", rows)
        metadata = {
            "chunk_index": chunk_index,
            "symbols": chunk_symbols,
            "raw_rows": len(rows),
            "daily_rows": len(daily),
            "returned_dates": sorted(daily_by_date),
            "raw_path": str(raw_path),
        }
        done_path = checkpoint_dir / f"chunk_{chunk_index:04d}.done.json"
        done_path.write_text(
            json.dumps(metadata, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        return metadata

    client: YinheClient | None = None
    if fetcher is None:
        client = YinheClient(config or YinheConfig.from_env())
        if not client.login():
            raise YinheUpstreamError("银河登录失败，请检查账号、密码、IP、端口和账号权限")
    try:
        for chunk_index, chunk_symbols in enumerate(chunks, 1):
            done_path = checkpoint_dir / f"chunk_{chunk_index:04d}.done.json"
            if done_path.exists() and not overwrite:
                metadata = json.loads(done_path.read_text(encoding="utf-8"))
                reused_chunks += 1
                print(
                    f"银河区间回填块：{chunk_index}/{len(chunks)}，"
                    f"checkpoint=复用，symbols={len(chunk_symbols)}"
                )
            else:
                if fetcher:
                    rows = fetcher()
                else:
                    assert client is not None
                    rows = client.fetch_daily_range(
                        chunk_symbols,
                        start_key,
                        end_key,
                        interval=interval,
                        retries=query_retries,
                        retry_delay_seconds=retry_delay_seconds,
                    )
                metadata = merge_chunk(rows, chunk_symbols, chunk_index)
                query_count += len(chunk_symbols)
                merged_dates.update(metadata["returned_dates"])
                print(
                    f"银河区间回填块：{chunk_index}/{len(chunks)}，"
                    f"symbols={len(chunk_symbols)}，K线={metadata['daily_rows']}"
                )
            raw_rows += int(metadata["raw_rows"])
            daily_rows += int(metadata["daily_rows"])
            returned_dates.update(metadata["returned_dates"])
    finally:
        if client is not None:
            client.close()

    coverage_by_date: dict[str, float] = {}
    missing_by_date: dict[str, int] = {}
    for day in sorted(returned_dates):
        processed_path = root / "data" / "processed" / "yinhe_daily" / f"{day}.csv"
        if not processed_path.exists():
            continue
        with processed_path.open("r", encoding="utf-8-sig", newline="") as handle:
            day_rows = list(csv.DictReader(handle))
        coverage = _daily_coverage(root, day, symbols, day_rows)
        coverage_by_date[day] = coverage["coverage_rate"]
        missing_by_date[day] = coverage["missing_symbols"]

    return {
        "start_date": start_key,
        "end_date": end_key,
        "symbols": len(symbols),
        "planned_query_count": len(symbols),
        "query_count": query_count,
        "chunk_size": chunk_size,
        "chunks": len(chunks),
        "reused_chunks": reused_chunks,
        "raw_rows": raw_rows,
        "daily_rows": daily_rows,
        "returned_dates": len(returned_dates),
        "merged_dates": len(merged_dates),
        "first_returned_date": min(returned_dates, default=None),
        "last_returned_date": max(returned_dates, default=None),
        "checkpoint_dir": str(checkpoint_dir),
        "coverage_by_date": coverage_by_date,
        "missing_by_date": missing_by_date,
    }


def sync_snapshots(
    root: Path,
    trade_date: str,
    *,
    symbols: list[str] | None = None,
    config: YinheConfig | None = None,
    level_type: int = 1,
    begin_time: int = 0,
    end_time: int = 0,
    data_type: str | int = "snapshot",
    fetcher: Callable[[], list[dict[str, Any]]] | None = None,
) -> dict[str, Any]:
    if fetcher:
        rows = fetcher()
    else:
        if not symbols:
            raise ValueError("请通过 --symbols 或 --symbols-file 提供至少一个六位股票代码")
        rows = _with_logged_in_client(
            config or YinheConfig.from_env(),
            lambda client: client.fetch_snapshots(
                symbols,
                trade_date,
                level_type=level_type,
                begin_time=begin_time,
                end_time=end_time,
                data_type=data_type,
            ),
        )
    snapshots = normalize_snapshot_rows(rows, trade_date)
    _write_snapshot(
        root / "data" / "raw" / "yinhe" / trade_date / "snapshot.json",
        "QuerySnapshot",
        rows,
    )
    processed_path = root / "data" / "processed" / "yinhe_snapshots" / f"{trade_date}.csv"
    _write_csv(processed_path, snapshots, SNAPSHOT_FIELDS)
    return {
        "trade_date": trade_date,
        "raw_rows": len(rows),
        "snapshot_rows": len(snapshots),
        "processed_path": str(processed_path),
    }


def fetch_amazing_data_snapshots(
    config: YinheConfig,
    symbols: list[str],
    trade_date: str,
    *,
    begin_time: int = 0,
    end_time: int = 0,
    code_format: str = "suffix",
    timeout_seconds: int = 60,
) -> list[dict[str, Any]]:
    if timeout_seconds <= 0:
        return _fetch_amazing_data_snapshots_direct(
            config,
            symbols,
            trade_date,
            begin_time=begin_time,
            end_time=end_time,
            code_format=code_format,
        )
    result_queue: multiprocessing.Queue = multiprocessing.Queue(maxsize=1)
    process = multiprocessing.Process(
        target=_fetch_amazing_data_snapshots_worker,
        args=(result_queue, asdict(config), symbols, trade_date, begin_time, end_time, code_format),
    )
    process.start()
    process.join(timeout_seconds)
    if process.is_alive():
        process.terminate()
        process.join(5)
        raise YinheUpstreamError(
            f"AmazingData 快照查询超过 {timeout_seconds} 秒未返回；供应商连接已关闭或账号缺少历史快照权限"
        )
    try:
        payload = result_queue.get_nowait()
    except queue.Empty as exc:
        raise YinheUpstreamError("AmazingData 快照查询未返回结果；供应商连接可能已关闭") from exc
    if not payload.get("ok"):
        raise YinheUpstreamError(str(payload.get("error") or "AmazingData 快照查询失败"))
    return list(payload.get("rows") or [])


def sync_snapshots_amazing_data(
    root: Path,
    trade_date: str,
    *,
    symbols: list[str] | None = None,
    config: YinheConfig | None = None,
    begin_time: int = 0,
    end_time: int = 0,
    code_format: str = "suffix",
    timeout_seconds: int = 60,
    fetcher: Callable[[], list[dict[str, Any]]] | None = None,
) -> dict[str, Any]:
    if fetcher:
        rows = fetcher()
    else:
        if not symbols:
            raise ValueError("请通过 --symbols 或 --symbols-file 提供至少一个六位股票代码")
        rows = fetch_amazing_data_snapshots(
            config or YinheConfig.from_env(),
            symbols,
            trade_date,
            begin_time=begin_time,
            end_time=end_time,
            code_format=code_format,
            timeout_seconds=timeout_seconds,
        )
    snapshots = normalize_snapshot_rows(rows, trade_date)
    _write_snapshot(
        root / "data" / "raw" / "yinhe" / trade_date / "snapshot_ad.json",
        "AmazingData.MarketData.query_snapshot",
        rows,
    )
    processed_path = root / "data" / "processed" / "yinhe_snapshots_ad" / f"{trade_date}.csv"
    _write_csv(processed_path, snapshots, SNAPSHOT_FIELDS)
    return {
        "trade_date": trade_date,
        "raw_rows": len(rows),
        "snapshot_rows": len(snapshots),
        "processed_path": str(processed_path),
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="同步中国银河星耀数智数据")
    parser.add_argument(
        "command",
        choices=[
            "securities",
            "build-symbols",
            "daily",
            "backfill-daily",
            "backfill-range",
            "audit-daily",
            "acceptance",
            "repair-turnover",
            "adjustment-ad",
            "snapshot",
            "snapshot-ad",
        ],
    )
    parser.add_argument("--root", default=".")
    parser.add_argument("--env-file", default=".env")
    parser.add_argument("--securities-file", help="银河证券清单 CSV；build-symbols 默认读取 data/processed/yinhe_securities.csv")
    parser.add_argument("--output", help="build-symbols 输出文件；默认 data/processed/yinhe_symbols.txt")
    parser.add_argument("--include-st", action="store_true", help="build-symbols 保留 ST 股票；默认排除")
    parser.add_argument("--include-delisting", action="store_true", help="build-symbols 保留退市风险股票；默认排除")
    parser.add_argument("--start", help="开始日期，格式 YYYYMMDD；backfill-daily/backfill-range 需要")
    parser.add_argument("--end", help="结束日期，格式 YYYYMMDD；backfill-daily/backfill-range 需要")
    parser.add_argument("--date", help="交易日，格式 YYYYMMDD；daily/snapshot 需要")
    parser.add_argument("--as-of", help="观察日期，格式 YYYY-MM-DD；仅 securities 需要")
    parser.add_argument("--symbols", help="逗号分隔六位股票代码；daily/snapshot 需要")
    parser.add_argument("--symbols-file", help="包含六位股票代码的文本/CSV文件；daily/snapshot 需要")
    parser.add_argument("--interval", default="day", help="K线周期，默认 day；可用 1m/5m/day/week/month 等")
    parser.add_argument("--max-days", type=int, help="backfill-daily 本批最多处理多少个工作日")
    parser.add_argument("--chunk-size", type=int, default=250, help="backfill-range 每块股票数量，默认 250")
    parser.add_argument("--query-retries", type=int, default=3, help="backfill-range 查询超时后的重试次数，默认 3")
    parser.add_argument("--retry-delay", type=float, default=5.0, help="backfill-range 重试基础等待秒数，默认 5")
    parser.add_argument("--calendar-file", help="acceptance 使用的独立交易日历 CSV")
    parser.add_argument("--acceptance-output", help="acceptance 报告输出目录")
    parser.add_argument("--delay", type=float, default=0.0, help="backfill-daily 每个日期之间等待秒数，默认 0")
    parser.add_argument(
        "--overwrite",
        action="store_true",
        help="强制覆盖已存在数据；backfill-range 同时忽略检查点并重新查询",
    )
    parser.add_argument("--level-type", type=int, default=1, help="快照 Level 类型，默认 1")
    parser.add_argument("--begin-time", type=int, default=0, help="查询开始时间，默认 0；可试 93000000")
    parser.add_argument("--end-time", type=int, default=0, help="查询结束时间，默认 0；可试 150000000")
    parser.add_argument("--snapshot-data-type", default="snapshot", help="快照数据类型，默认 snapshot；可试 l1/l2/index 或 SDK 枚举整数")
    parser.add_argument("--code-format", default="suffix", choices=["suffix", "prefix", "raw"], help="AmazingData 代码格式，默认 600000.SH")
    parser.add_argument("--query-timeout", type=int, default=60, help="AmazingData 查询超时秒数，默认 60；设为 0 可关闭")
    args = parser.parse_args()
    root = Path(args.root).resolve()
    try:
        if args.command == "securities":
            result = sync_securities(root, as_of=args.as_of, config=YinheConfig.from_env(args.env_file))
        elif args.command == "build-symbols":
            result = build_symbol_pool(
                root,
                securities_path=Path(args.securities_file).resolve() if args.securities_file else None,
                output_path=Path(args.output).resolve() if args.output else None,
                include_st=args.include_st,
                include_delisting=args.include_delisting,
            )
        elif args.command == "daily":
            if not args.date:
                raise SystemExit("daily 必须提供 --date YYYYMMDD")
            symbols = _read_symbols(args.symbols, args.symbols_file)
            if not symbols:
                raise SystemExit("daily 必须通过 --symbols 或 --symbols-file 提供至少一个六位股票代码")
            result = sync_daily(
                root,
                args.date,
                symbols=symbols,
                config=YinheConfig.from_env(args.env_file),
                interval=args.interval,
            )
        elif args.command == "backfill-daily":
            if not args.start or not args.end:
                raise SystemExit("backfill-daily 必须提供 --start YYYYMMDD 和 --end YYYYMMDD")
            symbols = _read_symbols(args.symbols, args.symbols_file)
            if not symbols:
                raise SystemExit("backfill-daily 必须通过 --symbols 或 --symbols-file 提供至少一个六位股票代码")
            result = backfill_daily(
                root,
                args.start,
                args.end,
                symbols=symbols,
                config=YinheConfig.from_env(args.env_file),
                interval=args.interval,
                max_days=args.max_days,
                delay_seconds=args.delay,
                overwrite=args.overwrite,
            )
        elif args.command == "backfill-range":
            if not args.start or not args.end:
                raise SystemExit("backfill-range 必须提供 --start YYYYMMDD 和 --end YYYYMMDD")
            symbols = _read_symbols(args.symbols, args.symbols_file)
            if not symbols:
                raise SystemExit("backfill-range 必须通过 --symbols 或 --symbols-file 提供至少一个六位股票代码")
            result = backfill_daily_range(
                root,
                args.start,
                args.end,
                symbols=symbols,
                config=YinheConfig.from_env(args.env_file),
                interval=args.interval,
                chunk_size=args.chunk_size,
                query_retries=args.query_retries,
                retry_delay_seconds=args.retry_delay,
                overwrite=args.overwrite,
            )
        elif args.command == "audit-daily":
            if not args.date:
                raise SystemExit("audit-daily 必须提供 --date YYYYMMDD")
            symbols = _read_symbols(args.symbols, args.symbols_file)
            if not symbols:
                raise SystemExit("audit-daily 必须通过 --symbols 或 --symbols-file 提供至少一个六位股票代码")
            result = audit_daily_coverage(root, args.date, symbols)
        elif args.command == "acceptance":
            if not args.start or not args.end:
                raise SystemExit("acceptance 必须提供 --start YYYYMMDD 和 --end YYYYMMDD")
            from .yinhe_acceptance import run_yinhe_acceptance

            result = run_yinhe_acceptance(
                root,
                start_date=args.start,
                end_date=args.end,
                calendar_file=Path(args.calendar_file).resolve() if args.calendar_file else None,
                output_dir=Path(args.acceptance_output).resolve() if args.acceptance_output else None,
            )
        elif args.command == "repair-turnover":
            if not args.start or not args.end:
                raise SystemExit("repair-turnover 必须提供 --start YYYYMMDD 和 --end YYYYMMDD")
            from .yinhe_acceptance import repair_yinhe_turnover_units

            result = repair_yinhe_turnover_units(
                root,
                start_date=args.start,
                end_date=args.end,
            )
        elif args.command == "adjustment-ad":
            if not args.start or not args.end:
                raise SystemExit("adjustment-ad 必须提供 --start YYYYMMDD 和 --end YYYYMMDD")
            symbols = _read_symbols(args.symbols, args.symbols_file)
            if not symbols:
                raise SystemExit("adjustment-ad 必须通过 --symbols 或 --symbols-file 提供股票代码")
            from .yinhe_adjustment import sync_and_build_adjustment

            result = sync_and_build_adjustment(
                root,
                start_date=args.start,
                end_date=args.end,
                symbols=symbols,
                config=YinheConfig.from_env(args.env_file),
                chunk_size=args.chunk_size,
                overwrite=args.overwrite,
            )
        elif args.command == "snapshot":
            if not args.date:
                raise SystemExit("snapshot 必须提供 --date YYYYMMDD")
            symbols = _read_symbols(args.symbols, args.symbols_file)
            if not symbols:
                raise SystemExit("snapshot 必须通过 --symbols 或 --symbols-file 提供至少一个六位股票代码")
            result = sync_snapshots(
                root,
                args.date,
                symbols=symbols,
                config=YinheConfig.from_env(args.env_file),
                level_type=args.level_type,
                begin_time=args.begin_time,
                end_time=args.end_time,
                data_type=args.snapshot_data_type,
            )
        else:
            if not args.date:
                raise SystemExit("snapshot-ad 必须提供 --date YYYYMMDD")
            symbols = _read_symbols(args.symbols, args.symbols_file)
            if not symbols:
                raise SystemExit("snapshot-ad 必须通过 --symbols 或 --symbols-file 提供至少一个六位股票代码")
            result = sync_snapshots_amazing_data(
                root,
                args.date,
                symbols=symbols,
                config=YinheConfig.from_env(args.env_file),
                begin_time=args.begin_time,
                end_time=args.end_time,
                code_format=args.code_format,
                timeout_seconds=args.query_timeout,
            )
    except (YinheUnavailable, YinheUpstreamError, ValueError) as exc:
        raise SystemExit(str(exc)) from exc
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()

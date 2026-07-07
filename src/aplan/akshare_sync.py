from __future__ import annotations

import argparse
import csv
import hashlib
import json
import math
import time
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any, Callable


class AkShareUnavailable(RuntimeError):
    pass


class AkShareUpstreamError(RuntimeError):
    pass


VALUATION_FIELDS = (
    "symbol",
    "trade_date",
    "pe",
    "pb",
    "total_mv",
    "circ_mv",
    "turnover_rate",
    "volume_ratio",
)

FUNDAMENTAL_FIELDS = (
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
)

SECURITY_FIELDS = (
    "symbol",
    "name",
    "list_date",
    "industry",
    "is_st",
    "is_delisting_risk",
)


def _write_csv(path: Path, rows: list[dict[str, Any]], fields: tuple[str, ...]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)


def _write_snapshot(path: Path, api_name: str, rows: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(
            {
                "api_name": api_name,
                "downloaded_at": datetime.now(UTC).isoformat(),
                "row_count": len(rows),
                "rows": rows,
            },
            ensure_ascii=False,
            indent=2,
            default=str,
        ),
        encoding="utf-8",
    )


def _number(value: Any) -> float:
    if value in (None, "", "-"):
        return 0.0
    try:
        number = float(value)
    except (TypeError, ValueError):
        return 0.0
    return number if math.isfinite(number) else 0.0


def _records(dataframe: Any) -> list[dict[str, Any]]:
    if hasattr(dataframe, "to_dict"):
        return list(dataframe.to_dict(orient="records"))
    return list(dataframe)


def _fetch_spot_em_rows() -> list[dict[str, Any]]:
    try:
        import akshare as ak  # type: ignore[import-not-found]
    except ImportError as exc:
        raise AkShareUnavailable(
            "未安装 akshare；请先安装可选依赖：python3 -m pip install -e '.[akshare]'"
        ) from exc

    dataframe = ak.stock_zh_a_spot_em()
    return _records(dataframe)


def _fetch_financial_indicator_rows(symbol: str, start_year: str) -> list[dict[str, Any]]:
    try:
        import akshare as ak  # type: ignore[import-not-found]
    except ImportError as exc:
        raise AkShareUnavailable(
            "未安装 akshare；请先安装可选依赖：python3 -m pip install -e '.[akshare]'"
        ) from exc

    dataframe = ak.stock_financial_analysis_indicator(symbol=symbol, start_year=start_year)
    return _records(dataframe)


def _fetch_security_rows() -> list[dict[str, Any]]:
    try:
        import akshare as ak  # type: ignore[import-not-found]
    except ImportError as exc:
        raise AkShareUnavailable(
            "未安装 akshare；请先安装可选依赖：python3 -m pip install -e '.[akshare]'"
        ) from exc

    rows: list[dict[str, Any]] = []
    for source, dataframe in (
        ("akshare:stock_info_sh_name_code:main_a:sse", ak.stock_info_sh_name_code(symbol="主板A股")),
        ("akshare:stock_info_sz_name_code:a_list:szse", ak.stock_info_sz_name_code(symbol="A股列表")),
    ):
        for row in _records(dataframe):
            item = dict(row)
            item["_source"] = source
            rows.append(item)
    return rows


def _fetch_with_retries(
    fetcher: Callable[[], list[dict[str, Any]]],
    *,
    retries: int,
    retry_delay: float,
) -> list[dict[str, Any]]:
    attempts = max(1, retries + 1)
    last_error: Exception | None = None
    for attempt in range(1, attempts + 1):
        try:
            return fetcher()
        except AkShareUnavailable:
            raise
        except Exception as exc:  # noqa: BLE001 - 数据源异常类型不稳定，统一包装为可读错误
            last_error = exc
            if attempt < attempts and retry_delay > 0:
                time.sleep(retry_delay)
    detail = f"{type(last_error).__name__}: {last_error}" if last_error else "unknown error"
    raise AkShareUpstreamError(
        "AkShare/东方财富现货估值接口暂时不可用；"
        f"已尝试 {attempts} 次。原始错误：{detail}"
    ) from last_error


def normalize_spot_em_rows(rows: list[dict[str, Any]], trade_date: str) -> list[dict[str, Any]]:
    output: list[dict[str, Any]] = []
    for row in rows:
        symbol = str(row.get("代码") or row.get("symbol") or "").strip()
        if len(symbol) != 6 or not symbol.isdigit():
            continue
        output.append(
            {
                "symbol": symbol,
                "trade_date": trade_date,
                "pe": _number(row.get("市盈率-动态") or row.get("市盈率")),
                "pb": _number(row.get("市净率")),
                # AkShare/东方财富现货接口通常以元展示市值；APlan processed valuations 保持字段名一致，
                # 来源单位通过 raw snapshot 和 source 说明审计，不与 Tushare 万元口径直接混算。
                "total_mv": _number(row.get("总市值")),
                "circ_mv": _number(row.get("流通市值")),
                "turnover_rate": _number(row.get("换手率")),
                "volume_ratio": _number(row.get("量比")),
            }
        )
    return sorted(output, key=lambda item: item["symbol"])


def _parse_period_end(value: Any) -> date | None:
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    text = str(value or "").strip()
    if not text:
        return None
    try:
        return datetime.fromisoformat(text).date()
    except ValueError:
        return None


def _row_hash(row: dict[str, Any]) -> str:
    payload = json.dumps(row, ensure_ascii=False, sort_keys=True, default=str)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _pick_number(row: dict[str, Any], aliases: tuple[str, ...], *, percent: bool = True) -> float | None:
    normalized = {str(key).replace(" ", ""): value for key, value in row.items()}
    for alias in aliases:
        alias_key = alias.replace(" ", "")
        for key, value in normalized.items():
            if alias_key in key:
                number = _number(value)
                if number == 0.0 and str(value).lower() in {"nan", "none", ""}:
                    return None
                return number / 100 if percent else number
    return None


def normalize_financial_indicator_rows(
    symbol: str,
    rows: list[dict[str, Any]],
    observed_at: datetime,
) -> list[dict[str, Any]]:
    output: list[dict[str, Any]] = []
    for row in rows:
        period_end = _parse_period_end(row.get("日期") or row.get("报告期") or row.get("REPORT_DATE"))
        if period_end is None:
            continue
        output.append(
            {
                "symbol": symbol,
                "period_end": period_end.isoformat(),
                # 新浪财务指标接口不提供可靠公告发布时间；这里使用观察时间，避免污染历史回测。
                "publish_time": observed_at.isoformat(),
                "source": "akshare:stock_financial_analysis_indicator:sina:observed",
                "source_hash": _row_hash(row),
                "revenue_growth": _pick_number(row, ("主营业务收入增长率", "营业收入增长率")),
                "net_profit_growth": _pick_number(row, ("净利润增长率", "净利润同比增长率")),
                "roe": _pick_number(row, ("净资产收益率", "加权净资产收益率")),
                "operating_cashflow_to_profit": _pick_number(
                    row,
                    (
                        "经营现金净流量与净利润的比率",
                        "经营现金流量净额/净利润",
                        "经营现金流净额与净利润的比率",
                    ),
                ),
                "debt_to_assets": _pick_number(row, ("资产负债率",)),
            }
        )
    return sorted(output, key=lambda item: (item["symbol"], item["period_end"]))


def _parse_date_text(value: Any) -> str:
    parsed = _parse_period_end(value)
    return parsed.isoformat() if parsed else "1900-01-01"


def normalize_security_rows(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    output: list[dict[str, Any]] = []
    seen: set[str] = set()
    for row in rows:
        symbol = str(row.get("证券代码") or row.get("A股代码") or row.get("code") or "").strip()
        symbol = symbol.split(".", 1)[0].zfill(6)
        if len(symbol) != 6 or not symbol.isdigit() or symbol in seen:
            continue
        name = str(row.get("证券简称") or row.get("A股简称") or row.get("name") or "").strip()
        if not name:
            continue
        seen.add(symbol)
        output.append(
            {
                "symbol": symbol,
                "name": name,
                "list_date": _parse_date_text(row.get("上市日期") or row.get("A股上市日期")),
                "industry": str(row.get("所属行业") or row.get("industry") or "未知").strip() or "未知",
                "is_st": "1" if "ST" in name.upper() else "0",
                "is_delisting_risk": "1" if "退" in name else "0",
            }
        )
    return sorted(output, key=lambda item: item["symbol"])


def sync_spot_valuations(
    root: Path,
    trade_date: str | None = None,
    *,
    retries: int = 2,
    retry_delay: float = 2.0,
    fetcher: Callable[[], list[dict[str, Any]]] | None = None,
) -> dict[str, Any]:
    date_key = trade_date or datetime.now(UTC).strftime("%Y%m%d")
    rows = _fetch_with_retries(fetcher or _fetch_spot_em_rows, retries=retries, retry_delay=retry_delay)
    valuations = normalize_spot_em_rows(rows, date_key)
    _write_snapshot(
        root / "data" / "raw" / "akshare" / date_key / "stock_zh_a_spot_em.json",
        "stock_zh_a_spot_em",
        rows,
    )
    _write_csv(
        root / "data" / "processed" / "akshare_valuations" / f"{date_key}.csv",
        valuations,
        VALUATION_FIELDS,
    )
    return {
        "trade_date": date_key,
        "raw_rows": len(rows),
        "valuation_rows": len(valuations),
        "processed_path": str(root / "data" / "processed" / "akshare_valuations" / f"{date_key}.csv"),
    }


def sync_securities(
    root: Path,
    *,
    as_of: str | None = None,
    retries: int = 1,
    retry_delay: float = 2.0,
    fetcher: Callable[[], list[dict[str, Any]]] | None = None,
) -> dict[str, Any]:
    observed_at = (
        datetime.fromisoformat(as_of).replace(tzinfo=UTC)
        if as_of
        else datetime.now(UTC)
    )
    date_key = observed_at.strftime("%Y%m%d")
    rows = _fetch_with_retries(fetcher or _fetch_security_rows, retries=retries, retry_delay=retry_delay)
    securities = normalize_security_rows(rows)
    _write_snapshot(
        root / "data" / "raw" / "akshare" / date_key / "stock_info_securities.json",
        "stock_info_sh_name_code+stock_info_sz_name_code",
        rows,
    )
    processed_path = root / "data" / "processed" / "securities.csv"
    _write_csv(processed_path, securities, SECURITY_FIELDS)
    return {
        "observed_date": date_key,
        "raw_rows": len(rows),
        "security_rows": len(securities),
        "processed_path": str(processed_path),
    }


def _read_symbols(symbols: str | None, symbols_file: str | None) -> list[str]:
    values: list[str] = []
    if symbols:
        values.extend(symbol.strip() for symbol in symbols.split(","))
    if symbols_file:
        for line in Path(symbols_file).read_text(encoding="utf-8").splitlines():
            values.extend(part.strip() for part in line.split(","))
    cleaned: list[str] = []
    seen: set[str] = set()
    for value in values:
        symbol = value.strip()
        if len(symbol) == 6 and symbol.isdigit() and symbol not in seen:
            seen.add(symbol)
            cleaned.append(symbol)
    return cleaned


def sync_financial_indicators(
    root: Path,
    symbols: list[str],
    *,
    as_of: str | None = None,
    start_year: str = "2020",
    retries: int = 1,
    retry_delay: float = 2.0,
    fetcher: Callable[[str, str], list[dict[str, Any]]] | None = None,
) -> dict[str, Any]:
    if not symbols:
        raise ValueError("请通过 --symbols 或 --symbols-file 提供至少一个六位股票代码")

    observed_at = (
        datetime.fromisoformat(as_of).replace(tzinfo=UTC)
        if as_of
        else datetime.now(UTC)
    )
    date_key = observed_at.strftime("%Y%m%d")
    all_fundamentals: list[dict[str, Any]] = []
    failures: dict[str, str] = {}
    for symbol in symbols:
        try:
            rows = _fetch_with_retries(
                lambda symbol=symbol: (fetcher or _fetch_financial_indicator_rows)(symbol, start_year),
                retries=retries,
                retry_delay=retry_delay,
            )
        except AkShareUpstreamError as exc:
            failures[symbol] = str(exc)
            continue
        _write_snapshot(
            root
            / "data"
            / "raw"
            / "akshare"
            / date_key
            / "stock_financial_analysis_indicator"
            / f"{symbol}.json",
            "stock_financial_analysis_indicator",
            rows,
        )
        all_fundamentals.extend(normalize_financial_indicator_rows(symbol, rows, observed_at))

    processed_path = root / "data" / "processed" / "akshare_fundamentals" / f"{date_key}.csv"
    _write_csv(processed_path, all_fundamentals, FUNDAMENTAL_FIELDS)
    return {
        "observed_date": date_key,
        "symbols": len(symbols),
        "fundamental_rows": len(all_fundamentals),
        "failures": failures,
        "processed_path": str(processed_path),
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="同步 AkShare 免费数据源")
    parser.add_argument("command", choices=["spot-valuations", "financial-indicators", "securities"])
    parser.add_argument("--root", default=".")
    parser.add_argument("--date", help="估值快照日期 YYYYMMDD；默认今日UTC")
    parser.add_argument("--as-of", help="基本面观察时间 YYYY-MM-DD 或 ISO datetime；默认当前UTC")
    parser.add_argument("--symbols", help="逗号分隔六位股票代码，仅用于 financial-indicators")
    parser.add_argument("--symbols-file", help="包含六位股票代码的文本/CSV文件，仅用于 financial-indicators")
    parser.add_argument("--start-year", default="2020", help="财务指标起始年份，默认2020")
    parser.add_argument("--retries", type=int, default=2, help="上游请求失败后的重试次数，默认2")
    parser.add_argument("--retry-delay", type=float, default=2.0, help="每次重试前等待秒数，默认2")
    args = parser.parse_args()
    project = Path(args.root).resolve()
    try:
        if args.command == "spot-valuations":
            result = sync_spot_valuations(
                project,
                args.date,
                retries=args.retries,
                retry_delay=args.retry_delay,
            )
        elif args.command == "securities":
            result = sync_securities(
                project,
                as_of=args.as_of,
                retries=args.retries,
                retry_delay=args.retry_delay,
            )
        elif args.command == "financial-indicators":
            result = sync_financial_indicators(
                project,
                _read_symbols(args.symbols, args.symbols_file),
                as_of=args.as_of,
                start_year=args.start_year,
                retries=args.retries,
                retry_delay=args.retry_delay,
            )
    except (AkShareUnavailable, AkShareUpstreamError, ValueError) as exc:
        raise SystemExit(str(exc)) from exc
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()

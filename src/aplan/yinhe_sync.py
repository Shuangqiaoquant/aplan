from __future__ import annotations

import argparse
import csv
import json
import math
import os
from dataclasses import dataclass
from datetime import UTC, datetime
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


def _rows(dataframe: Any) -> list[dict[str, Any]]:
    if dataframe is None:
        return []
    if hasattr(dataframe, "to_dict"):
        return list(dataframe.to_dict(orient="records"))
    return list(dataframe)


def _strip_suffix(code: str) -> str:
    value = str(code or "").strip()
    if "." in value:
        value = value.split(".", 1)[0]
    return value.zfill(6)


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
    output: list[dict[str, Any]] = []
    seen: set[str] = set()
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
        if len(symbol) != 6 or not symbol.isdigit() or symbol in seen:
            continue
        name = str(_first_value(row, "证券简称", "A股简称", "name", "security_name") or "").strip()
        if not name:
            continue
        seen.add(symbol)
        output.append(
            {
                "symbol": symbol,
                "name": name,
                "list_date": _parse_date(_first_value(row, "上市日期", "A股上市日期", "list_date")),
                "industry": str(_first_value(row, "所属行业", "industry") or "未知").strip() or "未知",
                "is_st": "1" if "ST" in name.upper() else "0",
                "is_delisting_risk": "1" if "退" in name else "0",
            }
        )
    return sorted(output, key=lambda item: item["symbol"])


def normalize_daily_rows(rows: list[dict[str, Any]], trade_date: str) -> list[dict[str, Any]]:
    output: list[dict[str, Any]] = []
    for row in rows:
        symbol = _strip_suffix(_first_value(row, "证券代码", "security_code", "symbol", "code"))
        if len(symbol) != 6 or not symbol.isdigit():
            continue
        output.append(
            {
                "symbol": symbol,
                "trade_date": str(_first_value(row, "交易日期", "trade_date", "date") or trade_date).replace("-", ""),
                "open": _number(_first_value(row, "开盘价", "open", "open_price")),
                "high": _number(_first_value(row, "最高价", "high", "high_price")),
                "low": _number(_first_value(row, "最低价", "low", "low_price")),
                "close": _number(_first_value(row, "收盘价", "close", "close_price")),
                "volume": _number(_first_value(row, "成交量", "volume", "volume_trade")),
                "turnover": _number(_first_value(row, "成交额", "turnover", "value_trade")),
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
        cfg.force_logout = 1 if self.config.force_logout else 0
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
        for symbol in symbols:
            request = build_kline_request(
                tgw,
                symbol,
                begin_date=trade_date,
                end_date=trade_date,
                interval=interval,
            )
            rows.extend(self.query_kline(request))
        return rows

    def fetch_securities(self, markets: tuple[str, ...] = ("sse", "szse")) -> list[dict[str, Any]]:
        tgw = self._sdk()
        requests = [build_security_info_request(tgw, market) for market in markets]
        return self.query_securities_info(requests)


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
    return {
        "trade_date": trade_date,
        "raw_rows": len(rows),
        "daily_rows": len(daily),
        "processed_path": str(processed_path),
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="同步中国银河星耀数智数据")
    parser.add_argument("command", choices=["securities", "daily"])
    parser.add_argument("--root", default=".")
    parser.add_argument("--env-file", default=".env")
    parser.add_argument("--date", help="交易日，格式 YYYYMMDD；仅 daily 需要")
    parser.add_argument("--as-of", help="观察日期，格式 YYYY-MM-DD；仅 securities 需要")
    parser.add_argument("--symbols", help="逗号分隔六位股票代码；仅 daily 需要")
    parser.add_argument("--symbols-file", help="包含六位股票代码的文本/CSV文件；仅 daily 需要")
    parser.add_argument("--interval", default="day", help="K线周期，默认 day；可用 1m/5m/day/week/month 等")
    args = parser.parse_args()
    root = Path(args.root).resolve()
    try:
        if args.command == "securities":
            result = sync_securities(root, as_of=args.as_of, config=YinheConfig.from_env(args.env_file))
        else:
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
    except (YinheUnavailable, YinheUpstreamError, ValueError) as exc:
        raise SystemExit(str(exc)) from exc
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()

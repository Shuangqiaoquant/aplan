from __future__ import annotations

import argparse
import csv
import json
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from .tushare import TushareClient, TushareError

STOCK_FIELDS = (
    "ts_code",
    "symbol",
    "name",
    "area",
    "industry",
    "market",
    "exchange",
    "list_status",
    "list_date",
    "delist_date",
)

MARKET_FIELDS: dict[str, tuple[str, ...]] = {
    "daily": (
        "ts_code",
        "trade_date",
        "open",
        "high",
        "low",
        "close",
        "pre_close",
        "change",
        "pct_chg",
        "vol",
        "amount",
    ),
    "adj_factor": ("ts_code", "trade_date", "adj_factor"),
    "daily_basic": (
        "ts_code",
        "trade_date",
        "turnover_rate",
        "turnover_rate_f",
        "volume_ratio",
        "pe",
        "pb",
        "total_share",
        "float_share",
        "free_share",
        "total_mv",
        "circ_mv",
    ),
    "stk_limit": ("trade_date", "ts_code", "pre_close", "up_limit", "down_limit"),
    "suspend_d": ("ts_code", "trade_date", "suspend_timing", "suspend_type"),
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
    path.write_text(json.dumps(document, ensure_ascii=False, indent=2), encoding="utf-8")


def rebuild_master_data(root: Path) -> dict[str, int]:
    """从每个状态的最新快照重建清洗表，不访问网络。"""
    all_rows: list[dict[str, Any]] = []
    latest_by_status: dict[str, Path] = {}
    for snapshot in sorted((root / "data" / "raw" / "tushare").glob("*/stock_basic_?.json")):
        latest_by_status[snapshot.stem[-1]] = snapshot
    counts: dict[str, int] = {}
    for saved_status, snapshot in latest_by_status.items():
        saved_rows = json.loads(snapshot.read_text(encoding="utf-8")).get("rows", [])
        counts[saved_status] = len(saved_rows)
        all_rows.extend(saved_rows)
    unique = {row["ts_code"]: row for row in all_rows}
    normalized = sorted(unique.values(), key=lambda row: row["ts_code"])
    _write_csv(root / "data" / "processed" / "tushare_stock_basic.csv", normalized, STOCK_FIELDS)
    counts.setdefault("L", 0)
    counts.setdefault("P", 0)
    counts.setdefault("D", 0)
    counts["total"] = len(normalized)
    return counts


def sync_master_data(client: TushareClient, root: Path, status: str) -> dict[str, int]:
    """按状态同步，并合并本地已有快照；适配低频率账户。"""
    if status not in {"L", "P", "D"}:
        raise ValueError("status 必须是 L、P 或 D")
    snapshot_day = datetime.now(UTC).date().isoformat()
    rows = client.query("stock_basic", fields=STOCK_FIELDS, list_status=status)
    _write_snapshot(
        root / "data" / "raw" / "tushare" / snapshot_day / f"stock_basic_{status}.json",
        "stock_basic",
        rows,
    )
    return rebuild_master_data(root)


def verify(client: TushareClient) -> int:
    rows = client.query(
        "trade_cal",
        fields=("exchange", "cal_date", "is_open", "pretrade_date"),
        exchange="SSE",
        start_date="20260101",
        end_date="20260110",
    )
    return len(rows)


def sync_indices(
    client: TushareClient,
    root: Path,
    codes: tuple[str, ...],
    start_date: str,
    end_date: str,
) -> dict[str, int]:
    fields = (
        "ts_code",
        "trade_date",
        "close",
        "open",
        "high",
        "low",
        "pre_close",
        "change",
        "pct_chg",
        "vol",
        "amount",
    )
    counts: dict[str, int] = {}
    for code in codes:
        rows = client.query(
            "index_daily",
            fields=fields,
            ts_code=code,
            start_date=start_date,
            end_date=end_date,
        )
        rows = sorted(rows, key=lambda row: row["trade_date"])
        safe_code = code.replace(".", "_")
        _write_snapshot(
            root / "data" / "raw" / "tushare" / "indices" / f"{safe_code}.json",
            "index_daily",
            rows,
        )
        _write_csv(
            root / "data" / "processed" / "indices" / f"{safe_code}.csv",
            rows,
            fields,
        )
        counts[code] = len(rows)
    return counts


def _parse_number(value: Any) -> float:
    if value in (None, ""):
        return 0.0
    return float(value)


def build_daily_bars(
    daily_rows: list[dict[str, Any]],
    limit_rows: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """转换为 APlan 格式；Tushare 成交量单位为手、成交额单位为千元。"""
    limits = {row["ts_code"]: row for row in limit_rows}
    output: list[dict[str, Any]] = []
    for row in daily_rows:
        limit = limits.get(row["ts_code"], {})
        open_price = _parse_number(row.get("open"))
        high_price = _parse_number(row.get("high"))
        low_price = _parse_number(row.get("low"))
        pre_close = _parse_number(row.get("pre_close"))
        up_limit = _parse_number(limit.get("up_limit"))
        down_limit = _parse_number(limit.get("down_limit"))
        one_price = abs(open_price - high_price) < 1e-6 and abs(open_price - low_price) < 1e-6
        fallback_up = not up_limit and one_price and pre_close and open_price > pre_close
        fallback_down = not down_limit and one_price and pre_close and open_price < pre_close
        output.append(
            {
                "symbol": str(row["ts_code"]).split(".", 1)[0],
                "trade_date": row["trade_date"],
                "open": open_price,
                "high": high_price,
                "low": low_price,
                "close": _parse_number(row.get("close")),
                "volume": _parse_number(row.get("vol")) * 100,
                "turnover": _parse_number(row.get("amount")) * 1_000,
                "is_suspended": "0",
                # 开盘封板时按不可成交处理；盘中触板不应直接视为无法成交。
                "is_limit_up": "1"
                if (up_limit and open_price >= up_limit - 1e-6) or fallback_up
                else "0",
                "is_limit_down": "1"
                if (down_limit and open_price <= down_limit + 1e-6) or fallback_down
                else "0",
            }
        )
    return sorted(output, key=lambda item: item["symbol"])


def build_daily_valuations(daily_basic_rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """转换 Tushare daily_basic 为 APlan 估值快照；市值沿用 Tushare 万元单位。"""
    output: list[dict[str, Any]] = []
    for row in daily_basic_rows:
        output.append(
            {
                "symbol": str(row["ts_code"]).split(".", 1)[0],
                "trade_date": row["trade_date"],
                "pe": _parse_number(row.get("pe")),
                "pb": _parse_number(row.get("pb")),
                "total_mv": _parse_number(row.get("total_mv")),
                "circ_mv": _parse_number(row.get("circ_mv")),
                "turnover_rate": _parse_number(row.get("turnover_rate")),
                "volume_ratio": _parse_number(row.get("volume_ratio")),
            }
        )
    return sorted(output, key=lambda item: item["symbol"])


def sync_market_day(
    client: TushareClient,
    root: Path,
    trade_date: str,
    datasets: tuple[str, ...],
) -> dict[str, int]:
    if len(trade_date) != 8 or not trade_date.isdigit():
        raise ValueError("trade_date 必须使用 YYYYMMDD")
    unsupported = set(datasets) - set(MARKET_FIELDS)
    if unsupported:
        raise ValueError(f"未知数据集：{', '.join(sorted(unsupported))}")

    snapshot_dir = root / "data" / "raw" / "tushare" / trade_date
    downloaded: dict[str, list[dict[str, Any]]] = {}
    counts: dict[str, int] = {}
    for dataset in datasets:
        try:
            rows = client.query(dataset, fields=MARKET_FIELDS[dataset], trade_date=trade_date)
        except TushareError as exc:
            print(f"警告：{dataset} 未同步：{exc}")
            counts[dataset] = -1
            continue
        downloaded[dataset] = rows
        counts[dataset] = len(rows)
        _write_snapshot(snapshot_dir / f"{dataset}.json", dataset, rows)

    bars = rebuild_market_day(root, trade_date)
    if bars is not None:
        counts["aplan_daily"] = bars
    return counts


def open_trade_dates(
    client: TushareClient,
    start_date: str,
    end_date: str,
    *,
    cache_path: Path | None = None,
) -> list[str]:
    rows: list[dict[str, Any]]
    if cache_path and cache_path.exists():
        cached = json.loads(cache_path.read_text(encoding="utf-8"))
        rows = cached.get("rows", [])
        cached_dates = [str(row.get("cal_date", "")) for row in rows]
        if not cached_dates or min(cached_dates) > start_date or max(cached_dates) < end_date:
            rows = []
    else:
        rows = []
    if not rows:
        rows = client.query(
            "trade_cal",
            fields=("exchange", "cal_date", "is_open", "pretrade_date"),
            exchange="SSE",
            start_date=start_date,
            end_date=end_date,
            is_open="1",
        )
        if cache_path:
            _write_snapshot(cache_path, "trade_cal", rows)
    return sorted(str(row["cal_date"]) for row in rows if str(row.get("is_open")) == "1")


def backfill_market(
    client: TushareClient,
    root: Path,
    start_date: str,
    end_date: str,
    datasets: tuple[str, ...],
    *,
    max_days: int | None = None,
    delay_seconds: float = 0.25,
    calendar_mode: str = "tushare",
    workers: int = 1,
) -> dict[str, int]:
    if calendar_mode == "local-daily":
        dates = [
            path.parent.name
            for path in sorted((root / "data" / "raw" / "tushare").glob("20??????/daily.json"))
            if start_date <= path.parent.name <= end_date
            and int(json.loads(path.read_text(encoding="utf-8")).get("row_count", 0)) > 0
        ]
    elif calendar_mode == "weekdays":
        start = datetime.strptime(start_date, "%Y%m%d").date()
        end = datetime.strptime(end_date, "%Y%m%d").date()
        dates = []
        current = start
        while current <= end:
            if current.weekday() < 5:
                dates.append(current.strftime("%Y%m%d"))
            current += timedelta(days=1)
    else:
        dates = open_trade_dates(
            client,
            start_date,
            end_date,
            cache_path=root / "data" / "raw" / "tushare" / "calendars" / f"SSE_{start_date}_{end_date}.json",
        )
    pending = [
        day
        for day in dates
        if any(
            not (root / "data" / "raw" / "tushare" / day / f"{dataset}.json").exists()
            for dataset in datasets
        )
    ]
    if max_days is not None:
        pending = pending[:max_days]

    summary = {"trade_dates": len(dates), "pending": len(pending), "completed": 0, "failed": 0}
    def process(day: str) -> tuple[str, dict[str, int]]:
        missing = tuple(
            dataset
            for dataset in datasets
            if not (root / "data" / "raw" / "tushare" / day / f"{dataset}.json").exists()
        )
        result = sync_market_day(client, root, day, missing)
        if delay_seconds > 0:
            time.sleep(delay_seconds)
        return day, result

    if workers <= 1:
        results = (process(day) for day in pending)
        for index, (day, counts) in enumerate(results, 1):
            failed = any(count < 0 for count in counts.values())
            summary["failed" if failed else "completed"] += 1
            print(f"[{index}/{len(pending)}] {day}：" + "，".join(f"{k}={v}" for k, v in counts.items()))
            if failed:
                print("遇到接口错误，停止本批次；稍后重复运行会从断点继续。")
                break
    else:
        with ThreadPoolExecutor(max_workers=workers) as executor:
            stop = False
            processed = 0
            for offset in range(0, len(pending), workers):
                chunk = pending[offset : offset + workers]
                futures = {executor.submit(process, day): day for day in chunk}
                for future in as_completed(futures):
                    processed += 1
                    day, counts = future.result()
                    failed = any(count < 0 for count in counts.values())
                    summary["failed" if failed else "completed"] += 1
                    print(
                        f"[{processed}/{len(pending)}] {day}："
                        + "，".join(f"{key}={value}" for key, value in counts.items())
                    )
                    stop = stop or failed
                if stop:
                    print("遇到接口错误，停止本批次；稍后重复运行会从断点继续。")
                    break
    return summary


def rebuild_market_day(root: Path, trade_date: str) -> int | None:
    """使用已有快照生成 APlan 日线；缺少涨跌停数据时相应标志保持为否。"""
    snapshot_dir = root / "data" / "raw" / "tushare" / trade_date
    daily_path = snapshot_dir / "daily.json"
    limit_path = snapshot_dir / "stk_limit.json"
    daily_basic_path = snapshot_dir / "daily_basic.json"
    if daily_basic_path.exists():
        daily_basic_rows = json.loads(daily_basic_path.read_text(encoding="utf-8")).get("rows", [])
        valuations = build_daily_valuations(daily_basic_rows)
        valuation_fields = (
            "symbol",
            "trade_date",
            "pe",
            "pb",
            "total_mv",
            "circ_mv",
            "turnover_rate",
            "volume_ratio",
        )
        _write_csv(
            root / "data" / "processed" / "valuations" / f"{trade_date}.csv",
            valuations,
            valuation_fields,
        )
    if daily_path.exists():
        daily_rows = json.loads(daily_path.read_text(encoding="utf-8")).get("rows", [])
        limit_rows = (
            json.loads(limit_path.read_text(encoding="utf-8")).get("rows", [])
            if limit_path.exists()
            else []
        )
        bars = build_daily_bars(daily_rows, limit_rows)
        bar_fields = (
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
        _write_csv(root / "data" / "processed" / "daily" / f"{trade_date}.csv", bars, bar_fields)
        return len(bars)
    return None


def main() -> None:
    parser = argparse.ArgumentParser(description="同步 Tushare 数据")
    parser.add_argument(
        "command",
        choices=[
            "verify",
            "master",
            "rebuild-master",
            "market-day",
            "rebuild-day",
            "backfill",
            "indices",
        ],
    )
    parser.add_argument("--workers", type=int, default=1, help="并发下载数；低频账户建议 2")
    parser.add_argument(
        "--codes",
        default="000300.SH,000905.SH,399006.SZ",
        help="逗号分隔的指数代码",
    )
    parser.add_argument(
        "--status",
        choices=["L", "P", "D"],
        default="L",
        help="证券状态：L上市、P暂停上市、D退市；调用间隔以账户权限提示为准",
    )
    parser.add_argument("--root", default=".", help="项目根目录")
    parser.add_argument("--env-file", default=".env")
    parser.add_argument("--date", help="交易日，格式 YYYYMMDD")
    parser.add_argument("--start", help="开始日期，格式 YYYYMMDD")
    parser.add_argument("--end", help="结束日期，格式 YYYYMMDD")
    parser.add_argument("--max-days", type=int, help="本次最多处理多少个缺失交易日")
    parser.add_argument("--delay", type=float, default=0.25, help="交易日之间的等待秒数")
    parser.add_argument(
        "--calendar-mode",
        choices=["tushare", "weekdays", "local-daily"],
        default="tushare",
        help="tushare 使用精确交易日历；weekdays 会额外请求法定休市日；local-daily 使用本地已有日线日期",
    )
    parser.add_argument(
        "--datasets",
        default="daily,adj_factor,daily_basic,stk_limit,suspend_d",
        help="逗号分隔：daily,adj_factor,daily_basic,stk_limit,suspend_d",
    )
    args = parser.parse_args()
    try:
        if args.command == "rebuild-master":
            counts = rebuild_master_data(Path(args.root).resolve())
            print(f"已从本地快照重建主数据，共 {counts['total']} 条。")
            return
        if args.command == "rebuild-day":
            if not args.date:
                raise SystemExit("rebuild-day 必须提供 --date YYYYMMDD")
            count = rebuild_market_day(Path(args.root).resolve(), args.date)
            if count is None:
                raise SystemExit(f"没有找到 {args.date} 的 daily 快照")
            print(f"已从本地快照重建 {args.date} 日线，共 {count} 条。")
            return
        client = TushareClient.from_env(args.env_file)
        if args.command == "verify":
            print(f"Tushare 连接正常，交易日历返回 {verify(client)} 行。")
        elif args.command == "indices":
            if not args.start or not args.end:
                raise SystemExit("indices 必须提供 --start 和 --end")
            counts = sync_indices(
                client,
                Path(args.root).resolve(),
                tuple(code.strip() for code in args.codes.split(",") if code.strip()),
                args.start,
                args.end,
            )
            print("指数同步完成：" + "，".join(f"{code}={count}" for code, count in counts.items()))
        elif args.command == "backfill":
            if not args.start or not args.end:
                raise SystemExit("backfill 必须提供 --start 和 --end")
            datasets = tuple(item.strip() for item in args.datasets.split(",") if item.strip())
            summary = backfill_market(
                client,
                Path(args.root).resolve(),
                args.start,
                args.end,
                datasets,
                max_days=args.max_days,
                delay_seconds=args.delay,
                calendar_mode=args.calendar_mode,
                workers=max(1, args.workers),
            )
            print("回填结束：" + "，".join(f"{key}={value}" for key, value in summary.items()))
        elif args.command == "market-day":
            if not args.date:
                raise SystemExit("market-day 必须提供 --date YYYYMMDD")
            datasets = tuple(item.strip() for item in args.datasets.split(",") if item.strip())
            counts = sync_market_day(client, Path(args.root).resolve(), args.date, datasets)
            print("行情同步完成：" + "，".join(f"{name} {count}" for name, count in counts.items()))
        else:
            counts = sync_master_data(client, Path(args.root).resolve(), args.status)
            print(
                f"状态 {args.status} 同步完成；本地主数据："
                f"上市 {counts['L']}，暂停上市 {counts['P']}，退市 {counts['D']}，"
                f"去重后共 {counts['total']}。"
            )
    except TushareError as exc:
        raise SystemExit(str(exc)) from exc


if __name__ == "__main__":
    main()

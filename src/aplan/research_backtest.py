from __future__ import annotations

import argparse
import csv
import json
import math
from collections import defaultdict, deque
from dataclasses import dataclass
from pathlib import Path
from statistics import mean, pstdev
from typing import Any

from .factors import percentile_ranks

PREFIXES = ("000", "001", "002", "003", "300", "301", "600", "601", "603", "605")


@dataclass(frozen=True, slots=True)
class Signal:
    signal_index: int
    signal_date: str
    symbol: str
    rank: int
    score: float
    momentum: float
    volatility: float
    turnover_trend: float


@dataclass(frozen=True, slots=True)
class Result:
    signal_date: str
    symbol: str
    entry_date: str
    exit_date: str
    gross_return: float
    net_return: float
    score: float


def snapshot_dates(root: Path) -> list[str]:
    dates: list[str] = []
    for path in root.glob("20??????/daily.json"):
        document = json.loads(path.read_text(encoding="utf-8"))
        if int(document.get("row_count", 0)) > 0:
            dates.append(path.parent.name)
    return sorted(dates)


def load_daily_rows(root: Path, trade_date: str) -> list[dict[str, Any]]:
    path = root / trade_date / "daily.json"
    return json.loads(path.read_text(encoding="utf-8")).get("rows", [])


def _product_return(values: list[float]) -> float:
    result = 1.0
    for value in values:
        result *= 1 + value
    return result - 1


def generate_signals(
    root: Path,
    dates: list[str],
    *,
    holding_days: int,
    momentum_days: int,
    top_n: int,
    min_history: int = 120,
    min_avg_turnover: float = 50_000_000,
    rebalance_offset: int = 0,
) -> list[Signal]:
    """逐日流式计算；每个时点仅持有此前可见的 120 日历史。"""
    histories: dict[str, deque[tuple[float, float]]] = defaultdict(lambda: deque(maxlen=120))
    signals: list[Signal] = []
    next_signal_index = min_history - 1 + rebalance_offset

    for day_index, trade_date in enumerate(dates):
        for row in load_daily_rows(root, trade_date):
            symbol = str(row["ts_code"]).split(".", 1)[0]
            if not symbol.startswith(PREFIXES):
                continue
            pct_return = float(row.get("pct_chg") or 0) / 100
            turnover_yuan = float(row.get("amount") or 0) * 1_000
            histories[symbol].append((pct_return, turnover_yuan))

        if day_index != next_signal_index:
            continue
        next_signal_index += holding_days

        raw: dict[str, tuple[float, float, float]] = {}
        for symbol, history in histories.items():
            if len(history) < min_history:
                continue
            values = list(history)
            recent_turnover = [item[1] for item in values[-20:]]
            if mean(recent_turnover) < min_avg_turnover:
                continue
            returns = [item[0] for item in values]
            momentum_value = _product_return(returns[-momentum_days:])
            volatility = pstdev(returns[-20:]) * math.sqrt(252)
            old_turnover = mean(item[1] for item in values[-20:-10])
            new_turnover = mean(item[1] for item in values[-10:])
            turnover_trend = new_turnover / old_turnover - 1 if old_turnover else 0
            raw[symbol] = (momentum_value, volatility, turnover_trend)

        common = set(raw)
        momentum_rank = percentile_ranks({s: raw[s][0] for s in common})
        low_vol_rank = percentile_ranks({s: raw[s][1] for s in common}, higher_is_better=False)
        turnover_rank = percentile_ranks({s: raw[s][2] for s in common})
        scored = {
            symbol: 100
            * (
                0.55 * momentum_rank[symbol]
                + 0.25 * low_vol_rank[symbol]
                + 0.20 * turnover_rank[symbol]
            )
            for symbol in common
        }
        for rank, (symbol, score) in enumerate(
            sorted(scored.items(), key=lambda item: item[1], reverse=True)[:top_n],
            1,
        ):
            signals.append(
                Signal(
                    day_index,
                    trade_date,
                    symbol,
                    rank,
                    score,
                    raw[symbol][0],
                    raw[symbol][1],
                    raw[symbol][2],
                )
            )
    return signals


def _one_price(row: dict[str, Any], direction: str) -> bool:
    open_price = float(row["open"])
    flat = abs(open_price - float(row["high"])) < 1e-8 and abs(open_price - float(row["low"])) < 1e-8
    pre_close = float(row.get("pre_close") or open_price)
    return flat and (open_price > pre_close if direction == "up" else open_price < pre_close)


def collect_selected_rows(
    root: Path,
    dates: list[str],
    symbols: set[str],
) -> dict[str, dict[str, dict[str, Any]]]:
    selected: dict[str, dict[str, dict[str, Any]]] = defaultdict(dict)
    for trade_date in dates:
        for row in load_daily_rows(root, trade_date):
            symbol = str(row["ts_code"]).split(".", 1)[0]
            if symbol in symbols:
                selected[symbol][trade_date] = row
    return selected


def calculate_holding_return(
    rows: dict[str, dict[str, Any]],
    dates: list[str],
    entry_index: int,
    target_exit_index: int,
) -> tuple[str, str, float] | None:
    """开盘买入、持有、目标日开盘卖出；停牌或一字跌停则顺延退出。"""
    # 次日无法交易则取消该信号，不能把订单保留到数周后的复牌日。
    if entry_index >= len(dates) or dates[entry_index] not in rows:
        return None
    entry_index_found = entry_index
    entry_date = dates[entry_index_found]
    entry = rows[entry_date]
    if _one_price(entry, "up"):
        return None

    exit_index = next(
        (
            i
            for i in range(max(target_exit_index, entry_index_found + 1), len(dates))
            if dates[i] in rows and not _one_price(rows[dates[i]], "down")
        ),
        None,
    )
    if exit_index is None:
        return None
    exit_date = dates[exit_index]
    exit_row = rows[exit_date]

    growth = float(entry["close"]) / float(entry["open"])
    for index in range(entry_index_found + 1, exit_index):
        row = rows.get(dates[index])
        if row:
            growth *= 1 + float(row.get("pct_chg") or 0) / 100
    growth *= float(exit_row["open"]) / float(exit_row["pre_close"])
    return entry_date, exit_date, growth - 1


def evaluate_signals(
    root: Path,
    dates: list[str],
    signals: list[Signal],
    *,
    holding_days: int,
    commission_rate: float = 0.0003,
    stamp_tax_rate: float = 0.0005,
    slippage_rate: float = 0.001,
) -> list[Result]:
    rows_by_symbol = collect_selected_rows(root, dates, {signal.symbol for signal in signals})
    return evaluate_signals_from_rows(
        dates,
        signals,
        rows_by_symbol,
        holding_days=holding_days,
        commission_rate=commission_rate,
        stamp_tax_rate=stamp_tax_rate,
        slippage_rate=slippage_rate,
    )


def evaluate_signals_from_rows(
    dates: list[str],
    signals: list[Signal],
    rows_by_symbol: dict[str, dict[str, dict[str, Any]]],
    *,
    holding_days: int,
    commission_rate: float = 0.0003,
    stamp_tax_rate: float = 0.0005,
    slippage_rate: float = 0.001,
) -> list[Result]:
    results: list[Result] = []
    for signal in signals:
        outcome = calculate_holding_return(
            rows_by_symbol[signal.symbol],
            dates,
            signal.signal_index + 1,
            signal.signal_index + 1 + holding_days,
        )
        if outcome is None:
            continue
        entry_date, exit_date, gross_return = outcome
        net_growth = (
            (1 + gross_return)
            * (1 - slippage_rate)
            * (1 - commission_rate)
            * (1 - slippage_rate)
            * (1 - commission_rate - stamp_tax_rate)
        )
        results.append(
            Result(
                signal.signal_date,
                signal.symbol,
                entry_date,
                exit_date,
                gross_return,
                net_growth - 1,
                signal.score,
            )
        )
    return results


def summarize(results: list[Result], holding_days: int) -> dict[str, float]:
    if not results:
        return {}
    by_cohort: dict[str, list[float]] = defaultdict(list)
    for result in results:
        by_cohort[result.signal_date].append(result.net_return)
    cohort_returns = [mean(by_cohort[day]) for day in sorted(by_cohort)]
    equity = 1.0
    peak = 1.0
    max_drawdown = 0.0
    for value in cohort_returns:
        equity *= 1 + value
        peak = max(peak, equity)
        max_drawdown = min(max_drawdown, equity / peak - 1)
    dispersion = pstdev(cohort_returns) if len(cohort_returns) > 1 else 0
    return {
        "trades": float(len(results)),
        "cohorts": float(len(cohort_returns)),
        "win_rate": sum(result.net_return > 0 for result in results) / len(results),
        "mean_trade_return": mean(result.net_return for result in results),
        "compound_return": equity - 1,
        "annualized_return": equity ** (252 / (len(cohort_returns) * holding_days)) - 1,
        "max_drawdown": max_drawdown,
        "annualized_sharpe": (
            mean(cohort_returns) / dispersion * math.sqrt(252 / holding_days)
            if dispersion
            else 0
        ),
    }


def write_results(
    output_dir: Path,
    name: str,
    signals: list[Signal],
    results: list[Result],
    metrics: dict[str, float],
) -> None:
    output_dir.mkdir(parents=True, exist_ok=True)
    with (output_dir / f"{name}_trades.csv").open("w", encoding="utf-8", newline="") as handle:
        fields = list(Result.__dataclass_fields__)
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        writer.writerows({field: getattr(row, field) for field in fields} for row in results)
    (output_dir / f"{name}_metrics.json").write_text(
        json.dumps(metrics, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


def render_report(all_metrics: dict[str, dict[str, float]], start: str, end: str) -> str:
    lines = [
        "# APlan 第一轮基线回测",
        "",
        f"数据范围：{start}–{end}",
        "",
        "| 周期 | 交易数 | 组合期数 | 胜率 | 平均单笔 | 复合收益 | 年化收益 | 最大回撤 | 年化 Sharpe |",
        "|---|---:|---:|---:|---:|---:|---:|---:|---:|",
    ]
    labels = {"short": "短线 10日", "swing": "波段 40日", "medium": "中期 120日"}
    for name, metrics in all_metrics.items():
        lines.append(
            f"| {labels[name]} | {int(metrics.get('trades', 0))} | "
            f"{int(metrics.get('cohorts', 0))} | {metrics.get('win_rate', 0):.1%} | "
            f"{metrics.get('mean_trade_return', 0):.2%} | {metrics.get('compound_return', 0):.2%} | "
            f"{metrics.get('annualized_return', 0):.2%} | {metrics.get('max_drawdown', 0):.2%} | "
            f"{metrics.get('annualized_sharpe', 0):.2f} |"
        )
    lines.extend(
        [
            "",
            "## 口径",
            "",
            "- 每期等权选择 10 只；信号于收盘后生成，下一交易日开盘执行。",
            "- 收益使用 `pct_chg` 串接以处理除权，开仓和退出使用真实开盘价。",
            "- 成本包含双边 0.1% 滑点、双边 0.03% 佣金及卖出 0.05% 印花税。",
            "- 一字涨停不买，一字跌停延迟卖出；普通开盘封板因缺少 `stk_limit` 暂不能识别。",
            "- 未纳入历史 ST 状态、行业约束和基准指数，因此结果仅是工程基线。",
            "",
        ]
    )
    return "\n".join(lines)


def main() -> None:
    parser = argparse.ArgumentParser(description="运行第一轮 APlan 基线回测")
    parser.add_argument("--root", default=".")
    parser.add_argument("--output", default="reports/backtest_v1")
    args = parser.parse_args()
    project = Path(args.root).resolve()
    raw_root = project / "data" / "raw" / "tushare"
    dates = snapshot_dates(raw_root)
    configurations = {
        "short": (10, 5),
        "swing": (40, 20),
        "medium": (120, 60),
    }
    all_metrics: dict[str, dict[str, float]] = {}
    for name, (holding_days, momentum_days) in configurations.items():
        print(f"正在计算 {name}...")
        signals = generate_signals(
            raw_root,
            dates,
            holding_days=holding_days,
            momentum_days=momentum_days,
            top_n=10,
        )
        results = evaluate_signals(
            raw_root,
            dates,
            signals,
            holding_days=holding_days,
        )
        metrics = summarize(results, holding_days)
        all_metrics[name] = metrics
        write_results(project / args.output, name, signals, results, metrics)
        print(name, metrics)
    report = render_report(all_metrics, dates[0], dates[-1])
    report_path = project / args.output / "report.md"
    report_path.write_text(report, encoding="utf-8")
    print(f"报告已写入 {report_path}")


if __name__ == "__main__":
    main()

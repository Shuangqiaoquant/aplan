from __future__ import annotations

import csv
import json
import math
from collections import defaultdict
from pathlib import Path
from statistics import mean, pstdev

from .research_backtest import (
    Result,
    calculate_holding_return,
    evaluate_signals,
    generate_signals,
    snapshot_dates,
)

HOLDING_DAYS = 40
MOMENTUM_DAYS = 20
OFFSETS = (0, 10, 20, 30)


def load_index(path: Path) -> dict[str, dict[str, object]]:
    with path.open(encoding="utf-8", newline="") as handle:
        return {row["trade_date"]: row for row in csv.DictReader(handle)}


def cohort_returns(results: list[Result]) -> dict[str, float]:
    groups: dict[str, list[float]] = defaultdict(list)
    for result in results:
        groups[result.signal_date].append(result.net_return)
    return {day: mean(values) for day, values in groups.items()}


def series_metrics(values: list[float], period_days: int = HOLDING_DAYS) -> dict[str, float]:
    if not values:
        return {}
    equity = 1.0
    peak = 1.0
    max_drawdown = 0.0
    for value in values:
        equity *= 1 + value
        peak = max(peak, equity)
        max_drawdown = min(max_drawdown, equity / peak - 1)
    sigma = pstdev(values) if len(values) > 1 else 0
    return {
        "periods": float(len(values)),
        "compound_return": equity - 1,
        "annualized_return": equity ** (252 / (len(values) * period_days)) - 1,
        "max_drawdown": max_drawdown,
        "annualized_sharpe": mean(values) / sigma * math.sqrt(252 / period_days) if sigma else 0,
        "positive_period_rate": sum(value > 0 for value in values) / len(values),
    }


def benchmark_for_signals(
    dates: list[str],
    index_rows: dict[str, dict[str, object]],
    signal_dates: list[str],
) -> dict[str, float]:
    index_by_date = {day: index for index, day in enumerate(dates)}
    output: dict[str, float] = {}
    for signal_date in signal_dates:
        signal_index = index_by_date[signal_date]
        outcome = calculate_holding_return(
            index_rows,
            dates,
            signal_index + 1,
            signal_index + 1 + HOLDING_DAYS,
        )
        if outcome:
            output[signal_date] = outcome[2]
    return output


def yearly_metrics(strategy: dict[str, float], benchmark: dict[str, float]) -> dict[str, dict[str, float]]:
    years: dict[str, list[tuple[float, float]]] = defaultdict(list)
    for day, value in strategy.items():
        if day in benchmark:
            years[day[:4]].append((value, benchmark[day]))
    return {
        year: {
            "periods": float(len(values)),
            "strategy_mean": mean(value[0] for value in values),
            "benchmark_mean": mean(value[1] for value in values),
            "excess_mean": mean(value[0] - value[1] for value in values),
        }
        for year, values in sorted(years.items())
    }


def render_report(records: list[dict[str, object]]) -> str:
    positive_excess = sum(record["excess"]["annualized_return"] > 0 for record in records)
    lines = [
        "# APlan 第二轮：40日策略稳健性验证",
        "",
        "## 结论",
        "",
        f"仅 {positive_excess}/{len(records)} 个调仓起点取得正超额收益。策略对调仓日期高度敏感，"
        "未通过稳健性验证，不进入模拟盘。",
        "",
        "## 不同调仓起点",
        "",
        "| 起点偏移 | 期数 | 策略年化 | 沪深300年化 | 超额年化 | 策略回撤 | 超额Sharpe |",
        "|---:|---:|---:|---:|---:|---:|---:|",
    ]
    for record in records:
        strategy = record["strategy"]
        benchmark = record["benchmark"]
        excess = record["excess"]
        lines.append(
            f"| {record['offset']}日 | {int(strategy['periods'])} | "
            f"{strategy['annualized_return']:.2%} | {benchmark['annualized_return']:.2%} | "
            f"{excess['annualized_return']:.2%} | {strategy['max_drawdown']:.2%} | "
            f"{excess['annualized_sharpe']:.2f} |"
        )
    lines.extend(
        [
            "",
            "## 分年度平均40日收益",
            "",
            "| 起点 | 年份 | 期数 | 策略 | 沪深300 | 平均超额 |",
            "|---:|---:|---:|---:|---:|---:|",
        ]
    )
    for record in records:
        for year, metrics in record["yearly"].items():
            lines.append(
                f"| {record['offset']}日 | {year} | {int(metrics['periods'])} | "
                f"{metrics['strategy_mean']:.2%} | {metrics['benchmark_mean']:.2%} | "
                f"{metrics['excess_mean']:.2%} |"
            )
    lines.extend(
        [
            "",
            "## 限制",
            "",
            "- 四个起点共享同一历史区间，不是四份独立样本；它用于检查调仓日期敏感性。",
            "- 当前只使用沪深300作基准，策略股票池更宽，因此仍包含规模与风格暴露。",
            "- 尚未取得历史ST状态和历史行业分类，本轮不以当前分类替代历史分类。",
            "",
        ]
    )
    return "\n".join(lines)


def main() -> None:
    project = Path(".").resolve()
    raw_root = project / "data" / "raw" / "tushare"
    dates = snapshot_dates(raw_root)
    index_rows = load_index(project / "data" / "processed" / "indices" / "000300_SH.csv")
    records: list[dict[str, object]] = []

    for offset in OFFSETS:
        print(f"正在计算调仓偏移 {offset} 日...")
        signals = generate_signals(
            raw_root,
            dates,
            holding_days=HOLDING_DAYS,
            momentum_days=MOMENTUM_DAYS,
            top_n=10,
            rebalance_offset=offset,
        )
        results = evaluate_signals(raw_root, dates, signals, holding_days=HOLDING_DAYS)
        strategy = cohort_returns(results)
        benchmark = benchmark_for_signals(dates, index_rows, sorted(strategy))
        common_dates = sorted(set(strategy) & set(benchmark))
        strategy_values = [strategy[day] for day in common_dates]
        benchmark_values = [benchmark[day] for day in common_dates]
        excess_values = [
            (1 + strategy[day]) / (1 + benchmark[day]) - 1
            for day in common_dates
        ]
        records.append(
            {
                "offset": offset,
                "strategy": series_metrics(strategy_values),
                "benchmark": series_metrics(benchmark_values),
                "excess": series_metrics(excess_values),
                "yearly": yearly_metrics(strategy, benchmark),
                "trades": len(results),
            }
        )

    output = project / "reports" / "backtest_v2"
    output.mkdir(parents=True, exist_ok=True)
    (output / "metrics.json").write_text(
        json.dumps(records, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    (output / "report.md").write_text(render_report(records), encoding="utf-8")
    print(f"报告已写入 {output / 'report.md'}")


if __name__ == "__main__":
    main()

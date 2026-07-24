from __future__ import annotations

import argparse
import json
import math
from collections import defaultdict, deque
from dataclasses import dataclass, field
from pathlib import Path
from statistics import mean, pstdev
from typing import Any

from .factors import percentile_ranks
from .research_backtest import (
    PREFIXES,
    Signal,
    calculate_holding_return,
    collect_selected_rows,
    evaluate_signals_from_rows,
    load_daily_rows,
    snapshot_dates,
)
from .second_round import load_index

HORIZONS = (5, 10, 20, 30, 60, 90, 120)
MIN_HISTORY = 120
TOP_N = 10
MIN_TURNOVER = 50_000_000


def _product_return(values: list[float]) -> float:
    growth = 1.0
    for value in values:
        growth *= 1 + value
    return growth - 1


def _offsets(horizon: int) -> tuple[int, ...]:
    return tuple(sorted({0, horizon // 4, horizon // 2, 3 * horizon // 4}))


@dataclass(slots=True)
class RollingState:
    count: int = 0
    cumulative_log: deque[float] = field(default_factory=lambda: deque([0.0], maxlen=121))
    cumulative_turnover: deque[float] = field(default_factory=lambda: deque([0.0], maxlen=21))
    returns20: deque[float] = field(default_factory=lambda: deque(maxlen=20))
    return_sum: float = 0.0
    return_square_sum: float = 0.0

    def update(self, value: float, turnover: float) -> None:
        if len(self.returns20) == self.returns20.maxlen:
            removed = self.returns20[0]
            self.return_sum -= removed
            self.return_square_sum -= removed * removed
        self.returns20.append(value)
        self.return_sum += value
        self.return_square_sum += value * value
        self.cumulative_log.append(self.cumulative_log[-1] + math.log1p(max(value, -0.999999)))
        self.cumulative_turnover.append(self.cumulative_turnover[-1] + turnover)
        self.count += 1


def _score(
    histories: dict[str, RollingState],
    momentum_days: int,
) -> list[tuple[str, float, float, float, float]]:
    raw: dict[str, tuple[float, float, float]] = {}
    for symbol, state in histories.items():
        if state.count < MIN_HISTORY or len(state.cumulative_turnover) < 21:
            continue
        turnover20 = state.cumulative_turnover[-1] - state.cumulative_turnover[-21]
        if turnover20 / 20 < MIN_TURNOVER:
            continue
        momentum = math.exp(
            state.cumulative_log[-1] - state.cumulative_log[-momentum_days - 1]
        ) - 1
        variance = max(
            state.return_square_sum / 20 - (state.return_sum / 20) ** 2,
            0,
        )
        volatility = math.sqrt(variance) * math.sqrt(252)
        older = state.cumulative_turnover[-11] - state.cumulative_turnover[-21]
        newer = state.cumulative_turnover[-1] - state.cumulative_turnover[-11]
        turnover_trend = newer / older - 1 if older else 0
        raw[symbol] = (momentum, volatility, turnover_trend)
    symbols = set(raw)
    mom_rank = percentile_ranks({symbol: raw[symbol][0] for symbol in symbols})
    vol_rank = percentile_ranks(
        {symbol: raw[symbol][1] for symbol in symbols},
        higher_is_better=False,
    )
    turnover_rank = percentile_ranks({symbol: raw[symbol][2] for symbol in symbols})
    scored = [
        (
            symbol,
            100 * (0.55 * mom_rank[symbol] + 0.25 * vol_rank[symbol] + 0.20 * turnover_rank[symbol]),
            raw[symbol][0],
            raw[symbol][1],
            raw[symbol][2],
        )
        for symbol in symbols
    ]
    return sorted(scored, key=lambda item: item[1], reverse=True)[:TOP_N]


def generate_all_signals(
    root: Path,
    dates: list[str],
) -> dict[tuple[int, int], list[Signal]]:
    configurations = [(horizon, offset) for horizon in HORIZONS for offset in _offsets(horizon)]
    next_index = {
        config: MIN_HISTORY - 1 + config[1]
        for config in configurations
    }
    output: dict[tuple[int, int], list[Signal]] = defaultdict(list)
    histories: dict[str, RollingState] = defaultdict(RollingState)

    for day_index, trade_date in enumerate(dates):
        for row in load_daily_rows(root, trade_date):
            symbol = str(row["ts_code"]).split(".", 1)[0]
            if symbol.startswith(PREFIXES):
                histories[symbol].update(
                    float(row.get("pct_chg") or 0) / 100,
                    float(row.get("amount") or 0) * 1_000,
                )
        due = [config for config in configurations if day_index == next_index[config]]
        score_cache: dict[int, list[tuple[str, float, float, float, float]]] = {}
        for horizon, offset in due:
            momentum_days = max(3, horizon // 2)
            if momentum_days not in score_cache:
                score_cache[momentum_days] = _score(histories, momentum_days)
            for rank, (symbol, score, momentum, volatility, turnover) in enumerate(
                score_cache[momentum_days],
                1,
            ):
                output[(horizon, offset)].append(
                    Signal(
                        day_index,
                        trade_date,
                        symbol,
                        rank,
                        score,
                        momentum,
                        volatility,
                        turnover,
                    )
                )
            next_index[(horizon, offset)] += horizon
    return output


def _cohorts(results: list[Any]) -> dict[str, float]:
    grouped: dict[str, list[float]] = defaultdict(list)
    for result in results:
        grouped[result.signal_date].append(result.net_return)
    return {day: mean(values) for day, values in grouped.items()}


def _period_metrics(
    strategy: dict[str, float],
    index_rows: dict[str, dict[str, object]],
    dates: list[str],
    horizon: int,
    start_year: int,
    end_year: int,
) -> dict[str, float]:
    index_by_date = {day: index for index, day in enumerate(dates)}
    strategy_values: list[float] = []
    excess_values: list[float] = []
    for day in sorted(strategy):
        year = int(day[:4])
        if not start_year <= year <= end_year:
            continue
        signal_index = index_by_date[day]
        benchmark = calculate_holding_return(
            index_rows,
            dates,
            signal_index + 1,
            signal_index + 1 + horizon,
        )
        if not benchmark:
            continue
        strategy_return = strategy[day]
        strategy_values.append(strategy_return)
        excess_values.append((1 + strategy_return) / (1 + benchmark[2]) - 1)
    if not excess_values:
        return {"periods": 0.0, "annualized_excess": 0.0, "excess_sharpe": 0.0}
    excess_growth = math.prod(1 + value for value in excess_values)
    sigma = pstdev(excess_values) if len(excess_values) > 1 else 0
    return {
        "periods": float(len(excess_values)),
        "mean_strategy": mean(strategy_values),
        "mean_excess": mean(excess_values),
        "annualized_excess": excess_growth ** (252 / (len(excess_values) * horizon)) - 1,
        "excess_sharpe": (
            mean(excess_values) / sigma * math.sqrt(252 / horizon)
            if sigma
            else 0
        ),
        "positive_excess_rate": sum(value > 0 for value in excess_values) / len(excess_values),
    }


def render_report(records: list[dict[str, Any]], selected: int | None) -> str:
    lines = [
        "# APlan 多周期隔离验证",
        "",
        "训练集：2023–2024；验证集：2025–2026。每个周期使用四个调仓起点。",
        "",
        "| 周期 | 训练正超额起点 | 训练平均年化超额 | 验证正超额起点 | 验证平均年化超额 |",
        "|---:|---:|---:|---:|---:|",
    ]
    for record in records:
        lines.append(
            f"| {record['horizon']}日 | {record['train_positive']}/4 | "
            f"{record['train_mean_annualized_excess']:.2%} | {record['validation_positive']}/4 | "
            f"{record['validation_mean_annualized_excess']:.2%} |"
        )
    lines.extend(["", "## 选择结果", ""])
    if selected is None:
        lines.append("训练期没有任何周期满足至少3/4个调仓起点为正超额；不打开验证集选赢家。")
    else:
        candidate = next(record for record in records if record["horizon"] == selected)
        passed = candidate["validation_positive"] >= 3
        lines.append(
            f"训练期选中 {selected} 日周期。验证期"
            f"{'通过' if passed else '未通过'}："
            f"{candidate['validation_positive']}/4 个起点为正超额。"
        )
    lines.extend(
        [
            "",
            "验证集成绩只用于接受或拒绝训练期选中的周期，不用于改选其他周期。",
            "",
        ]
    )
    return "\n".join(lines)


def main() -> None:
    parser = argparse.ArgumentParser(description="运行 APlan 多周期隔离验证")
    parser.add_argument("--source", choices=["tushare", "yinhe"], default="tushare")
    parser.add_argument("--root", default=".")
    parser.add_argument("--start", default="20230101")
    parser.add_argument("--end", default="20260722")
    parser.add_argument(
        "--open-final-holdout",
        action="store_true",
        help="打开冻结协议中的最终留出集；默认不读取其绩效",
    )
    parser.add_argument("--output", help="报告输出目录")
    args = parser.parse_args()
    project = Path(args.root).resolve()
    if args.source == "yinhe":
        from .yinhe_price_baseline import run_yinhe_price_baseline

        result = run_yinhe_price_baseline(
            project,
            start_date=args.start,
            end_date=args.end,
            open_final_holdout=args.open_final_holdout,
            output_dir=Path(args.output).resolve() if args.output else None,
        )
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return

    raw = project / "data" / "raw" / "tushare"
    dates = snapshot_dates(raw)
    print("一次扫描生成全部周期信号...")
    signal_sets = generate_all_signals(raw, dates)
    all_symbols = {
        signal.symbol
        for signals in signal_sets.values()
        for signal in signals
    }
    print(f"收集 {len(all_symbols)} 只入选股票的成交路径...")
    selected_rows = collect_selected_rows(raw, dates, all_symbols)
    index_rows = load_index(project / "data" / "processed" / "indices" / "000300_SH.csv")

    detail: dict[tuple[int, int], dict[str, dict[str, float]]] = {}
    for (horizon, offset), signals in signal_sets.items():
        results = evaluate_signals_from_rows(
            dates,
            signals,
            selected_rows,
            holding_days=horizon,
        )
        cohorts = _cohorts(results)
        detail[(horizon, offset)] = {
            "train": _period_metrics(cohorts, index_rows, dates, horizon, 2023, 2024),
            "validation": _period_metrics(cohorts, index_rows, dates, horizon, 2025, 2026),
        }

    records: list[dict[str, Any]] = []
    for horizon in HORIZONS:
        values = [detail[(horizon, offset)] for offset in _offsets(horizon)]
        records.append(
            {
                "horizon": horizon,
                "train_positive": sum(value["train"]["annualized_excess"] > 0 for value in values),
                "train_mean_annualized_excess": mean(
                    value["train"]["annualized_excess"] for value in values
                ),
                "validation_positive": sum(
                    value["validation"]["annualized_excess"] > 0 for value in values
                ),
                "validation_mean_annualized_excess": mean(
                    value["validation"]["annualized_excess"] for value in values
                ),
                "offsets": {
                    str(offset): detail[(horizon, offset)]
                    for offset in _offsets(horizon)
                },
            }
        )
    eligible = [record for record in records if record["train_positive"] >= 3]
    selected = (
        max(eligible, key=lambda record: record["train_mean_annualized_excess"])["horizon"]
        if eligible
        else None
    )

    output = project / "reports" / "horizon_validation"
    output.mkdir(parents=True, exist_ok=True)
    (output / "metrics.json").write_text(
        json.dumps({"selected": selected, "horizons": records}, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    (output / "report.md").write_text(render_report(records, selected), encoding="utf-8")
    print(f"报告已写入 {output / 'report.md'}")


if __name__ == "__main__":
    main()

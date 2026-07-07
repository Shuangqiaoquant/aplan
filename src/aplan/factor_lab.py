from __future__ import annotations

import argparse
import json
from dataclasses import asdict, dataclass
from datetime import date
from pathlib import Path
from statistics import mean

from math import sqrt
from statistics import pstdev

from .factors import annualized_volatility, momentum, turnover_trend
from .models import DailyBar
from .research_workflow import _load_bars_source


@dataclass(frozen=True, slots=True)
class FactorEvaluation:
    factor: str
    horizon_days: int
    sample_dates: int
    sample_pairs: int
    top_average_return: float | None
    bottom_average_return: float | None
    spread: float | None
    verdict: str

    def to_dict(self) -> dict[str, object]:
        return asdict(self)


def _histories_by_symbol(bars: list[DailyBar]) -> dict[str, list[DailyBar]]:
    histories: dict[str, list[DailyBar]] = {}
    for bar in sorted(bars, key=lambda item: (item.symbol, item.trade_date)):
        histories.setdefault(bar.symbol, []).append(bar)
    return histories


def _factor_value(name: str, history: list[DailyBar]) -> float | None:
    if name == "momentum20":
        return momentum(history, 20)
    if name == "momentum60":
        return momentum(history, 60)
    if name == "turnover_trend20":
        return turnover_trend(history, 20)
    if name == "low_vol20":
        value = annualized_volatility(history, 20)
        return -value if value is not None else None
    raise ValueError(f"未知因子：{name}")


def _factor_value_at(name: str, history: list[DailyBar], index: int) -> float | None:
    if name == "momentum20":
        return None if index < 20 else history[index].close / history[index - 20].close - 1
    if name == "momentum60":
        return None if index < 60 else history[index].close / history[index - 60].close - 1
    if name == "turnover_trend20":
        if index < 19:
            return None
        values = [bar.turnover for bar in history[index - 19 : index + 1]]
        baseline = sum(values[:10]) / 10
        return None if baseline == 0 else (sum(values[10:]) / 10) / baseline - 1
    if name == "low_vol20":
        if index < 20:
            return None
        closes = [bar.close for bar in history[index - 20 : index + 1]]
        returns = [closes[i] / closes[i - 1] - 1 for i in range(1, len(closes))]
        return -pstdev(returns) * sqrt(252)
    raise ValueError(f"未知因子：{name}")


def evaluate_factor(
    bars: list[DailyBar],
    factor: str,
    *,
    horizon_days: int = 20,
    quantile: float = 0.2,
    min_cross_section: int = 20,
) -> FactorEvaluation:
    if factor not in {"momentum20", "momentum60", "turnover_trend20", "low_vol20"}:
        raise ValueError(f"未知因子：{factor}")
    histories = _histories_by_symbol(bars)
    date_indices = {
        symbol: {bar.trade_date: index for index, bar in enumerate(history)}
        for symbol, history in histories.items()
    }
    by_date: dict[date, dict[str, DailyBar]] = {}
    for bar in bars:
        by_date.setdefault(bar.trade_date, {})[bar.symbol] = bar
    dates = sorted(by_date)
    top_returns: list[float] = []
    bottom_returns: list[float] = []
    sample_dates = 0
    sample_pairs = 0
    for index, current_date in enumerate(dates):
        future_index = index + horizon_days
        if future_index >= len(dates):
            break
        future_date = dates[future_index]
        values: dict[str, float] = {}
        future_returns: dict[str, float] = {}
        for symbol, history in histories.items():
            current_symbol_index = date_indices[symbol].get(current_date)
            future_symbol_index = date_indices[symbol].get(future_date)
            if current_symbol_index is None or future_symbol_index is None:
                continue
            if future_symbol_index <= current_symbol_index:
                continue
            value = _factor_value_at(factor, history, current_symbol_index)
            if value is None:
                continue
            values[symbol] = value
            future_returns[symbol] = history[future_symbol_index].close / history[current_symbol_index].close - 1
        if len(values) < min_cross_section:
            continue
        ordered = sorted(values, key=values.get)
        bucket = max(1, int(len(ordered) * quantile))
        bottom = ordered[:bucket]
        top = ordered[-bucket:]
        top_returns.extend(future_returns[symbol] for symbol in top)
        bottom_returns.extend(future_returns[symbol] for symbol in bottom)
        sample_dates += 1
        sample_pairs += len(top) + len(bottom)
    top_average = mean(top_returns) if top_returns else None
    bottom_average = mean(bottom_returns) if bottom_returns else None
    spread = (
        top_average - bottom_average
        if top_average is not None and bottom_average is not None
        else None
    )
    if spread is None:
        verdict = "insufficient_data"
    elif spread > 0.01:
        verdict = "promising_for_research"
    elif spread < -0.01:
        verdict = "adverse_or_inverted"
    else:
        verdict = "weak_or_flat"
    return FactorEvaluation(
        factor=factor,
        horizon_days=horizon_days,
        sample_dates=sample_dates,
        sample_pairs=sample_pairs,
        top_average_return=top_average,
        bottom_average_return=bottom_average,
        spread=spread,
        verdict=verdict,
    )


def main() -> None:
    parser = argparse.ArgumentParser(description="APlan 因子分层验证")
    parser.add_argument("--root", default=".")
    parser.add_argument("--bars", required=True)
    parser.add_argument("--date", required=True)
    parser.add_argument("--factor", choices=["momentum20", "momentum60", "turnover_trend20", "low_vol20"], required=True)
    parser.add_argument("--horizon-days", type=int, default=20)
    parser.add_argument("--min-cross-section", type=int, default=20)
    parser.add_argument("--output")
    args = parser.parse_args()
    project = Path(args.root).resolve()
    as_of = date.fromisoformat(args.date)
    result = evaluate_factor(
        _load_bars_source(project / args.bars if not Path(args.bars).is_absolute() else args.bars, as_of),
        args.factor,
        horizon_days=args.horizon_days,
        min_cross_section=args.min_cross_section,
    )
    payload = json.dumps(result.to_dict(), ensure_ascii=False, indent=2)
    if args.output:
        output = Path(args.output)
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(payload + "\n", encoding="utf-8")
    else:
        print(payload)


if __name__ == "__main__":
    main()

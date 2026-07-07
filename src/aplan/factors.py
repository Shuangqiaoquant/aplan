from __future__ import annotations

from math import sqrt
from statistics import mean, pstdev

from .models import DailyBar


def momentum(history: list[DailyBar], lookback: int) -> float | None:
    if len(history) <= lookback:
        return None
    return history[-1].close / history[-lookback - 1].close - 1


def annualized_volatility(history: list[DailyBar], lookback: int = 20) -> float | None:
    if len(history) <= lookback:
        return None
    closes = [bar.close for bar in history[-lookback - 1 :]]
    returns = [closes[i] / closes[i - 1] - 1 for i in range(1, len(closes))]
    return pstdev(returns) * sqrt(252)


def turnover_trend(history: list[DailyBar], lookback: int = 20) -> float | None:
    if len(history) < lookback:
        return None
    values = [bar.turnover for bar in history[-lookback:]]
    half = lookback // 2
    baseline = mean(values[:half])
    return None if baseline == 0 else mean(values[half:]) / baseline - 1


def percentile_ranks(values: dict[str, float], *, higher_is_better: bool = True) -> dict[str, float]:
    """横截面分位排名；单标的返回中性分 0.5。"""
    if not values:
        return {}
    if len(values) == 1:
        return {next(iter(values)): 0.5}
    ordered = sorted(values, key=values.get, reverse=not higher_is_better)
    denominator = len(ordered) - 1
    return {symbol: rank / denominator for rank, symbol in enumerate(ordered)}


def score_candidates(
    histories: dict[str, list[DailyBar]],
    *,
    horizon: str,
    momentum_days: int,
) -> dict[str, float]:
    """简单可解释基线：动量 55%、低波动 25%、成交额趋势 20%。"""
    return {
        symbol: sum(components.values())
        for symbol, components in score_candidate_components(
            histories,
            horizon=horizon,
            momentum_days=momentum_days,
        ).items()
    }


def score_candidate_components(
    histories: dict[str, list[DailyBar]],
    *,
    horizon: str,
    momentum_days: int,
) -> dict[str, dict[str, float]]:
    """v0.1 研究评分分项；缺失的基本面/估值/事件证据不得臆造为高分。"""
    mom = {s: value for s, h in histories.items() if (value := momentum(h, momentum_days)) is not None}
    vol = {s: value for s, h in histories.items() if (value := annualized_volatility(h)) is not None}
    flow = {s: value for s, h in histories.items() if (value := turnover_trend(h)) is not None}
    common = set(mom) & set(vol) & set(flow)
    mom_rank = percentile_ranks({s: mom[s] for s in common})
    vol_rank = percentile_ranks({s: vol[s] for s in common}, higher_is_better=False)
    flow_rank = percentile_ranks({s: flow[s] for s in common})
    return {
        s: {
            "relative_strength": 30 * mom_rank[s],
            "trend_drawdown": 20 * vol_rank[s],
            "liquidity_flow": 15 * flow_rank[s],
            "quality_catalyst": 5,
            "valuation_risk": 3,
            "execution_fit": 7,
        }
        for s in common
    }

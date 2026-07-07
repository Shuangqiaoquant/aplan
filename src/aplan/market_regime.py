from __future__ import annotations

from dataclasses import dataclass
from statistics import median

from .models import DailyBar


@dataclass(frozen=True, slots=True)
class MarketRegime:
    label: str
    breadth_above_ma20: float
    median_momentum20: float
    sample_size: int
    score_cap: float | None
    reason: str


def assess_market_regime(
    histories: dict[str, list[DailyBar]],
    *,
    min_sample: int = 50,
) -> MarketRegime:
    """用内部股票池估计市场环境；不依赖指数数据，避免外部缺失时沉默失效。"""
    above_ma20 = 0
    momentums: list[float] = []
    sample = 0
    for history in histories.values():
        if len(history) < 21:
            continue
        recent = history[-20:]
        ma20 = sum(bar.close for bar in recent) / 20
        latest = history[-1].close
        above_ma20 += int(latest >= ma20)
        momentums.append(latest / history[-21].close - 1)
        sample += 1
    if sample < min_sample:
        return MarketRegime(
            label="insufficient",
            breadth_above_ma20=0.0,
            median_momentum20=0.0,
            sample_size=sample,
            score_cap=None,
            reason=f"市场环境样本不足：{sample}/{min_sample}",
        )
    breadth = above_ma20 / sample
    med_mom = median(momentums)
    if breadth < 0.25 and med_mom < -0.08:
        return MarketRegime(
            label="stress",
            breadth_above_ma20=breadth,
            median_momentum20=med_mom,
            sample_size=sample,
            score_cap=64.0,
            reason=f"压力市：仅 {breadth:.1%} 股票站上20日均线，20日中位动量 {med_mom:.1%}",
        )
    if breadth < 0.35 and med_mom < -0.03:
        return MarketRegime(
            label="weak",
            breadth_above_ma20=breadth,
            median_momentum20=med_mom,
            sample_size=sample,
            score_cap=74.0,
            reason=f"弱市：仅 {breadth:.1%} 股票站上20日均线，20日中位动量 {med_mom:.1%}",
        )
    if breadth > 0.60 and med_mom > 0.03:
        return MarketRegime(
            label="strong",
            breadth_above_ma20=breadth,
            median_momentum20=med_mom,
            sample_size=sample,
            score_cap=None,
            reason=f"强市：{breadth:.1%} 股票站上20日均线，20日中位动量 {med_mom:.1%}",
        )
    return MarketRegime(
        label="neutral",
        breadth_above_ma20=breadth,
        median_momentum20=med_mom,
        sample_size=sample,
        score_cap=None,
        reason=f"中性市场：{breadth:.1%} 股票站上20日均线，20日中位动量 {med_mom:.1%}",
    )

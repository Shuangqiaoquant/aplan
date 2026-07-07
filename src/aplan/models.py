from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime


@dataclass(frozen=True, slots=True)
class DailyBar:
    symbol: str
    trade_date: date
    open: float
    high: float
    low: float
    close: float
    volume: float
    turnover: float
    is_suspended: bool = False
    is_limit_up: bool = False
    is_limit_down: bool = False

    def __post_init__(self) -> None:
        if min(self.open, self.high, self.low, self.close) <= 0:
            raise ValueError("价格必须为正数")
        if self.high < max(self.open, self.close) or self.low > min(self.open, self.close):
            raise ValueError("OHLC 数据不一致")


@dataclass(frozen=True, slots=True)
class ValuationSnapshot:
    symbol: str
    trade_date: date
    pe: float
    pb: float
    total_mv: float
    circ_mv: float
    turnover_rate: float
    volume_ratio: float


@dataclass(frozen=True, slots=True)
class FundamentalSnapshot:
    symbol: str
    period_end: date
    publish_time: datetime
    source: str
    source_hash: str
    revenue_growth: float | None = None
    net_profit_growth: float | None = None
    roe: float | None = None
    operating_cashflow_to_profit: float | None = None
    debt_to_assets: float | None = None


@dataclass(frozen=True, slots=True)
class Security:
    symbol: str
    name: str
    list_date: date
    industry: str = "未知"
    is_st: bool = False
    is_delisting_risk: bool = False


@dataclass(frozen=True, slots=True)
class Candidate:
    symbol: str
    score: float
    horizon: str
    reasons: tuple[str, ...]
    risks: tuple[str, ...]
    score_breakdown: tuple[tuple[str, float], ...] = ()
    confidence: float = 0.0
    decision_band: str = "watch_only"
    entry_style: str = "unclassified"
    evidence_gaps: tuple[str, ...] = ()
    invalidation: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class Trade:
    symbol: str
    signal_date: date
    entry_date: date
    exit_date: date
    entry_price: float
    exit_price: float
    net_return: float

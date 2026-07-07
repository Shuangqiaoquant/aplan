from __future__ import annotations

from datetime import date
from statistics import mean

from .models import DailyBar, Security

DEFAULT_PREFIXES = ("000", "001", "002", "003", "300", "301", "600", "601", "603", "605")


def eligible_securities(
    securities: list[Security],
    bars: list[DailyBar],
    as_of: date,
    *,
    prefixes: tuple[str, ...] = DEFAULT_PREFIXES,
    min_listing_days: int = 120,
    min_avg_turnover: float = 50_000_000,
    liquidity_window: int = 20,
) -> list[Security]:
    """只使用 as_of 当日及以前的数据构造股票池。"""
    histories: dict[str, list[DailyBar]] = {}
    for bar in bars:
        if bar.trade_date <= as_of:
            histories.setdefault(bar.symbol, []).append(bar)

    result: list[Security] = []
    for security in securities:
        history = histories.get(security.symbol, [])
        recent = history[-liquidity_window:]
        listed_days = (as_of - security.list_date).days
        liquid = len(recent) == liquidity_window and mean(b.turnover for b in recent) >= min_avg_turnover
        if (
            security.symbol.startswith(prefixes)
            and listed_days >= min_listing_days
            and not security.is_st
            and not security.is_delisting_risk
            and liquid
        ):
            result.append(security)
    return result


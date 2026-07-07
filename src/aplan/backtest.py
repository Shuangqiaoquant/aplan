from __future__ import annotations

from collections.abc import Callable
from datetime import date

from .models import DailyBar, Trade

SignalFunction = Callable[[date, dict[str, list[DailyBar]]], list[str]]


def run_fixed_horizon_backtest(
    bars: list[DailyBar],
    signal_dates: list[date],
    signal_fn: SignalFunction,
    *,
    holding_days: int,
    commission_rate: float = 0.0003,
    stamp_tax_rate: float = 0.0005,
    slippage_rate: float = 0.001,
) -> list[Trade]:
    """信号日收盘后决策，下一可交易日开盘买入，固定周期后开盘卖出。

    涨停不可买、跌停不可卖；若到期无法卖出，顺延至下一可卖日。
    """
    by_symbol: dict[str, list[DailyBar]] = {}
    for bar in sorted(bars, key=lambda item: (item.symbol, item.trade_date)):
        by_symbol.setdefault(bar.symbol, []).append(bar)

    trades: list[Trade] = []
    for signal_date in sorted(signal_dates):
        visible = {
            symbol: [bar for bar in history if bar.trade_date <= signal_date]
            for symbol, history in by_symbol.items()
        }
        for symbol in signal_fn(signal_date, visible):
            future = [bar for bar in by_symbol.get(symbol, []) if bar.trade_date > signal_date]
            entry_index = next(
                (i for i, bar in enumerate(future) if not bar.is_suspended and not bar.is_limit_up),
                None,
            )
            if entry_index is None:
                continue
            entry = future[entry_index]
            target_index = entry_index + holding_days
            if target_index >= len(future):
                continue
            exit_bar = next(
                (
                    bar
                    for bar in future[target_index:]
                    if not bar.is_suspended and not bar.is_limit_down
                ),
                None,
            )
            if exit_bar is None:
                continue
            entry_price = entry.open * (1 + slippage_rate)
            exit_price = exit_bar.open * (1 - slippage_rate)
            gross = exit_price / entry_price - 1
            costs = commission_rate * 2 + stamp_tax_rate
            trades.append(
                Trade(
                    symbol=symbol,
                    signal_date=signal_date,
                    entry_date=entry.trade_date,
                    exit_date=exit_bar.trade_date,
                    entry_price=entry_price,
                    exit_price=exit_price,
                    net_return=gross - costs,
                )
            )
    return trades


def summarize(trades: list[Trade]) -> dict[str, float]:
    if not trades:
        return {"trades": 0.0, "win_rate": 0.0, "mean_return": 0.0, "compounded_return": 0.0}
    compounded = 1.0
    for trade in trades:
        compounded *= 1 + trade.net_return
    return {
        "trades": float(len(trades)),
        "win_rate": sum(t.net_return > 0 for t in trades) / len(trades),
        "mean_return": sum(t.net_return for t in trades) / len(trades),
        "compounded_return": compounded - 1,
    }


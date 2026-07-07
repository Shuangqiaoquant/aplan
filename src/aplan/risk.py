from __future__ import annotations

import math
from dataclasses import asdict, dataclass
from enum import StrEnum

from .portfolio import PortfolioState
from .strategy import SignalIntent, UnifiedSignal


class OrderSide(StrEnum):
    BUY = "buy"
    SELL = "sell"


@dataclass(frozen=True, slots=True)
class RiskPolicy:
    max_positions: int = 10
    max_single_weight: float = 0.10
    max_industry_weight: float = 0.30
    max_gross_weight: float = 0.95
    min_cash_weight: float = 0.05
    max_daily_turnover: float = 0.30
    max_drawdown: float = 0.15
    lot_size: int = 100


@dataclass(frozen=True, slots=True)
class ProposedOrder:
    symbol: str
    side: OrderSide
    quantity: int
    reference_price: float
    estimated_value: float
    resulting_weight: float
    signal_id: str
    reason: str


@dataclass(slots=True)
class RiskDecision:
    accepted_orders: list[ProposedOrder]
    rejected: list[dict[str, str]]
    warnings: list[str]
    circuit_breaker: bool
    estimated_turnover: float

    def to_dict(self) -> dict[str, object]:
        return {
            "accepted_orders": [asdict(order) for order in self.accepted_orders],
            "rejected": self.rejected,
            "warnings": self.warnings,
            "circuit_breaker": self.circuit_breaker,
            "estimated_turnover": self.estimated_turnover,
        }


def assess_portfolio_risk(
    portfolio: PortfolioState,
    *,
    policy: RiskPolicy = RiskPolicy(),
) -> list[str]:
    warnings: list[str] = []
    nav = portfolio.nav
    if nav <= 0:
        return ["组合净值无效，无法评估风险"]
    if portfolio.drawdown <= -policy.max_drawdown:
        warnings.append(f"组合回撤 {portfolio.drawdown:.1%} 已触发熔断阈值 {-policy.max_drawdown:.1%}")
    gross_weight = sum(position.market_value for position in portfolio.positions.values()) / nav
    if gross_weight > policy.max_gross_weight:
        warnings.append(f"总仓位 {gross_weight:.1%} 超过上限 {policy.max_gross_weight:.1%}")
    cash_weight = portfolio.cash / nav
    if cash_weight < policy.min_cash_weight:
        warnings.append(f"现金比例 {cash_weight:.1%} 低于下限 {policy.min_cash_weight:.1%}")
    for symbol, position in sorted(portfolio.positions.items()):
        weight = position.market_value / nav
        if weight > policy.max_single_weight:
            warnings.append(f"{symbol} 单票仓位 {weight:.1%} 超过上限 {policy.max_single_weight:.1%}")
        if weight >= 0.50:
            warnings.append(f"{symbol} 单票仓位达到 {weight:.1%}，属于高度集中风险")
    return warnings


def _round_lot(quantity: float, lot_size: int) -> int:
    return max(0, math.floor(quantity / lot_size) * lot_size)


def plan_orders(
    portfolio: PortfolioState,
    signals: list[UnifiedSignal],
    prices: dict[str, float],
    *,
    industries: dict[str, str] | None = None,
    policy: RiskPolicy = RiskPolicy(),
) -> RiskDecision:
    industries = industries or {}
    nav = portfolio.nav
    if nav <= 0:
        raise ValueError("组合净值必须为正")
    circuit_breaker = portfolio.drawdown <= -policy.max_drawdown
    rejected: list[dict[str, str]] = []
    warnings: list[str] = []

    unique: dict[str, UnifiedSignal] = {}
    for signal in sorted(signals, key=lambda item: item.score, reverse=True):
        if not signal.actionable:
            rejected.append({"signal_id": signal.signal_id, "reason": "信号不可执行"})
            continue
        if signal.symbol in unique:
            rejected.append({"signal_id": signal.signal_id, "reason": "同一证券存在重复信号"})
            continue
        unique[signal.symbol] = signal

    target_weights: dict[str, tuple[float, UnifiedSignal]] = {}
    unchanged_positions = {
        symbol: position
        for symbol, position in portfolio.positions.items()
        if symbol not in unique
    }
    unchanged_gross = sum(position.market_value for position in unchanged_positions.values()) / nav
    industry_weights: dict[str, float] = {}
    for position in unchanged_positions.values():
        industry = industries.get(position.symbol, position.industry)
        if industry != "unknown":
            industry_weights[industry] = (
                industry_weights.get(industry, 0.0) + position.market_value / nav
            )
    enter_count = 0
    for symbol, signal in unique.items():
        current_quantity = portfolio.positions.get(symbol).quantity if symbol in portfolio.positions else 0
        if signal.intent in {SignalIntent.EXIT, SignalIntent.REDUCE}:
            requested = 0.0 if signal.intent == SignalIntent.EXIT else signal.target_weight
        else:
            if circuit_breaker:
                rejected.append({"signal_id": signal.signal_id, "reason": "组合回撤熔断，禁止新增仓位"})
                continue
            requested = signal.target_weight
            if current_quantity == 0:
                enter_count += 1
                if len(portfolio.positions) + enter_count > policy.max_positions:
                    rejected.append({"signal_id": signal.signal_id, "reason": "超过最大持仓数量"})
                    continue
        weight = min(requested, policy.max_single_weight)
        industry = industries.get(symbol, "unknown")
        if industry != "unknown":
            allowed = policy.max_industry_weight - industry_weights.get(industry, 0.0)
            weight = max(0.0, min(weight, allowed))
            industry_weights[industry] = industry_weights.get(industry, 0.0) + weight
        target_weights[symbol] = (weight, signal)

    gross = unchanged_gross + sum(weight for weight, _ in target_weights.values())
    gross_limit = min(policy.max_gross_weight, 1 - policy.min_cash_weight)
    if gross > gross_limit and gross > 0:
        available = max(0.0, gross_limit - unchanged_gross)
        proposed = sum(weight for weight, _ in target_weights.values())
        scale = available / proposed if proposed else 0.0
        target_weights = {
            symbol: (weight * scale, signal)
            for symbol, (weight, signal) in target_weights.items()
        }
        warnings.append(f"目标总仓位按比例缩放至 {gross_limit:.1%}")

    orders: list[ProposedOrder] = []
    for symbol, (target_weight, signal) in target_weights.items():
        price = prices.get(symbol)
        if price is None or price <= 0:
            rejected.append({"signal_id": signal.signal_id, "reason": "缺少有效成交参考价"})
            continue
        current_quantity = portfolio.positions.get(symbol).quantity if symbol in portfolio.positions else 0
        target_quantity = _round_lot(nav * target_weight / price, policy.lot_size)
        delta = target_quantity - current_quantity
        if delta == 0:
            continue
        side = OrderSide.BUY if delta > 0 else OrderSide.SELL
        quantity = abs(delta)
        orders.append(
            ProposedOrder(
                symbol=symbol,
                side=side,
                quantity=quantity,
                reference_price=price,
                estimated_value=quantity * price,
                resulting_weight=target_quantity * price / nav,
                signal_id=signal.signal_id,
                reason=f"目标仓位 {target_weight:.2%}",
            )
        )

    turnover = sum(order.estimated_value for order in orders) / nav
    if turnover > policy.max_daily_turnover:
        buy_orders = sorted(
            (order for order in orders if order.side == OrderSide.BUY),
            key=lambda order: unique[order.symbol].score,
            reverse=True,
        )
        sell_orders = [order for order in orders if order.side == OrderSide.SELL]
        accepted = list(sell_orders)
        used = sum(order.estimated_value for order in accepted) / nav
        for order in buy_orders:
            addition = order.estimated_value / nav
            if used + addition <= policy.max_daily_turnover:
                accepted.append(order)
                used += addition
            else:
                rejected.append({"signal_id": order.signal_id, "reason": "超过每日换手上限"})
        orders = accepted
        turnover = used
        warnings.append("订单已按信号评分裁剪至每日换手上限")

    return RiskDecision(orders, rejected, warnings, circuit_breaker, turnover)

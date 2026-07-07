from __future__ import annotations

import tempfile
import unittest
from dataclasses import replace
from pathlib import Path

from aplan.portfolio import PortfolioState, PortfolioStore, Position
from aplan.risk import RiskPolicy, assess_portfolio_risk, plan_orders
from aplan.strategy import (
    Evidence,
    SignalIntent,
    StrategyContext,
    StrategyMetadata,
    StrategyStatus,
    new_signal,
)


def signal(symbol: str, weight: float, *, actionable: bool = True):
    metadata = StrategyMetadata(
        "validated-test",
        "1",
        "测试",
        StrategyStatus.VALIDATED,
        approved_for_simulation=True,
    )
    value = new_signal(
        metadata=metadata,
        context=StrategyContext("20260706", Path("."), "a" * 64),
        symbol=symbol,
        intent=SignalIntent.ENTER,
        horizon_days=20,
        score=80,
        confidence=0.7,
        target_weight=weight,
        evidence=(Evidence("test", "证据", "local", "20260706"),),
        risks=("风险",),
        invalidation=("失效",),
    )
    return replace(value, actionable=actionable)


class RiskTests(unittest.TestCase):
    def portfolio(self, *, peak: float = 100_000) -> PortfolioState:
        return PortfolioState("paper", "20260706", 100_000, 100_000, peak)

    def test_non_actionable_signal_is_rejected(self) -> None:
        decision = plan_orders(self.portfolio(), [signal("300001", 0.1, actionable=False)], {"300001": 10})
        self.assertFalse(decision.accepted_orders)
        self.assertEqual(decision.rejected[0]["reason"], "信号不可执行")

    def test_single_weight_and_lot_are_enforced(self) -> None:
        decision = plan_orders(
            self.portfolio(),
            [signal("300001", 0.5)],
            {"300001": 10},
            policy=RiskPolicy(max_daily_turnover=1),
        )
        order = decision.accepted_orders[0]
        self.assertEqual(order.quantity, 1_000)
        self.assertAlmostEqual(order.resulting_weight, 0.1)

    def test_drawdown_blocks_new_entries(self) -> None:
        portfolio = self.portfolio(peak=120_000)
        decision = plan_orders(portfolio, [signal("300001", 0.1)], {"300001": 10})
        self.assertTrue(decision.circuit_breaker)
        self.assertFalse(decision.accepted_orders)

    def test_store_round_trip_and_history(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = PortfolioStore(Path(directory))
            state = self.portfolio()
            current, history = store.save(state, event="test")
            self.assertTrue(current.exists())
            self.assertTrue(history.exists())
            self.assertEqual(store.load("paper").cash, 100_000)  # type: ignore[union-attr]

    def test_assess_portfolio_risk_flags_concentrated_position(self) -> None:
        portfolio = PortfolioState(
            "paper",
            "20260706",
            cash=0,
            initial_capital=100_000,
            peak_nav=100_000,
            positions={
                "600744": Position(
                    "600744",
                    quantity=10_000,
                    average_cost=9,
                    market_price=5.91,
                )
            },
        )

        warnings = assess_portfolio_risk(portfolio)

        self.assertTrue(any("600744 单票仓位" in warning for warning in warnings))
        self.assertTrue(any("高度集中风险" in warning for warning in warnings))
        self.assertTrue(any("现金比例" in warning for warning in warnings))


if __name__ == "__main__":
    unittest.main()

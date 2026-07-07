from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from aplan.strategy import (
    Evidence,
    SignalIntent,
    StrategyContext,
    StrategyMetadata,
    StrategyStatus,
    new_signal,
)
from aplan.strategy_registry import StrategyRegistry


class DummyStrategy:
    def __init__(self, status: StrategyStatus, approved: bool = False) -> None:
        self.metadata = StrategyMetadata(
            "dummy",
            "1.0.0",
            "测试策略",
            status=status,
            approved_for_simulation=approved,
        )

    def generate(self, context: StrategyContext):
        return [
            new_signal(
                metadata=self.metadata,
                context=context,
                symbol="300001",
                intent=SignalIntent.ENTER,
                horizon_days=20,
                score=70,
                confidence=0.6,
                target_weight=0.05,
                evidence=(Evidence("factor", "测试证据", "local:test", context.trade_date),),
                risks=("测试风险",),
                invalidation=("跌破失效价",),
            )
        ]


class StrategyTests(unittest.TestCase):
    def context(self) -> StrategyContext:
        return StrategyContext("20260706", Path(tempfile.gettempdir()), "a" * 64)

    def test_research_strategy_can_never_be_actionable(self) -> None:
        registry = StrategyRegistry()
        registry.register(DummyStrategy(StrategyStatus.RESEARCH))
        run = registry.run(self.context(), allow_simulation=True)[0]
        self.assertFalse(run.execution_allowed)
        self.assertFalse(run.signals[0].actionable)

    def test_validated_approved_strategy_can_enter_simulation(self) -> None:
        registry = StrategyRegistry()
        registry.register(DummyStrategy(StrategyStatus.VALIDATED, approved=True))
        run = registry.run(self.context(), allow_simulation=True)[0]
        self.assertTrue(run.execution_allowed)
        self.assertTrue(run.signals[0].actionable)

    def test_live_approval_requires_simulation_approval(self) -> None:
        with self.assertRaises(ValueError):
            StrategyMetadata(
                "bad",
                "1.0.0",
                "非法策略",
                status=StrategyStatus.VALIDATED,
                approved_for_live=True,
            )


if __name__ == "__main__":
    unittest.main()


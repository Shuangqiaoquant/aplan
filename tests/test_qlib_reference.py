from __future__ import annotations

import csv
import hashlib
import tempfile
import unittest
from collections import deque
from pathlib import Path

from aplan.qlib_reference import (
    Bar,
    FEATURE_NAMES,
    Observation,
    QlibAlpha158LinearLiteReference,
    _next_holdings,
    alpha158_selected20,
)
from aplan.strategy import SignalIntent, StrategyContext
from aplan.strategy_registry import StrategyRegistry


class QlibReferenceTests(unittest.TestCase):
    def test_selected_feature_vector_is_finite_and_named(self) -> None:
        bars = [
            Bar(
                open=10 + index * 0.02,
                high=10.2 + index * 0.02,
                low=9.8 + index * 0.02,
                close=10.05 + index * 0.02,
                volume=1_000_000 + index * 1_000,
                turnover=10_000_000,
            )
            for index in range(61)
        ]
        features = alpha158_selected20(deque(bars, maxlen=61))
        self.assertIsNotNone(features)
        self.assertEqual(len(features or ()), len(FEATURE_NAMES))

    def test_wvma_uses_five_changes_from_six_bars(self) -> None:
        bars = [
            Bar(
                open=10 + index,
                high=11 + index,
                low=9 + index,
                close=10 + index,
                volume=1_000 + index * 100,
                turnover=10_000,
            )
            for index in range(61)
        ]
        features = alpha158_selected20(bars)
        self.assertIsNotNone(features)
        selected = bars[-6:]
        weighted_moves = [
            abs(selected[index].close / selected[index - 1].close - 1)
            * selected[index].volume
            for index in range(1, 6)
        ]
        center = sum(weighted_moves) / len(weighted_moves)
        expected = (
            sum((value - center) ** 2 for value in weighted_moves)
            / len(weighted_moves)
        ) ** 0.5 / center
        self.assertAlmostEqual((features or ())[1], expected)

    def test_topk_dropout_does_not_sell_a_holding_still_above_new_candidate(self) -> None:
        def row(symbol: str) -> Observation:
            return Observation("20240102", symbol, (0.0,) * 20, 0.0, 1.0)

        ranked = [
            (row("000001"), 4.0),
            (row("000002"), 3.0),
            (row("000003"), 2.0),
            (row("000004"), 1.0),
        ]
        result = _next_holdings(
            {"000001", "000002", "000003"},
            ranked,
            topk=3,
            n_drop=1,
        )
        self.assertEqual(result, {"000001", "000002", "000003"})

    def test_research_adapter_emits_watch_only_non_actionable_signals(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            score_path = root / "scores" / "20241008.csv"
            score_path.parent.mkdir(parents=True)
            with score_path.open("w", newline="", encoding="utf-8") as handle:
                writer = csv.DictWriter(
                    handle,
                    fieldnames=(
                        "symbol",
                        "model_score",
                        "rank",
                    ),
                )
                writer.writeheader()
                writer.writerow(
                    {
                        "symbol": "600000",
                        "model_score": "0.2",
                        "rank": "1",
                    }
                )
            context = StrategyContext(
                "20241008",
                root,
                hashlib.sha256(score_path.read_bytes()).hexdigest(),
            )
            registry = StrategyRegistry()
            registry.register(QlibAlpha158LinearLiteReference(root))
            run = registry.run(context, allow_simulation=True)[0]
            self.assertFalse(run.execution_allowed)
            self.assertEqual(run.signals[0].intent, SignalIntent.WATCH)
            self.assertEqual(run.signals[0].target_weight, 0)
            self.assertFalse(run.signals[0].actionable)
            self.assertIn("研究", run.blocked_reason or "")


if __name__ == "__main__":
    unittest.main()

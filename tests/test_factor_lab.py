from __future__ import annotations

import unittest
from datetime import date, timedelta

from aplan.factor_lab import evaluate_factor
from aplan.models import DailyBar


def make_symbol(symbol: str, closes: list[float]) -> list[DailyBar]:
    start = date(2026, 1, 1)
    return [
        DailyBar(
            symbol=symbol,
            trade_date=start + timedelta(days=index),
            open=close,
            high=close * 1.01,
            low=close * 0.99,
            close=close,
            volume=1_000_000,
            turnover=100_000_000,
        )
        for index, close in enumerate(closes)
    ]


class FactorLabTests(unittest.TestCase):
    def test_momentum_factor_evaluation_detects_positive_spread(self) -> None:
        bars: list[DailyBar] = []
        for index in range(10):
            if index < 5:
                closes = [10 + step * 0.10 for step in range(40)]
            else:
                closes = [10 - step * 0.03 for step in range(40)]
            bars.extend(make_symbol(f"30000{index}", closes))

        result = evaluate_factor(
            bars,
            "momentum20",
            horizon_days=5,
            quantile=0.2,
            min_cross_section=10,
        )

        self.assertGreater(result.sample_dates, 0)
        self.assertIsNotNone(result.spread)
        self.assertGreater(result.spread or 0, 0)
        self.assertEqual(result.verdict, "promising_for_research")

    def test_unknown_factor_is_rejected(self) -> None:
        with self.assertRaises(ValueError):
            evaluate_factor([], "unknown")


if __name__ == "__main__":
    unittest.main()

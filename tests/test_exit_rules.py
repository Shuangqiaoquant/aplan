from __future__ import annotations

import unittest
from datetime import date, timedelta

from aplan.exit_rules import review_exit
from aplan.models import DailyBar


def make_bars(symbol: str, closes: list[float], start: date = date(2026, 1, 1)) -> list[DailyBar]:
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


class ExitRuleTests(unittest.TestCase):
    def test_deep_loss_and_trend_break_triggers_reduce_or_exit_review(self) -> None:
        bars = make_bars("600744", [9 - index * 0.05 for index in range(62)])

        review = review_exit("600744", bars, bars[-1].trade_date, cost_basis=9)

        self.assertEqual(review.action, "reduce_or_exit_review")
        self.assertEqual(review.risk_level, "high")
        self.assertTrue(any("浮亏" in reason for reason in review.reasons))
        self.assertTrue(any("低于20日均线" in reason for reason in review.reasons))

    def test_intact_trend_keeps_hold_review(self) -> None:
        bars = make_bars("300001", [10 + index * 0.05 for index in range(62)])

        review = review_exit("300001", bars, bars[-1].trade_date, cost_basis=10)

        self.assertEqual(review.action, "hold_review")
        self.assertEqual(review.risk_level, "normal")

    def test_initial_stop_loss_adds_exit_pressure(self) -> None:
        bars = make_bars("300001", [10, 10.1, 10.0, 9.7, 9.2])

        review = review_exit("300001", bars, bars[-1].trade_date, cost_basis=10)

        self.assertTrue(any("初始止损" in reason for reason in review.reasons))
        self.assertIn(review.action, {"reduce_review", "reduce_or_exit_review"})

    def test_trailing_stop_after_profit_adds_exit_pressure(self) -> None:
        bars = make_bars("300001", [10, 11.5, 12, 11, 10.4])

        review = review_exit(
            "300001",
            bars,
            bars[-1].trade_date,
            entry_date=bars[0].trade_date,
        )

        self.assertTrue(any("移动止盈" in reason for reason in review.reasons))
        self.assertIn(review.action, {"reduce_review", "reduce_or_exit_review"})

    def test_time_stop_flags_stale_position(self) -> None:
        bars = make_bars("300001", [10 + (0.001 * index) for index in range(25)])

        review = review_exit(
            "300001",
            bars,
            bars[-1].trade_date,
            entry_date=bars[0].trade_date,
            time_stop_days=20,
            min_return_for_time_stop=0.05,
        )

        self.assertTrue(any("时间止损" in reason for reason in review.reasons))


if __name__ == "__main__":
    unittest.main()

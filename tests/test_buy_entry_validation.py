from __future__ import annotations

import unittest
from datetime import date, timedelta

from aplan.buy_entry_validation import (
    render_validation_report,
    summarize_records,
    validate_candidate_entries,
)
from aplan.models import Candidate, DailyBar


def make_bars(symbol: str, closes: list[float], start: date = date(2026, 1, 1)) -> list[DailyBar]:
    return [
        DailyBar(
            symbol=symbol,
            trade_date=start + timedelta(days=index),
            open=close,
            high=close * 1.02,
            low=close * 0.98,
            close=close,
            volume=1_000_000,
            turnover=100_000_000,
        )
        for index, close in enumerate(closes)
    ]


class BuyEntryValidationTests(unittest.TestCase):
    def test_validate_candidate_entries_records_forward_return_without_future_signal(self) -> None:
        bars = make_bars("300001", [10, 10.2, 10.5, 10.8, 11.0, 11.2])
        candidate = Candidate(
            symbol="300001",
            score=78,
            horizon="swing",
            reasons=("动量强",),
            risks=(),
            decision_band="paper_candidate_if_validated",
            entry_style="breakout_continuation_watch",
        )

        records = validate_candidate_entries(
            [candidate],
            bars,
            bars[0].trade_date,
            horizon_days=3,
            slippage_rate=0,
        )

        self.assertEqual(len(records), 1)
        self.assertEqual(records[0].gate_decision, "paper_trade_confirmed")
        self.assertAlmostEqual(records[0].forward_return, 11.0 / 10.2 - 1)

    def test_research_candidate_is_counted_as_blocked_missed_or_avoided(self) -> None:
        bars = make_bars("300001", [10, 10.2, 10.5, 10.8, 11.0, 11.2])
        candidate = Candidate(
            symbol="300001",
            score=70,
            horizon="swing",
            reasons=("动量强",),
            risks=(),
            decision_band="research_candidate",
            entry_style="breakout_continuation_watch",
        )

        records = validate_candidate_entries([candidate], bars, bars[0].trade_date, horizon_days=3, slippage_rate=0)
        summary = summarize_records(records)
        report = render_validation_report(records, horizon_days=3)

        self.assertEqual(records[0].gate_decision, "watch_only")
        self.assertEqual(summary["blocked_missed_winners"], 1)
        self.assertIn("research_candidate", report)
        self.assertIn("阻断后错过上涨样本", report)


if __name__ == "__main__":
    unittest.main()

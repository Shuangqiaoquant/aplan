from __future__ import annotations

import unittest
from datetime import date

from aplan.models import Candidate, DailyBar
from aplan.pretrade import evaluate_next_day_confirmation, evaluate_pretrade


def bar(
    symbol: str,
    trade_date: date,
    *,
    open_: float,
    high: float,
    low: float,
    close: float,
    turnover: float = 100_000_000,
    is_limit_up: bool = False,
) -> DailyBar:
    return DailyBar(
        symbol=symbol,
        trade_date=trade_date,
        open=open_,
        high=high,
        low=low,
        close=close,
        volume=1_000_000,
        turnover=turnover,
        is_limit_up=is_limit_up,
    )


class PretradeConfirmationTests(unittest.TestCase):
    def candidate(self) -> Candidate:
        return Candidate(
            symbol="300001",
            score=80,
            horizon="swing",
            reasons=("动量强",),
            risks=(),
            decision_band="paper_candidate_if_validated",
            entry_style="breakout_continuation_watch",
        )

    def test_confirms_when_next_day_is_not_chasing_and_holds_signal_close(self) -> None:
        signal = bar("300001", date(2026, 1, 1), open_=10, high=10.8, low=9.8, close=10.5)
        next_day = bar("300001", date(2026, 1, 2), open_=10.7, high=11.1, low=10.4, close=10.9)

        check = evaluate_next_day_confirmation(self.candidate(), signal, next_day)

        self.assertEqual(check.decision, "paper_trade_confirmed")
        self.assertFalse(check.blockers)
        self.assertAlmostEqual(check.gap_open, 10.7 / 10.5 - 1)

    def test_blocks_gap_chase(self) -> None:
        signal = bar("300001", date(2026, 1, 1), open_=10, high=10.8, low=9.8, close=10.5)
        next_day = bar("300001", date(2026, 1, 2), open_=11.2, high=11.5, low=11.0, close=11.3)

        check = evaluate_next_day_confirmation(self.candidate(), signal, next_day)

        self.assertEqual(check.decision, "watch_only")
        self.assertTrue(any("超过追价上限" in blocker for blocker in check.blockers))

    def test_blocks_failed_breakout(self) -> None:
        signal = bar("300001", date(2026, 1, 1), open_=10, high=10.8, low=9.8, close=10.5)
        next_day = bar("300001", date(2026, 1, 2), open_=10.3, high=10.4, low=9.5, close=9.6)

        check = evaluate_next_day_confirmation(self.candidate(), signal, next_day)

        self.assertEqual(check.decision, "watch_only")
        self.assertTrue(any("信号失效" in blocker for blocker in check.blockers))

    def test_pending_when_next_day_data_missing(self) -> None:
        signal = bar("300001", date(2026, 1, 1), open_=10, high=10.8, low=9.8, close=10.5)

        check = evaluate_next_day_confirmation(self.candidate(), signal, None)

        self.assertEqual(check.decision, "pending_confirmation")
        self.assertTrue(any("缺少次日行情" in blocker for blocker in check.blockers))

    def test_research_candidate_cannot_enter_paper_buy_confirmation(self) -> None:
        candidate = Candidate(
            symbol="300001",
            score=70,
            horizon="swing",
            reasons=("动量强",),
            risks=(),
            decision_band="research_candidate",
            entry_style="breakout_continuation_watch",
        )

        check = evaluate_pretrade(candidate)

        self.assertEqual(check.decision, "watch_only")
        self.assertTrue(any("research_candidate" in blocker for blocker in check.blockers))


if __name__ == "__main__":
    unittest.main()

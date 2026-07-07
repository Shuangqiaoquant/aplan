from __future__ import annotations

import unittest
from datetime import date, timedelta
from pathlib import Path
from tempfile import TemporaryDirectory

from aplan.models import Candidate, DailyBar
from aplan.observations import (
    ObservationStore,
    add_feedback,
    register_candidates,
    register_manual_position,
    render_observation_review,
    summarize_observations,
    update_outcomes,
)


def bars(symbol: str, closes: list[float], start: date = date(2026, 1, 1)) -> list[DailyBar]:
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


class ObservationTests(unittest.TestCase):
    def test_register_update_and_feedback_observations(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            signal_date = date(2026, 1, 1)
            candidate = Candidate(
                symbol="300001",
                score=76.5,
                horizon="swing",
                reasons=("动量强",),
                risks=("research_only",),
                decision_band="paper_candidate_if_validated",
                entry_style="breakout_continuation_watch",
            )
            history = bars("300001", [10, 11, 12, 13, 14, 15, 16])

            registered = register_candidates(root, signal_date, [candidate], history)

            self.assertEqual(len(registered), 1)
            self.assertEqual(registered[0].initial_close, 10)
            stored = ObservationStore(root).load()
            self.assertEqual(len(stored), 1)
            self.assertEqual(stored[0].rank, 1)

            updated = update_outcomes(
                root,
                history,
                as_of=date(2026, 1, 7),
                horizons=(5,),
            )

            self.assertAlmostEqual(updated[0].outcomes["5d_close_return"], 0.5)
            self.assertEqual(updated[0].status, "measured")

            feedback = add_feedback(
                root,
                updated[0].observation_id,
                note="模拟买入后按计划观察",
                action="bought",
                realized_return=0.12,
            )

            self.assertEqual(feedback.status, "bought")
            self.assertEqual(feedback.feedback[-1].note, "模拟买入后按计划观察")
            self.assertAlmostEqual(feedback.feedback[-1].realized_return or 0, 0.12)

    def test_register_is_idempotent(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            signal_date = date(2026, 1, 1)
            candidate = Candidate(symbol="300001", score=70, horizon="swing", reasons=(), risks=())
            history = bars("300001", [10, 11])

            register_candidates(root, signal_date, [candidate], history)
            register_candidates(root, signal_date, [candidate], history)

            self.assertEqual(len(ObservationStore(root).load()), 1)

    def test_register_manual_position_risk_case(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)

            observation = register_manual_position(
                root,
                as_of=date(2026, 7, 7),
                symbol="600744",
                cost_basis=9.0,
                current_price=5.91,
                note="用户反馈全仓持有，进入风险处置观察",
            )

            self.assertEqual(observation.symbol, "600744")
            self.assertEqual(observation.decision_band, "manual_risk_review")
            self.assertEqual(observation.status, "risk_review")
            self.assertAlmostEqual(observation.outcomes["unrealized_return"], -0.3433333333)
            stored = ObservationStore(root).load()
            self.assertEqual(len(stored), 1)
            self.assertEqual(stored[0].feedback[0].action, "risk_review")

    def test_observation_review_summarizes_by_entry_style(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            signal_date = date(2026, 1, 1)
            candidates = [
                Candidate(
                    symbol="300001",
                    score=76,
                    horizon="swing",
                    reasons=(),
                    risks=(),
                    decision_band="paper_candidate_if_validated",
                    entry_style="breakout_continuation_watch",
                ),
                Candidate(
                    symbol="300002",
                    score=70,
                    horizon="swing",
                    reasons=(),
                    risks=(),
                    decision_band="research_candidate",
                    entry_style="pullback_in_uptrend_watch",
                ),
            ]
            history = bars("300001", [10, 11, 12, 13, 14, 15]) + bars("300002", [10, 9, 8, 7, 6, 5])
            register_candidates(root, signal_date, candidates, history)
            observations = update_outcomes(root, history, as_of=date(2026, 1, 6), horizons=(5,))

            summary = summarize_observations(observations)
            report = render_observation_review(observations)

            breakout = summary["by_entry_style"]["breakout_continuation_watch"]
            self.assertAlmostEqual(breakout["5d_close_return"]["average"], 0.5)
            self.assertEqual(summary["system_candidates"], 2)
            self.assertEqual(summary["manual_risk_cases"], 0)
            self.assertIn("swing", summary["by_horizon"])
            self.assertIn("breakout_continuation_watch", report)
            self.assertIn("平均收益 50.00%", report)
            self.assertIn("按观察周期", report)


if __name__ == "__main__":
    unittest.main()

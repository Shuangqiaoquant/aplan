from __future__ import annotations

import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from aplan.paper_watch import load_watchlist, update_watchlist_from_insights


class PaperWatchTests(unittest.TestCase):
    def test_update_watchlist_from_insights_is_idempotent(self) -> None:
        with TemporaryDirectory() as tmp:
            project = Path(tmp)
            insights = {
                "candidate_top": {
                    "status": "available",
                    "as_of": "2026-07-07",
                    "items": [
                        {
                            "symbol": "300001",
                            "score": 76.5,
                            "decision_band": "research_candidate",
                            "entry_style": "breakout_continuation_watch",
                            "action": "观察：等待次日确认",
                            "reasons": ["20日动量强"],
                            "risks": ["research_only"],
                        }
                    ],
                }
            }

            first = update_watchlist_from_insights(project, insights)
            second = update_watchlist_from_insights(project, insights)
            records = load_watchlist(project)

            self.assertEqual(first["added"], 1)
            self.assertEqual(second["added"], 0)
            self.assertEqual(second["updated"], 1)
            self.assertEqual(len(records), 1)
            self.assertEqual(records[0].status, "pending_next_day_confirmation")
            self.assertEqual(records[0].target_weight_min, 0.01)
            self.assertEqual(records[0].target_weight_max, 0.03)

    def test_skips_when_candidate_insights_unavailable(self) -> None:
        with TemporaryDirectory() as tmp:
            project = Path(tmp)

            result = update_watchlist_from_insights(
                project,
                {"candidate_top": {"status": "unavailable", "reason": "缺少数据"}},
            )

            self.assertEqual(result["status"], "skipped")
            self.assertEqual(load_watchlist(project), [])


if __name__ == "__main__":
    unittest.main()

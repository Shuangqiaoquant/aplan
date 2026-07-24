from __future__ import annotations

import json
import unittest
from datetime import date, timedelta
from pathlib import Path
from tempfile import TemporaryDirectory

from aplan.models import DailyBar, Security
from aplan.research_funnel import (
    UserConstraints,
    apply_user_constraints,
    run_funnel,
    write_funnel_run,
)


def _history(symbol: str, slope: float, turnover_slope: float) -> list[DailyBar]:
    start = date(2025, 1, 1)
    rows: list[DailyBar] = []
    for index in range(25):
        close = 10 + index * slope
        rows.append(
            DailyBar(
                symbol=symbol,
                trade_date=start + timedelta(days=index),
                open=close,
                high=close * 1.01,
                low=close * 0.99,
                close=close,
                volume=1_000_000,
                turnover=100_000_000 + index * turnover_slope,
            )
        )
    return rows


class ResearchFunnelTests(unittest.TestCase):
    def securities(self) -> list[Security]:
        return [
            Security("300001", "科技强势", date(2020, 1, 1), industry="科技"),
            Security("300002", "科技弱势", date(2020, 1, 1), industry="科技"),
            Security("600001", "消费股票", date(2020, 1, 1), industry="消费"),
            Security("688001", "科创股票", date(2020, 1, 1), industry="科技"),
        ]

    def bars(self) -> list[DailyBar]:
        return (
            _history("300001", 0.25, 2_000_000)
            + _history("300002", 0.05, 500_000)
            + _history("600001", 0.15, 1_000_000)
            + _history("688001", 0.20, 1_500_000)
        )

    def test_user_constraints_only_reduce_scope(self) -> None:
        selected, eliminated = apply_user_constraints(
            self.securities(),
            UserConstraints(industries=("科技",), allow_star=False),
        )

        self.assertEqual([item.symbol for item in selected], ["300001", "300002"])
        self.assertEqual(eliminated, {"industry_constraint": 1, "star_market_disabled": 1})

    def test_funnel_stops_after_broad_screen_without_confirmation(self) -> None:
        run = run_funnel(
            self.securities(),
            self.bars(),
            date(2025, 1, 25),
            strategy_profile="momentum",
            broad_pool_size=3,
            refined_pool_size=2,
            final_top_n=1,
        )
        payload = run.to_dict()

        self.assertEqual(run.status, "awaiting_user_confirmation")
        self.assertEqual(len(run.stages), 2)
        self.assertEqual(run.final_candidates, ())
        self.assertTrue(payload["human_confirmation_required"])
        self.assertFalse(payload["execution_allowed"])
        self.assertEqual(
            next(
                item["availability"]
                for item in payload["data_requirements"]
                if item["dataset"] == "realtime_l1"
            ),
            "on_demand_only",
        )

    def test_confirmed_funnel_writes_final_research_candidates(self) -> None:
        run = run_funnel(
            self.securities(),
            self.bars(),
            date(2025, 1, 25),
            strategy_profile="hybrid",
            constraints=UserConstraints(exclude_symbols=("300002",)),
            broad_pool_size=3,
            refined_pool_size=2,
            final_top_n=1,
            confirmed=True,
            available_datasets={"announcements"},
        )

        self.assertEqual(run.status, "research_candidates_ready")
        self.assertEqual(len(run.stages), 4)
        self.assertEqual(len(run.final_candidates), 1)
        self.assertEqual(run.final_candidates[0].symbol, "300001")
        with TemporaryDirectory() as tmp:
            output = write_funnel_run(Path(tmp), run)
            payload = json.loads(output.read_text(encoding="utf-8"))

        self.assertEqual(payload["mode"], "research_only")
        self.assertFalse(payload["execution_allowed"])
        self.assertEqual(payload["final_candidates"][0]["symbol"], "300001")
        self.assertEqual(
            next(
                item["availability"]
                for item in payload["data_requirements"]
                if item["dataset"] == "announcements"
            ),
            "available",
        )

    def test_funnel_rejects_inverted_stage_sizes(self) -> None:
        with self.assertRaisesRegex(ValueError, "broad_pool_size"):
            run_funnel(
                self.securities(),
                self.bars(),
                date(2025, 1, 25),
                broad_pool_size=2,
                refined_pool_size=3,
                final_top_n=1,
            )


if __name__ == "__main__":
    unittest.main()

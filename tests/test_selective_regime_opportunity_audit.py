from __future__ import annotations

import sys
import unittest
from pathlib import Path
from unittest.mock import patch

from aplan.selective_regime_opportunity_audit import (
    Outcome,
    _breadth,
    _component_names,
    _expert_gate,
    _legacy_gate,
    _market_state,
    _metrics,
    _protocol,
    _ranks,
    _spearman,
    main,
)


class SelectiveRegimeOpportunityAuditTests(unittest.TestCase):
    def test_protocol_hash_and_phase_zero_boundaries(self) -> None:
        project = Path(__file__).resolve().parents[1]

        result = _protocol(project)

        self.assertEqual(
            result["sha256"],
            "a3e6b5afe688a65a5cb5b1d89f9c79d271695b99937b176eb15892d4e2709d3c",
        )
        self.assertFalse(result["document"]["complexity_budget"]["selector_allowed"])

    def test_percentile_ranks_are_deterministic_with_ties(self) -> None:
        result = _ranks({"a": 1.0, "b": 1.0, "c": 3.0})

        self.assertEqual(result["a"], result["b"])
        self.assertEqual(result["c"], 1.0)

    def test_spearman_detects_monotonic_relation(self) -> None:
        self.assertAlmostEqual(
            _spearman([(1.0, 10.0), (2.0, 20.0), (3.0, 30.0)] * 4),
            1.0,
        )

    def test_recovery_has_priority_over_other_states(self) -> None:
        dates = [f"2023{month:02d}{day:02d}" for month in range(1, 13) for day in range(1, 29)]
        dates = dates[:150]
        market = {
            ("000300.SH", day): (100.0, 100.0 + index * 0.01)
            for index, day in enumerate(dates)
        }
        breadth = {day: 0.30 for day in dates}
        breadth[dates[-6]] = 0.20
        breadth[dates[-1]] = 0.35
        states = {dates[-3]: "stress"}

        result = _market_state(
            market,
            dates,
            len(dates) - 1,
            breadth,
            states,
        )

        self.assertEqual(result, "recovery")

    def test_breadth_uses_feature_values_not_symbol_keys(self) -> None:
        eligible = {
            "000001": {"above_ma20": True},
            "000002": {"above_ma20": False},
            "000003": {"above_ma20": True},
        }

        self.assertAlmostEqual(_breadth(eligible), 2 / 3)

    def test_expert_gate_requires_every_frozen_kpi(self) -> None:
        metrics = {
            "observations": 1000,
            "cohorts": 60,
            "matched_control_coverage": 0.95,
            "median_net_matched_excess": 0.01,
            "median_daily_spearman_ic": 0.01,
            "top_decile_minus_bottom_decile_net_matched_spread": 0.01,
            "positive_fold_ratio": 0.75,
            "positive_ic_fold_ratio": 0.75,
            "max_drawdown": 0.24,
            "median_market_excess": 0.01,
            "median_industry_excess": 0.01,
        }

        self.assertEqual(_expert_gate(metrics), "pass_phase0")
        metrics["median_daily_spearman_ic"] = 0.0
        self.assertEqual(_expert_gate(metrics), "reject")

    def test_legacy_gate_requires_beating_each_component(self) -> None:
        metrics = {
            "observations": 300,
            "cohorts": 60,
            "matched_control_coverage": 0.95,
            "median_net_matched_excess": 0.01,
            "positive_fold_ratio": 0.75,
            "max_drawdown": 0.24,
        }

        self.assertEqual(
            _legacy_gate(
                metrics,
                {
                    "price_signal": {"increment": 0.01},
                    "trend_signal": {"increment": 0.01},
                },
            ),
            "pass_phase0",
        )
        self.assertEqual(
            _legacy_gate(
                metrics,
                {
                    "price_signal": {"increment": 0.0},
                    "trend_signal": {"increment": 0.01},
                },
            ),
            "reject",
        )

    def test_fixed_intersection_components_are_not_searched(self) -> None:
        self.assertEqual(
            _component_names("price_AND_trend_AND_a8"),
            ("price_signal", "trend_signal", "a8_signal"),
        )

    def test_metrics_report_one_way_cohort_turnover(self) -> None:
        def outcome(day: str, symbol: str) -> Outcome:
            return Outcome(
                route="price_AND_trend",
                horizon=20,
                signal_date=day,
                symbol=symbol,
                industry_code="801010",
                score=None,
                percentile=None,
                selected=True,
                gross_return=0.01,
                net_return=0.007,
                market_excess=0.005,
                industry_excess=0.004,
                matched_excess=0.003,
            )

        outcomes = [
            outcome("20230403", "000001"),
            outcome("20230403", "000002"),
            outcome("20230404", "000002"),
            outcome("20230404", "000003"),
        ]

        metrics = _metrics(outcomes, expert=False)

        self.assertEqual(metrics["mean_one_way_cohort_turnover"], 0.5)
        self.assertEqual(metrics["median_one_way_cohort_turnover"], 0.5)

    def test_cli_rejects_changed_signal_boundary(self) -> None:
        with patch.object(
            sys,
            "argv",
            ["phase0", "--maximum-signal-date", "20250101"],
        ):
            with self.assertRaisesRegex(SystemExit, "禁止读取2025"):
                main()


if __name__ == "__main__":
    unittest.main()

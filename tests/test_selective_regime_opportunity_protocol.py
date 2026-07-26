from __future__ import annotations

import hashlib
import json
import tomllib
import unittest
from pathlib import Path


class SelectiveRegimeOpportunityProtocolTests(unittest.TestCase):
    def setUp(self) -> None:
        self.project = Path(__file__).resolve().parents[1]
        self.source = self.project / "config" / "selective_regime_opportunity_audit.toml"
        self.lock = json.loads(
            (
                self.project
                / "config"
                / "selective_regime_opportunity_audit.lock.json"
            ).read_text(encoding="utf-8")
        )
        self.protocol = tomllib.loads(self.source.read_text(encoding="utf-8"))

    def test_lock_matches_frozen_protocol(self) -> None:
        self.assertEqual(
            hashlib.sha256(self.source.read_bytes()).hexdigest(),
            self.lock["sha256"],
        )

    def test_2025_and_2026_are_not_read_in_phase_zero(self) -> None:
        self.assertFalse(self.protocol["final_holdout_opened"])
        self.assertEqual(
            self.protocol["time_design"]["forbidden_read_start"],
            "2025-01-01",
        )
        self.assertEqual(
            self.protocol["time_design"]["maximum_signal_date"],
            "2024-10-08",
        )

    def test_complexity_budget_forbids_search_and_selector(self) -> None:
        budget = self.protocol["complexity_budget"]
        self.assertEqual(budget["new_experts"], 2)
        self.assertEqual(budget["fixed_legacy_intersections"], 4)
        self.assertFalse(budget["feature_period_search_allowed"])
        self.assertFalse(budget["market_state_threshold_search_allowed"])
        self.assertFalse(budget["expert_weight_search_allowed"])
        self.assertFalse(budget["interaction_search_allowed"])
        self.assertFalse(budget["nonlinear_model_allowed"])
        self.assertFalse(budget["selector_allowed"])

    def test_rejected_models_are_only_fixed_intersection_controls(self) -> None:
        legacy = self.protocol["legacy_interaction_audit"]
        self.assertEqual(
            legacy["routes"],
            [
                "price_AND_trend",
                "price_AND_a8",
                "trend_AND_a8",
                "price_AND_trend_AND_a8",
            ],
        )
        self.assertFalse(legacy["parameter_or_weight_search"])
        self.assertTrue(
            self.protocol["legacy_intersection_gate"][
                "must_beat_each_component_same_sample"
            ]
        )

    def test_no_model_training_occurs_during_opportunity_audit(self) -> None:
        self.assertTrue(self.protocol["purpose"]["not_a_model_backtest"])
        self.assertFalse(self.protocol["purpose"]["selector_training_allowed"])
        self.assertFalse(self.protocol["purpose"]["portfolio_simulation_allowed"])


if __name__ == "__main__":
    unittest.main()

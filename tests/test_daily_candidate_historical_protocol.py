from __future__ import annotations

import hashlib
import json
import tomllib
import unittest
from pathlib import Path


class DailyCandidateHistoricalProtocolTests(unittest.TestCase):
    def setUp(self) -> None:
        self.project = Path(__file__).resolve().parents[1]
        self.source = (
            self.project
            / "config"
            / "daily_candidate_historical_validation.toml"
        )
        self.lock = json.loads(
            (
                self.project
                / "config"
                / "daily_candidate_historical_validation.lock.json"
            ).read_text(encoding="utf-8")
        )
        self.protocol = tomllib.loads(self.source.read_text(encoding="utf-8"))

    def test_lock_matches_frozen_protocol(self) -> None:
        self.assertEqual(
            hashlib.sha256(self.source.read_bytes()).hexdigest(),
            self.lock["sha256"],
        )

    def test_holdout_is_closed_and_training_is_purged(self) -> None:
        self.assertFalse(self.protocol["final_holdout_opened"])
        self.assertEqual(
            self.protocol["time_design"]["research_as_of_trade_date"],
            "20251231",
        )
        self.assertEqual(self.protocol["time_design"]["purge_trading_days"], 60)

    def test_full_model_has_leave_one_out_ablations(self) -> None:
        self.assertEqual(
            set(self.protocol["variants"]),
            {
                "price_score_only",
                "full_current_model",
                "no_valuation",
                "no_market_regime_cap",
                "no_industry_cap",
                "no_fundamental_cap",
                "no_announcement_cap",
            },
        )

    def test_failed_positive_layers_cannot_reenter_first_pass(self) -> None:
        control = self.protocol["change_control"]
        self.assertFalse(control["positive_fundamental_weight_change_allowed"])
        self.assertFalse(control["positive_announcement_weight_change_allowed"])
        self.assertFalse(control["parameter_search_allowed"])
        self.assertFalse(control["buy_confirmation_allowed"])


if __name__ == "__main__":
    unittest.main()

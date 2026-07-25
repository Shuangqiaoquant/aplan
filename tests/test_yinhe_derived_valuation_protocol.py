from __future__ import annotations

import hashlib
import json
import tomllib
import unittest
from pathlib import Path


class YinheDerivedValuationProtocolTests(unittest.TestCase):
    def setUp(self) -> None:
        self.project = Path(__file__).resolve().parents[1]
        self.source = self.project / "config" / "yinhe_derived_valuation.toml"
        self.lock = json.loads(
            (
                self.project
                / "config"
                / "yinhe_derived_valuation.lock.json"
            ).read_text(encoding="utf-8")
        )
        self.protocol = tomllib.loads(self.source.read_text(encoding="utf-8"))

    def test_lock_matches_frozen_protocol(self) -> None:
        self.assertEqual(
            hashlib.sha256(self.source.read_bytes()).hexdigest(),
            self.lock["sha256"],
        )

    def test_uses_raw_close_and_strict_pit_inputs(self) -> None:
        self.assertFalse(self.protocol["sources"]["adjusted_price_for_valuation"])
        self.assertEqual(
            self.protocol["equity_structure"]["economic_effective_date"],
            "CHANGE_DATE",
        )
        self.assertEqual(
            self.protocol["equity_structure"]["information_available_date"],
            "next_official_trading_day_after_ANN_DATE",
        )

    def test_ttm_formula_requires_all_as_of_components(self) -> None:
        self.assertEqual(
            self.protocol["ttm_net_profit"]["interim_formula"],
            "current_ytd_net_profit + prior_available_annual_net_profit - prior_year_same_period_ytd_net_profit",
        )
        self.assertTrue(
            self.protocol["ttm_net_profit"]["require_all_interim_components"]
        )
        self.assertEqual(
            self.protocol["financials"]["missing_required_component"],
            "leave derived metric null",
        )

    def test_no_provider_mix_or_imputation_or_2026(self) -> None:
        control = self.protocol["change_control"]
        self.assertFalse(control["data_source_mix_allowed"])
        self.assertFalse(control["industry_valuation_substitution_allowed"])
        self.assertFalse(control["default_valuation_score_substitution_allowed"])
        self.assertFalse(control["cross_day_valuation_forward_fill_allowed"])
        self.assertFalse(control["2026_rows_allowed"])


if __name__ == "__main__":
    unittest.main()

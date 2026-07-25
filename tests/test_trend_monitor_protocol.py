from __future__ import annotations

import hashlib
import json
import tomllib
import unittest
from pathlib import Path


class TrendMonitorProtocolTests(unittest.TestCase):
    def setUp(self) -> None:
        self.project = Path(__file__).resolve().parents[1]
        self.source = self.project / "config" / "trend_monitor_validation.toml"
        self.lock = json.loads(
            (
                self.project
                / "config"
                / "trend_monitor_validation.lock.json"
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

    def test_rule_axes_are_independent(self) -> None:
        self.assertEqual(
            set(self.protocol["variants"]),
            {
                "B1_volume_breakout",
                "B2_ma20_pullback",
                "B3_ma5_cross_ma20",
                "combined_top5",
            },
        )

    def test_first_pass_does_not_add_unfrozen_inputs_or_stops(self) -> None:
        control = self.protocol["change_control"]
        self.assertFalse(control["parameter_search_allowed"])
        self.assertFalse(control["stop_loss_allowed"])
        self.assertFalse(control["fundamental_inputs_allowed"])
        self.assertFalse(control["announcement_inputs_allowed"])
        self.assertFalse(control["news_inputs_allowed"])


if __name__ == "__main__":
    unittest.main()

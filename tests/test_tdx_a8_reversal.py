from __future__ import annotations

import hashlib
import json
import tomllib
import unittest
from pathlib import Path

from aplan.tdx_a8_reversal import (
    a8_oscillator,
    analyze_a8_prompt,
    filter_signals,
    raw_buy_flags,
    red_hold_flags,
)


class TdxA8ReversalTests(unittest.TestCase):
    def test_frozen_protocol_and_signal_implementation_match_lock(self) -> None:
        project = Path(__file__).resolve().parents[1]
        protocol_path = project / "config" / "tdx_a8_reversal_validation.toml"
        implementation_path = project / "src" / "aplan" / "tdx_a8_reversal.py"
        lock = json.loads(
            (project / "config" / "tdx_a8_reversal_validation.lock.json").read_text(
                encoding="utf-8"
            )
        )
        protocol = tomllib.loads(protocol_path.read_text(encoding="utf-8"))

        self.assertEqual(hashlib.sha256(protocol_path.read_bytes()).hexdigest(), lock["sha256"])
        self.assertEqual(
            hashlib.sha256(implementation_path.read_bytes()).hexdigest(),
            lock["signal_implementation_sha256"],
        )
        self.assertFalse(protocol["final_holdout_opened"])
        self.assertFalse(protocol["change_control"]["parameter_search_allowed"])
        self.assertEqual(
            set(protocol["variants"]),
            {
                "exact_raw_buy",
                "exact_text_prompt",
                "text_prompt_above_ema245",
                "text_prompt_trend_aligned",
            },
        )

    def test_raw_buy_translates_local_low_negative_count_and_cross(self) -> None:
        oscillator = [-1.0, -2.0, -3.0, -4.0, -5.0, -6.0, -7.0, -6.0]

        flags = raw_buy_flags(oscillator, warmup_bars=1)

        self.assertTrue(flags[-1])
        self.assertFalse(any(flags[:-1]))

    def test_filter_keeps_first_signal_and_suppresses_following_five_bars(self) -> None:
        flags = [True, False, True, False, False, True, True, False]

        self.assertEqual(
            filter_signals(flags, 5),
            [True, False, False, False, False, False, True, False],
        )

    def test_flat_prices_have_no_defined_oscillator_or_buy(self) -> None:
        snapshots = analyze_a8_prompt([10.0] * 300)

        self.assertTrue(all(item.oscillator is None for item in snapshots))
        self.assertFalse(any(item.raw_buy for item in snapshots))
        self.assertFalse(any(item.filtered_buy for item in snapshots))

    def test_ema_oscillator_is_bounded_for_regular_price_changes(self) -> None:
        oscillator = a8_oscillator([10.0, 9.0, 8.0, 9.0, 10.0, 9.5, 10.5])
        values = [value for value in oscillator if value is not None]

        self.assertTrue(values)
        self.assertTrue(all(-100.0 <= value <= 100.0 for value in values))

    def test_red_hold_state_is_separate_from_buy_prompt(self) -> None:
        flags = red_hold_flags([10.0, 10.1, 10.3, 10.2])

        self.assertTrue(flags[2])
        self.assertTrue(flags[3])

    def test_long_context_requires_full_245_bar_history(self) -> None:
        rising = [10.0 + index * 0.01 for index in range(245)]
        snapshots = analyze_a8_prompt(rising)

        self.assertFalse(any(item.above_ema245 for item in snapshots[:244]))
        self.assertTrue(snapshots[-1].above_ema245)
        self.assertTrue(snapshots[-1].trend_aligned)


if __name__ == "__main__":
    unittest.main()

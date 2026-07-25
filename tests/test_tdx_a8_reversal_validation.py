from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from aplan.tdx_a8_reversal_validation import (
    AS_OF_TRADE_DATE,
    Candidate,
    _days_listed,
    _protocol,
    _red_hold_diagnostic,
    _sensitivity,
    _variant_names,
    _window_features,
    main,
)


class TdxA8ReversalValidationTests(unittest.TestCase):
    def test_protocol_and_signal_hashes_are_frozen(self) -> None:
        project = Path(__file__).resolve().parents[1]

        result = _protocol(project)

        self.assertEqual(
            result["sha256"],
            "14b56c80c772573e3ee3e3d8a195c4e637e90dfd682f1899eb58e72fe660ef5e",
        )
        self.assertFalse(result["document"]["final_holdout_opened"])

    def test_variants_are_isolated_and_original_buy_does_not_require_ema(self) -> None:
        raw_only = SimpleNamespace(
            raw_buy=True,
            filtered_buy=False,
            above_ema245=False,
            trend_aligned=False,
        )
        filtered = SimpleNamespace(
            raw_buy=True,
            filtered_buy=True,
            above_ema245=False,
            trend_aligned=False,
        )
        trend = SimpleNamespace(
            raw_buy=True,
            filtered_buy=True,
            above_ema245=True,
            trend_aligned=True,
        )

        self.assertEqual(_variant_names(raw_only), ("exact_raw_buy",))
        self.assertEqual(
            _variant_names(filtered),
            ("exact_raw_buy", "exact_text_prompt"),
        )
        self.assertEqual(
            _variant_names(trend),
            (
                "exact_raw_buy",
                "exact_text_prompt",
                "text_prompt_above_ema245",
                "text_prompt_trend_aligned",
            ),
        )

    def test_window_features_use_only_previous_bars(self) -> None:
        closes = [100.0 + value for value in range(22)]
        turnovers = [50_000_000.0 + value for value in range(22)]

        features = _window_features(closes, turnovers, 21)

        self.assertIsNotNone(features)
        assert features is not None
        self.assertAlmostEqual(features[0], 20 / 100)
        self.assertEqual(features[1], 0.0)
        self.assertEqual(features[3], 50_000_010.5)

    def test_raw_qfq_sensitivity_reports_both_directions(self) -> None:
        result = _sensitivity(
            {
                "exact_raw_buy": {("20240102", "600000")},
                "exact_text_prompt": set(),
                "text_prompt_above_ema245": set(),
                "text_prompt_trend_aligned": set(),
            },
            {
                "exact_raw_buy": {
                    ("20240102", "600000"),
                    ("20240103", "000001"),
                },
                "exact_text_prompt": set(),
                "text_prompt_above_ema245": set(),
                "text_prompt_trend_aligned": set(),
            },
        )

        self.assertEqual(result["exact_raw_buy"]["overlap"], 1)
        self.assertEqual(result["exact_raw_buy"]["raw_only"], 1)
        self.assertEqual(result["exact_raw_buy"]["qfq_only"], 0)

    def test_red_hold_is_diagnostic_only(self) -> None:
        candidate = Candidate(
            symbol="600000",
            signal_date="20240102",
            variants=("exact_raw_buy",),
            pre20_return=0.0,
            drawdown20=0.0,
            volatility20=0.01,
            median_turnover20=100_000_000.0,
            industry_code="801780.SI",
            red_hold_state=True,
        )

        result = _red_hold_diagnostic({"20240102": [candidate]})

        self.assertEqual(
            result["exact_raw_buy"]["role"],
            "separate_holding_or_exit_diagnostic_only",
        )
        self.assertEqual(result["exact_raw_buy"]["red_hold_at_signal_rate"], 1.0)

    def test_listing_days_are_calendar_days(self) -> None:
        self.assertEqual(_days_listed("20230101", "20230501"), 120)

    def test_cli_rejects_2026(self) -> None:
        import sys
        from unittest.mock import patch

        with patch.object(
            sys,
            "argv",
            ["aplan-tdx-a8-validate", "--as-of-trade-date", "20260101"],
        ):
            with self.assertRaisesRegex(SystemExit, "禁止打开 2026"):
                main()

    def test_frozen_as_of_is_2025(self) -> None:
        self.assertEqual(AS_OF_TRADE_DATE, "20251231")


if __name__ == "__main__":
    unittest.main()

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from aplan.trend_monitor_validation import (
    AS_OF_TRADE_DATE,
    OFFSETS,
    PROTOCOL_SHA256,
    TechnicalCandidate,
    _finalize_oos,
    _protocol,
    _technical_snapshot,
    _variant_candidates,
)


def _history(
    closes: list[float],
    *,
    volumes: list[float] | None = None,
) -> list[tuple[float, float, float, float]]:
    volumes = volumes or [100.0] * len(closes)
    return [
        (close, close + 0.1, volume, 60_000_000.0)
        for close, volume in zip(closes, volumes, strict=True)
    ]


class TrendMonitorValidationTests(unittest.TestCase):
    def test_protocol_fallback_is_registered_and_holdout_stays_closed(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            protocol = _protocol(Path(directory))
        self.assertEqual(protocol["sha256"], PROTOCOL_SHA256)
        self.assertEqual(AS_OF_TRADE_DATE, "20251231")
        self.assertEqual(OFFSETS, (0, 1, 2, 3, 4))

    def test_volume_breakout_matches_frozen_rule(self) -> None:
        closes = [10.0] * 60
        candidate = _technical_snapshot(
            "600000",
            "20240102",
            2,
            _history(closes),
            open_price=10.2,
            close=10.5,
            low=10.1,
            volume=160.0,
        )
        self.assertIsNotNone(candidate)
        assert candidate is not None
        self.assertIn("B1_volume_breakout", candidate.signals)
        self.assertEqual(candidate.offset, 2)

    def test_combined_top5_uses_score_then_symbol_without_hiding_axes(self) -> None:
        candidates = [
            TechnicalCandidate(
                symbol=f"60000{index}",
                signal_date="20240102",
                offset=0,
                signals=("B1_volume_breakout",)
                if index % 2
                else ("B2_ma20_pullback",),
                score=float(index),
                pre20_return=0.1,
                average_turnover20=60_000_000.0,
            )
            for index in range(7)
        ]
        variants = _variant_candidates(candidates)
        self.assertEqual(len(variants["combined_top5"]), 5)
        self.assertEqual(
            [item.symbol for item in variants["combined_top5"]],
            ["600006", "600005", "600004", "600003", "600002"],
        )
        self.assertEqual(
            {item.symbol for item in variants["B1_volume_breakout"]},
            {"600001", "600003", "600005"},
        )

    def test_scanner_passes_each_rows_own_open_price(self) -> None:
        source = Path("src/aplan/trend_monitor_validation.py").read_text(
            encoding="utf-8"
        )
        self.assertIn(
            "symbol,\n                open_price,\n                close,",
            source,
        )
        self.assertIn(
            "open_price=open_price,",
            source,
        )

    def test_oos_cannot_nominate_a_rejected_training_horizon(self) -> None:
        development = {"gate_decision": "reject", "metrics": {}}
        oos = {
            "gate_decision": "survive_first_pass",
            "metrics": {"median_matched_excess": 0.02},
        }
        result = _finalize_oos(
            development,
            oos,
            nominated=False,
        )
        self.assertEqual(result["gate_decision"], "reject")
        self.assertFalse(result["training_nominated"])
        self.assertTrue(result["oos_supported"])
        self.assertTrue(result["oos_cannot_self_nominate"])


if __name__ == "__main__":
    unittest.main()

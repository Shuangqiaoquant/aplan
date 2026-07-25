from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from aplan.daily_candidate_historical_validation import (
    AS_OF_TRADE_DATE,
    OFFSETS,
    PROTOCOL_SHA256,
    VALUATION_DEPENDENT,
    _exact_timeline_at,
    _factor_snapshot,
    _model_score,
    _protocol,
    _score_parts,
)


class DailyCandidateHistoricalValidationTests(unittest.TestCase):
    def test_protocol_fallback_keeps_2026_sealed(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            protocol = _protocol(Path(directory))
        self.assertEqual(protocol["sha256"], PROTOCOL_SHA256)
        self.assertEqual(AS_OF_TRADE_DATE, "20251231")
        self.assertEqual(OFFSETS, (0, 1, 2, 3, 4))

    def test_factor_snapshot_replays_twenty_day_axes(self) -> None:
        history = [(100.0 + index, 60_000_000.0 + index * 1_000_000) for index in range(20)]
        snapshot = _factor_snapshot(history, 125.0, 90_000_000.0)
        self.assertIsNotNone(snapshot)
        assert snapshot is not None
        (
            momentum,
            volatility,
            turnover_trend,
            average_turnover,
            above_ma20,
        ) = snapshot
        self.assertAlmostEqual(momentum, 0.25)
        self.assertGreater(volatility, 0)
        self.assertGreater(turnover_trend, 0)
        self.assertGreater(average_turnover, 50_000_000)
        self.assertTrue(above_ma20)

    def test_cross_sectional_scores_use_frozen_weights(self) -> None:
        parts = _score_parts(
            {
                "000001": (0.20, 0.10, 0.30, 80_000_000),
                "000002": (-0.10, 0.40, -0.20, 70_000_000),
            }
        )
        self.assertEqual(parts["000001"][:3], (30.0, 20.0, 15.0))
        self.assertEqual(parts["000002"][:3], (0.0, 0.0, 0.0))

    def test_caps_only_reduce_and_never_add_positive_weight(self) -> None:
        price = (30.0, 20.0, 15.0, 80_000_000.0)
        uncapped = _model_score(
            price,
            variant="no_announcement_cap",
            valuation_score=8.0,
            market_cap=None,
            industry_cap=None,
            fundamental_cap=None,
            announcement_cap=49.0,
        )
        capped = _model_score(
            price,
            variant="full_current_model",
            valuation_score=8.0,
            market_cap=None,
            industry_cap=None,
            fundamental_cap=None,
            announcement_cap=49.0,
        )
        self.assertEqual(uncapped, 85.0)
        self.assertEqual(capped, 49.0)

    def test_missing_pit_valuation_never_uses_default_for_full_model(self) -> None:
        price = (30.0, 20.0, 15.0, 80_000_000.0)
        for variant in VALUATION_DEPENDENT:
            self.assertIsNone(
                _model_score(
                    price,
                    variant=variant,
                    valuation_score=None,
                    market_cap=None,
                    industry_cap=None,
                    fundamental_cap=None,
                    announcement_cap=None,
                )
            )
        self.assertEqual(
            _model_score(
                price,
                variant="no_valuation",
                valuation_score=None,
                market_cap=None,
                industry_cap=None,
                fundamental_cap=None,
                announcement_cap=None,
            ),
            80.0,
        )
        self.assertEqual(
            _model_score(
                price,
                variant="price_score_only",
                valuation_score=None,
                market_cap=None,
                industry_cap=None,
                fundamental_cap=None,
                announcement_cap=None,
            ),
            80.0,
        )

    def test_valuation_requires_an_exact_signal_date(self) -> None:
        timeline = (
            ["20230103", "20230105"],
            [(10.0, 1.0), (12.0, 1.2)],
        )
        self.assertEqual(
            _exact_timeline_at(timeline, "20230103"),
            (10.0, 1.0),
        )
        self.assertIsNone(_exact_timeline_at(timeline, "20230104"))
        self.assertEqual(
            _exact_timeline_at(timeline, "20230105"),
            (12.0, 1.2),
        )


if __name__ == "__main__":
    unittest.main()

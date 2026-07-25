from __future__ import annotations

import unittest

from aplan.fundamental_quality_validation import (
    FundamentalSnapshot,
    _boundary_symbols,
    _finalize_oos,
    _monthly_signal_dates,
    _score_industry,
)
from aplan.announcement_event_validation import _purged_development_cutoff


def _snapshot(
    symbol: str,
    *,
    growth: float,
    profit_growth: float,
    roe: float,
    debt: float,
    cashflow_ratio: float | None,
) -> FundamentalSnapshot:
    return FundamentalSnapshot(
        symbol=symbol,
        period_end="20221231",
        publish_date="20230430",
        revenue_growth=growth,
        net_profit_growth=profit_growth,
        roe=roe,
        debt_to_assets=debt,
        operating_cashflow_to_profit=cashflow_ratio,
        source_hash=symbol,
    )


class FundamentalQualityValidationTests(unittest.TestCase):
    def test_cashflow_ratio_never_changes_score(self) -> None:
        snapshots = {
            f"60000{index}": _snapshot(
                f"60000{index}",
                growth=float(index),
                profit_growth=float(index),
                roe=float(index),
                debt=float(10 - index),
                cashflow_ratio=(-1000.0 if index == 1 else 1000.0),
            )
            for index in range(1, 6)
        }
        first = _score_industry(snapshots, "quality_growth_core")
        changed = {
            symbol: FundamentalSnapshot(
                symbol=item.symbol,
                period_end=item.period_end,
                publish_date=item.publish_date,
                revenue_growth=item.revenue_growth,
                net_profit_growth=item.net_profit_growth,
                roe=item.roe,
                debt_to_assets=item.debt_to_assets,
                operating_cashflow_to_profit=(
                    999999.0
                    if item.operating_cashflow_to_profit == -1000.0
                    else -999999.0
                ),
                source_hash=item.source_hash,
            )
            for symbol, item in snapshots.items()
        }

        self.assertEqual(
            first,
            _score_industry(changed, "quality_growth_core"),
        )

    def test_oos_cannot_self_nominate(self) -> None:
        development = {
            "gate_decision": "reject",
            "metrics": {"median_matched_excess": -0.01},
        }
        oos = {
            "gate_decision": "survive_first_pass",
            "metrics": {"median_matched_excess": 0.10},
        }

        result = _finalize_oos(
            development,
            oos,
            nominated=False,
            negative=False,
        )

        self.assertEqual(result["gate_decision"], "reject")
        self.assertFalse(result["training_nominated"])
        self.assertTrue(result["oos_cannot_self_nominate"])
        self.assertTrue(result["oos_supported"])

    def test_monthly_first_trade_day_and_core_boundaries_are_frozen(self) -> None:
        calendar = [
            "20230103",
            "20230104",
            "20230201",
            "20230202",
            "20240102",
            "20250102",
            "20251231",
            "20260102",
        ]
        self.assertEqual(
            _monthly_signal_dates(calendar),
            [
                "20230103",
                "20230201",
                "20240102",
                "20250102",
                "20251231",
            ],
        )

        snapshots = {
            f"60000{index}": _snapshot(
                f"60000{index}",
                growth=float(index),
                profit_growth=float(index),
                roe=float(index),
                debt=float(10 - index),
                cashflow_ratio=None,
            )
            for index in range(1, 6)
        }
        scores = _score_industry(snapshots, "quality_growth_core")
        top, bottom = _boundary_symbols(scores)

        self.assertEqual(top, {"600005"})
        self.assertEqual(bottom, {"600001"})
        self.assertNotIn("operating_cashflow_to_profit", {
            metric
            for metric in (
                "revenue_growth",
                "net_profit_growth",
                "roe",
                "debt_to_assets",
            )
        })

    def test_training_uses_the_frozen_sixty_day_purge(self) -> None:
        dates = [f"2024{index:04d}" for index in range(1, 101)]
        self.assertEqual(
            _purged_development_cutoff(dates, "20240100", 60),
            "20240040",
        )


if __name__ == "__main__":
    unittest.main()

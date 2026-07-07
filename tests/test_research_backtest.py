from __future__ import annotations

import unittest
import tempfile
from pathlib import Path

from aplan.evidence_coverage import build_coverage
from aplan.evidence_validation import EvidenceFlag, compare_variants
from aplan.research_backtest import calculate_holding_return
from aplan.research_backtest import Signal


class ResearchReturnTests(unittest.TestCase):
    def test_return_uses_pct_change_across_corporate_action(self) -> None:
        dates = ["20260101", "20260102", "20260103"]
        rows = {
            "20260101": {
                "open": 10,
                "high": 10.5,
                "low": 9.9,
                "close": 10.5,
                "pre_close": 10,
                "pct_chg": 5,
            },
            # 除权导致名义价格减半，但 pct_chg 保持真实收益 0%
            "20260102": {
                "open": 5.25,
                "high": 5.25,
                "low": 5.25,
                "close": 5.25,
                "pre_close": 5.25,
                "pct_chg": 0,
            },
            "20260103": {
                "open": 5.25,
                "high": 5.3,
                "low": 5.2,
                "close": 5.25,
                "pre_close": 5.25,
                "pct_chg": 0,
            },
        }
        outcome = calculate_holding_return(rows, dates, 0, 2)
        self.assertIsNotNone(outcome)
        self.assertAlmostEqual(outcome[2], 0.05)  # type: ignore[index]

    def test_one_price_limit_up_blocks_entry(self) -> None:
        dates = ["20260101", "20260102"]
        rows = {
            "20260101": {
                "open": 12,
                "high": 12,
                "low": 12,
                "close": 12,
                "pre_close": 10,
                "pct_chg": 20,
            },
            "20260102": {
                "open": 12,
                "high": 12.1,
                "low": 11.9,
                "close": 12,
                "pre_close": 12,
                "pct_chg": 0,
            },
        }
        self.assertIsNone(calculate_holding_return(rows, dates, 0, 1))

    def test_missing_next_day_cancels_entry_instead_of_waiting_for_resume(self) -> None:
        dates = ["20260101", "20260102", "20260103"]
        rows = {
            "20260103": {
                "open": 10,
                "high": 10.1,
                "low": 9.9,
                "close": 10,
                "pre_close": 10,
                "pct_chg": 0,
            }
        }
        self.assertIsNone(calculate_holding_return(rows, dates, 0, 2))

    def test_evidence_variant_filters_recompute_results(self) -> None:
        dates = ["20260101", "20260102", "20260103"]
        signals = [
            Signal(0, "20260101", "300001", 1, 90, 0.1, 0.2, 0.3),
            Signal(0, "20260101", "300002", 2, 80, 0.1, 0.2, 0.3),
        ]
        rows_by_symbol = {
            "300001": {
                "20260102": {
                    "open": 10,
                    "high": 11,
                    "low": 9.9,
                    "close": 11,
                    "pre_close": 10,
                    "pct_chg": 10,
                },
                "20260103": {
                    "open": 11,
                    "high": 11,
                    "low": 10.9,
                    "close": 11,
                    "pre_close": 11,
                    "pct_chg": 0,
                },
            },
            "300002": {
                "20260102": {
                    "open": 10,
                    "high": 10.1,
                    "low": 8.9,
                    "close": 9,
                    "pre_close": 10,
                    "pct_chg": -10,
                },
                "20260103": {
                    "open": 9,
                    "high": 9.1,
                    "low": 8.9,
                    "close": 9,
                    "pre_close": 9,
                    "pct_chg": 0,
                },
            },
        }
        flags = {
            ("300002", "20260101"): EvidenceFlag(
                "300002",
                "20260101",
                bad_valuation=True,
            )
        }
        records = compare_variants(
            dates,
            signals,
            rows_by_symbol,
            flags,
            holding_days=1,
            variants={
                "baseline": lambda _signal, _flag: True,
                "exclude_bad_valuation": lambda _signal, flag: not flag.bad_valuation,
            },
        )
        self.assertEqual(records["baseline"]["kept_signals"], 2)
        self.assertEqual(records["exclude_bad_valuation"]["kept_signals"], 1)
        self.assertGreater(
            records["exclude_bad_valuation"]["mean_trade_return"],
            records["baseline"]["mean_trade_return"],
        )

    def test_evidence_coverage_counts_missing_daily_basic(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for day in ("20260101", "20260102"):
                raw = root / "data" / "raw" / "tushare" / day
                processed = root / "data" / "processed" / "daily"
                raw.mkdir(parents=True)
                processed.mkdir(parents=True, exist_ok=True)
                (raw / "daily.json").write_text('{"row_count": 1, "rows": []}', encoding="utf-8")
                (processed / f"{day}.csv").write_text("symbol\n", encoding="utf-8")
            (root / "data" / "raw" / "tushare" / "20260102" / "daily_basic.json").write_text(
                '{"row_count": 1, "rows": []}',
                encoding="utf-8",
            )
            coverage = build_coverage(root)
            self.assertEqual(coverage["trade_dates"], 2)
            self.assertEqual(coverage["raw_daily_basic"], 1)
            self.assertEqual(coverage["missing_daily_basic_sample"], ["20260101"])


if __name__ == "__main__":
    unittest.main()

from __future__ import annotations

import unittest

from aplan.historical_extension_probe import evaluate_probe


DATES = ("20150105", "20150706", "20151231")


def _master() -> list[dict[str, str]]:
    return [
        {
            "symbol": "600000",
            "name": "浦发银行",
            "list_date": "19991110",
            "delist_date": "",
        },
        {
            "symbol": "000001",
            "name": "平安银行",
            "list_date": "19910403",
            "delist_date": "",
        },
        {
            "symbol": "600999",
            "name": "退市样本",
            "list_date": "20100101",
            "delist_date": "20201231",
        },
        {
            "symbol": "300750",
            "name": "宁德时代",
            "list_date": "20180611",
            "delist_date": "",
        },
    ]


class HistoricalExtensionProbeTests(unittest.TestCase):
    def test_passes_market_and_industry_with_daily_weights(self) -> None:
        active = {"600000", "000001", "600999"}
        expected = [(day, symbol) for day in DATES for symbol in active]
        result = evaluate_probe(
            dates=DATES,
            historical_pools={day: set(active) for day in DATES},
            master_rows=_master(),
            sample_symbols=["600000", "000001", "600999", "300750"],
            daily_rows=[
                {"trade_date": day, "symbol": symbol} for day, symbol in expected
            ],
            status_rows=[
                (day, symbol, 0, 0, 0, 0, 10, 11, 9, day)
                for day, symbol in expected
            ],
            factor_rows=[(day, symbol, 1.0) for day, symbol in expected],
            market_rows=[
                {"trade_date": day, "index_code": code}
                for day in DATES
                for code in ("000001.SH", "000300.SH", "000905.SH", "399001.SZ")
            ],
            weight_rows=[
                {
                    "trade_date": day,
                    "symbol": symbol,
                    "index_code": "801010.SI",
                    "weight": 1.0,
                }
                for day, symbol in expected
            ],
            constituent_rows=[],
            errors=[],
        )

        self.assertEqual(result["status"], "passed")
        self.assertEqual(
            result["recommendation"],
            "freeze_market_and_industry_history_protocol",
        )
        self.assertEqual(result["matrix"]["raw_status_factor_join"]["coverage"], 1.0)
        self.assertEqual(result["matrix"]["shenwan_level1_pit"]["coverage"], 1.0)
        self.assertEqual(len(result["delisted_evidence"]), 1)
        self.assertEqual(len(result["legal_absence"]), 3)
        self.assertFalse(result["holdout_2026_opened"])

    def test_constituent_intervals_cannot_claim_strict_pit(self) -> None:
        active = {"600000", "000001", "600999"}
        expected = [(day, symbol) for day in DATES for symbol in active]
        constituents = [
            {
                "index_code": "801010.SI",
                "symbol": symbol,
                "in_date": "20100101",
                "out_date": "",
            }
            for symbol in active
        ]
        result = evaluate_probe(
            dates=DATES,
            historical_pools={day: set(active) for day in DATES},
            master_rows=_master(),
            sample_symbols=["600000", "000001", "600999"],
            daily_rows=[
                {"trade_date": day, "symbol": symbol} for day, symbol in expected
            ],
            status_rows=[
                (day, symbol, 0, 0, 0, 0, 10, 11, 9, day)
                for day, symbol in expected
            ],
            factor_rows=[(day, symbol, 1.0) for day, symbol in expected],
            market_rows=[
                {"trade_date": day, "index_code": code}
                for day in DATES
                for code in ("000001.SH", "000300.SH", "000905.SH", "399001.SZ")
            ],
            weight_rows=[],
            constituent_rows=constituents,
            errors=[],
        )

        self.assertEqual(result["status"], "passed_market_only")
        self.assertEqual(
            result["recommendation"], "freeze_market_only_history_protocol"
        )
        industry = result["matrix"]["shenwan_level1_pit"]
        self.assertEqual(industry["status"], "blocked")
        self.assertTrue(industry["constituent_interval_structurally_complete"])

    def test_join_below_threshold_blocks_bulk_download(self) -> None:
        active = {"600000", "000001", "600999"}
        expected = [(day, symbol) for day in DATES for symbol in active]
        result = evaluate_probe(
            dates=DATES,
            historical_pools={day: set(active) for day in DATES},
            master_rows=_master(),
            sample_symbols=list(active),
            daily_rows=[
                {"trade_date": day, "symbol": symbol} for day, symbol in expected
            ],
            status_rows=[
                (day, symbol, 0, 0, 0, 0, 10, 11, 9, day)
                for day, symbol in expected
            ],
            factor_rows=[(day, symbol, 1.0) for day, symbol in expected[:-1]],
            market_rows=[
                {"trade_date": day, "index_code": code}
                for day in DATES
                for code in ("000001.SH", "000300.SH", "000905.SH", "399001.SZ")
            ],
            weight_rows=[],
            constituent_rows=[],
            errors=[],
        )

        self.assertEqual(result["status"], "blocked")
        self.assertEqual(
            result["recommendation"], "blocked_do_not_start_bulk_download"
        )
        self.assertLess(result["matrix"]["raw_status_factor_join"]["coverage"], 0.98)


if __name__ == "__main__":
    unittest.main()

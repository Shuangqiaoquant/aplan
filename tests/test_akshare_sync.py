from __future__ import annotations

import unittest
from math import nan
from datetime import UTC, datetime
from pathlib import Path
from tempfile import TemporaryDirectory

from aplan.akshare_sync import (
    AkShareUpstreamError,
    normalize_financial_indicator_rows,
    normalize_security_rows,
    normalize_spot_em_rows,
    sync_financial_indicators,
    sync_securities,
    sync_spot_valuations,
)


class AkShareSyncTests(unittest.TestCase):
    def test_spot_em_rows_normalize_to_valuation_schema(self) -> None:
        rows = [
            {
                "代码": "300001",
                "市盈率-动态": "12.5",
                "市净率": "1.8",
                "总市值": "1000000000",
                "流通市值": "800000000",
                "换手率": "2.3",
                "量比": "1.2",
            },
            {"代码": "not-a-stock", "市盈率-动态": "1"},
        ]
        values = normalize_spot_em_rows(rows, "20260706")
        self.assertEqual(len(values), 1)
        self.assertEqual(values[0]["symbol"], "300001")
        self.assertEqual(values[0]["trade_date"], "20260706")
        self.assertEqual(values[0]["pe"], 12.5)
        self.assertEqual(values[0]["pb"], 1.8)
        self.assertEqual(values[0]["total_mv"], 1_000_000_000)
        self.assertEqual(values[0]["circ_mv"], 800_000_000)
        self.assertEqual(values[0]["turnover_rate"], 2.3)
        self.assertEqual(values[0]["volume_ratio"], 1.2)

    def test_sync_spot_valuations_retries_upstream_failures(self) -> None:
        attempts = 0

        def flaky_fetcher() -> list[dict[str, object]]:
            nonlocal attempts
            attempts += 1
            if attempts == 1:
                raise ConnectionError("remote closed")
            return [{"代码": "600000", "市盈率-动态": "8", "市净率": "0.7"}]

        with TemporaryDirectory() as tmp:
            result = sync_spot_valuations(
                Path(tmp),
                "20260706",
                retries=1,
                retry_delay=0,
                fetcher=flaky_fetcher,
            )

            self.assertEqual(attempts, 2)
            self.assertEqual(result["valuation_rows"], 1)
            self.assertTrue(Path(result["processed_path"]).exists())

    def test_sync_spot_valuations_reports_clean_upstream_error(self) -> None:
        def broken_fetcher() -> list[dict[str, object]]:
            raise ConnectionError("remote closed")

        with TemporaryDirectory() as tmp:
            with self.assertRaises(AkShareUpstreamError) as context:
                sync_spot_valuations(
                    Path(tmp),
                    "20260706",
                    retries=1,
                    retry_delay=0,
                    fetcher=broken_fetcher,
                )

        message = str(context.exception)
        self.assertIn("暂时不可用", message)
        self.assertIn("已尝试 2 次", message)
        self.assertIn("ConnectionError", message)

    def test_financial_indicator_rows_normalize_to_fundamental_schema(self) -> None:
        observed_at = datetime(2026, 7, 6, tzinfo=UTC)
        rows = [
            {
                "日期": "2025-12-31",
                "主营业务收入增长率(%)": nan,
                "净利润增长率(%)": "-35",
                "净资产收益率(%)": "8.2",
                "经营现金净流量与净利润的比率(%)": "95",
                "资产负债率(%)": "76",
            }
        ]

        values = normalize_financial_indicator_rows("600000", rows, observed_at)

        self.assertEqual(len(values), 1)
        self.assertEqual(values[0]["symbol"], "600000")
        self.assertEqual(values[0]["period_end"], "2025-12-31")
        self.assertEqual(values[0]["publish_time"], "2026-07-06T00:00:00+00:00")
        self.assertEqual(values[0]["source"], "akshare:stock_financial_analysis_indicator:sina:observed")
        self.assertIsNone(values[0]["revenue_growth"])
        self.assertAlmostEqual(values[0]["net_profit_growth"], -0.35)
        self.assertAlmostEqual(values[0]["roe"], 0.082)
        self.assertAlmostEqual(values[0]["operating_cashflow_to_profit"], 0.95)
        self.assertAlmostEqual(values[0]["debt_to_assets"], 0.76)

    def test_sync_financial_indicators_writes_processed_csv(self) -> None:
        def fake_fetcher(symbol: str, start_year: str) -> list[dict[str, object]]:
            self.assertEqual(start_year, "2024")
            return [{"日期": "2025-12-31", "资产负债率(%)": "50", "净利润增长率(%)": "10"}]

        with TemporaryDirectory() as tmp:
            result = sync_financial_indicators(
                Path(tmp),
                ["600000"],
                as_of="2026-07-06",
                start_year="2024",
                retries=0,
                retry_delay=0,
                fetcher=fake_fetcher,
            )

            self.assertEqual(result["fundamental_rows"], 1)
            processed_path = Path(result["processed_path"])
            self.assertTrue(processed_path.exists())
            text = processed_path.read_text(encoding="utf-8")
            self.assertIn("600000", text)
            self.assertIn("akshare:stock_financial_analysis_indicator:sina:observed", text)

    def test_security_rows_normalize_to_security_schema(self) -> None:
        rows = [
            {"证券代码": "600000", "证券简称": "浦发银行", "上市日期": "1999-11-10"},
            {
                "A股代码": "000001",
                "A股简称": "*ST测试",
                "A股上市日期": "1991-04-03",
                "所属行业": "银行",
            },
            {"证券代码": "300001", "证券简称": "退市测试", "上市日期": "2010-01-01"},
        ]

        values = normalize_security_rows(rows)

        self.assertEqual([item["symbol"] for item in values], ["000001", "300001", "600000"])
        self.assertEqual(values[0]["name"], "*ST测试")
        self.assertEqual(values[0]["industry"], "银行")
        self.assertEqual(values[0]["is_st"], "1")
        self.assertEqual(values[1]["is_delisting_risk"], "1")
        self.assertEqual(values[2]["list_date"], "1999-11-10")

    def test_sync_securities_writes_processed_csv(self) -> None:
        def fake_fetcher() -> list[dict[str, object]]:
            return [{"证券代码": "600000", "证券简称": "浦发银行", "上市日期": "1999-11-10"}]

        with TemporaryDirectory() as tmp:
            result = sync_securities(
                Path(tmp),
                as_of="2026-07-06",
                retries=0,
                retry_delay=0,
                fetcher=fake_fetcher,
            )

            self.assertEqual(result["security_rows"], 1)
            processed_path = Path(result["processed_path"])
            self.assertTrue(processed_path.exists())
            self.assertIn("600000", processed_path.read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()

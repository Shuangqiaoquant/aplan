from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from aplan.yinhe_sync import (
    build_kline_request,
    build_security_info_request,
    normalize_daily_rows,
    normalize_security_rows,
    sync_daily,
    sync_securities,
)


class FakeMarketType:
    kSSE = 101
    kSZSE = 102


class FakeMDDatatype:
    kDayKline = 9
    kWeekKline = 10
    k1KLine = 1


class FakeReqKline:
    pass


class FakeSubCodeTableItem:
    pass


class FakeTgw:
    MarketType = FakeMarketType
    MDDatatype = FakeMDDatatype
    ReqKline = FakeReqKline
    SubCodeTableItem = FakeSubCodeTableItem


class YinheSyncTests(unittest.TestCase):
    def test_build_kline_request_fills_sdk_fields(self) -> None:
        request = build_kline_request(
            FakeTgw,
            "600000.SH",
            begin_date="2026-07-06",
            end_date="20260706",
            interval="day",
        )

        self.assertEqual(request.security_code, "600000")
        self.assertEqual(request.market_type, FakeMarketType.kSSE)
        self.assertEqual(request.cyc_type, FakeMDDatatype.kDayKline)
        self.assertEqual(request.begin_date, 20260706)
        self.assertEqual(request.end_date, 20260706)
        self.assertEqual(request.auto_complete, 1)

    def test_build_security_info_request_fills_market_and_symbol(self) -> None:
        request = build_security_info_request(FakeTgw, "szse", "000001.SZ")

        self.assertEqual(request.market, FakeMarketType.kSZSE)
        self.assertEqual(request.security_code, "000001")

    def test_security_rows_normalize_to_security_schema(self) -> None:
        rows = [
            {"证券代码": "600000.SH", "证券简称": "浦发银行", "上市日期": "1999-11-10", "所属行业": "银行"},
            {"A股代码": "000001.SZ", "A股简称": "*ST测试", "A股上市日期": "1991-04-03"},
            {"code": "300001", "name": "创业测试", "list_date": "2010/01/01"},
        ]

        values = normalize_security_rows(rows)

        self.assertEqual([item["symbol"] for item in values], ["000001", "300001", "600000"])
        self.assertEqual(values[0]["is_st"], "1")
        self.assertEqual(values[1]["list_date"], "2010-01-01")
        self.assertEqual(values[2]["industry"], "银行")

    def test_daily_rows_normalize_to_daily_schema(self) -> None:
        rows = [
            {
                "证券代码": "600000.SH",
                "交易日期": "20260706",
                "开盘价": "10.1",
                "最高价": "10.4",
                "最低价": "9.9",
                "收盘价": "10.3",
                "成交量": "1000000",
                "成交额": "12345678",
                "是否停牌": "0",
            }
        ]

        values = normalize_daily_rows(rows, "20260706")

        self.assertEqual(len(values), 1)
        self.assertEqual(values[0]["symbol"], "600000")
        self.assertEqual(values[0]["trade_date"], "20260706")
        self.assertAlmostEqual(values[0]["open"], 10.1)
        self.assertAlmostEqual(values[0]["turnover"], 12345678.0)
        self.assertEqual(values[0]["is_suspended"], "0")

    def test_sync_helpers_write_processed_outputs(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            security_result = sync_securities(
                root,
                as_of="2026-07-06",
                fetcher=lambda: [{"证券代码": "600000", "证券简称": "浦发银行", "上市日期": "1999-11-10"}],
            )
            daily_result = sync_daily(
                root,
                "20260706",
                fetcher=lambda: [
                    {
                        "证券代码": "600000",
                        "交易日期": "20260706",
                        "开盘价": "10",
                        "收盘价": "11",
                    }
                ],
            )

            self.assertTrue(Path(security_result["processed_path"]).exists())
            self.assertTrue(Path(daily_result["processed_path"]).exists())
            self.assertIn("600000", Path(security_result["processed_path"]).read_text(encoding="utf-8"))
            self.assertIn("20260706", Path(daily_result["processed_path"]).read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch
import unittest

from aplan.yinhe_sync import (
    build_kline_request,
    build_security_info_request,
    build_snapshot_request,
    fetch_amazing_data_snapshots,
    normalize_daily_rows,
    normalize_security_rows,
    normalize_snapshot_rows,
    sync_daily,
    sync_securities,
    sync_snapshots,
    sync_snapshots_amazing_data,
)


class FakeMarketType:
    kSSE = 101
    kSZSE = 102


class FakeMDDatatype:
    kDayKline = 9
    kWeekKline = 10
    k1KLine = 1
    kSnapshot = 99


class FakeSubscribeDataType:
    kSnapshot = 199
    kSnapshotL2 = 299


class FakeReqKline:
    pass


class FakeReqDefault:
    pass


class FakeSubCodeTableItem:
    pass


class FakeTgw:
    MarketType = FakeMarketType
    MDDatatype = FakeMDDatatype
    SubscribeDataType = FakeSubscribeDataType
    ReqKline = FakeReqKline
    ReqDefault = FakeReqDefault
    SubCodeTableItem = FakeSubCodeTableItem


class FakeAmazingData:
    last_calendar = None
    last_code_list = None

    @staticmethod
    def login(**_kwargs):
        return True

    class BaseData:
        def get_calendar(self):
            raise TypeError("'NoneType' object is not subscriptable")

    class MarketData:
        def __init__(self, calendar):
            FakeAmazingData.last_calendar = calendar

        def query_snapshot(self, code_list, **_kwargs):
            FakeAmazingData.last_code_list = code_list
            return {"600000.SH": [{"code": "600000.SH", "last": "10.3"}]}


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

    def test_build_snapshot_request_fills_sdk_fields(self) -> None:
        request = build_snapshot_request(FakeTgw, "600000.SH", trade_date="2026-07-06")

        self.assertEqual(request.security_code, "600000")
        self.assertEqual(request.market_type, FakeMarketType.kSSE)
        self.assertEqual(request.date, 20260706)
        self.assertEqual(request.data_type, FakeSubscribeDataType.kSnapshot)
        self.assertEqual(request.level_type, 1)

    def test_build_snapshot_request_accepts_l2_alias(self) -> None:
        request = build_snapshot_request(FakeTgw, "600000.SH", trade_date="20260706", data_type="l2")

        self.assertEqual(request.data_type, FakeSubscribeDataType.kSnapshotL2)

    def test_amazing_data_snapshot_falls_back_to_requested_date_calendar(self) -> None:
        config = type(
            "Config",
            (),
            {
                "username": "user",
                "password": "pass",
                "server_vip": "127.0.0.1",
                "server_port": 1234,
            },
        )()

        with patch("aplan.yinhe_sync._ensure_amazing_data_sdk", return_value=FakeAmazingData):
            rows = fetch_amazing_data_snapshots(config, ["600000"], "20260706", timeout_seconds=0)

        self.assertEqual(FakeAmazingData.last_calendar, [20260706])
        self.assertEqual(FakeAmazingData.last_code_list, ["600000.SH"])
        self.assertEqual(rows[0]["last"], "10.3")

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

    def test_snapshot_rows_normalize_to_snapshot_schema(self) -> None:
        rows = [
            {
                "code": "600000.SH",
                "trade_time": datetime(2026, 7, 6, 9, 30, 0),
                "pre_close": "9.9",
                "open": "10.1",
                "high": "10.2",
                "low": "10.0",
                "last": "10.15",
                "close": "0",
                "volume": "1000",
                "turnover": "10150",
                "trading_phase_code": "T",
            }
        ]

        values = normalize_snapshot_rows(rows, "20260706")

        self.assertEqual(len(values), 1)
        self.assertEqual(values[0]["symbol"], "600000")
        self.assertEqual(values[0]["trade_date"], "20260706")
        self.assertEqual(values[0]["orig_time"], "93000000")
        self.assertAlmostEqual(values[0]["last"], 10.15)
        self.assertEqual(values[0]["trading_phase_code"], "T")

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
            snapshot_result = sync_snapshots(
                root,
                "20260706",
                fetcher=lambda: [{"security_code": "600000", "last_price": "10.1"}],
            )
            snapshot_ad_result = sync_snapshots_amazing_data(
                root,
                "20260706",
                fetcher=lambda: [{"code": "600000.SH", "last": "10.2"}],
            )

            self.assertTrue(Path(security_result["processed_path"]).exists())
            self.assertTrue(Path(daily_result["processed_path"]).exists())
            self.assertTrue(Path(snapshot_result["processed_path"]).exists())
            self.assertTrue(Path(snapshot_ad_result["processed_path"]).exists())
            self.assertIn("600000", Path(security_result["processed_path"]).read_text(encoding="utf-8"))
            self.assertIn("20260706", Path(daily_result["processed_path"]).read_text(encoding="utf-8"))
            self.assertIn("10.1", Path(snapshot_result["processed_path"]).read_text(encoding="utf-8"))
            self.assertIn("10.2", Path(snapshot_ad_result["processed_path"]).read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()

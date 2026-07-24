from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch
import unittest

from aplan.yinhe_sync import (
    audit_daily_coverage,
    backfill_daily,
    build_symbol_pool,
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
    YinheClient,
    YinheUpstreamError,
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
            {
                "证券代码": "600000.SH",
                "证券简称": "浦发银行",
                "上市日期": "1999-11-10",
                "所属行业": "银行",
                "_query_market": "sse",
                "security_type": "stock",
                "security_status": "listed",
            },
            {"A股代码": "000001.SZ", "A股简称": "*ST测试", "A股上市日期": "1991-04-03"},
            {"code": "300001", "name": "创业测试", "list_date": "2010/01/01"},
            {
                "security_code": "000001",
                "symbol": "上证指数",
                "_query_market": "sse",
                "security_type": "01000",
            },
        ]

        values = normalize_security_rows(rows)

        self.assertEqual([item["symbol"] for item in values], ["000001", "300001", "600000"])
        self.assertEqual(values[0]["is_st"], "1")
        self.assertEqual(values[0]["name"], "*ST测试")
        self.assertEqual(values[1]["list_date"], "2010-01-01")
        self.assertEqual(values[2]["industry"], "银行")
        self.assertEqual(values[2]["market"], "sse")
        self.assertEqual(values[2]["security_type"], "stock")
        self.assertEqual(values[2]["security_status"], "listed")

    def test_fetch_securities_queries_markets_separately_and_combines_rows(self) -> None:
        config = type("Config", (), {})()
        client = YinheClient(config)
        client._tgw = FakeTgw
        with patch.object(
            client,
            "query_securities_info",
            side_effect=[
                [{"security_code": "600000", "symbol": "浦发银行"}],
                [{"security_code": "000001", "symbol": "平安银行"}],
            ],
        ) as query:
            rows = client.fetch_securities()

        self.assertEqual(query.call_count, 2)
        self.assertEqual([row["security_code"] for row in rows], ["600000", "000001"])
        self.assertEqual([row["_query_market"] for row in rows], ["sse", "szse"])

    def test_build_symbol_pool_keeps_regular_shanghai_and_shenzhen_a_shares(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            securities = root / "data" / "processed" / "yinhe_securities.csv"
            securities.parent.mkdir(parents=True)
            securities.write_text(
                "symbol,name,list_date,industry,is_st,is_delisting_risk,market,security_type,security_status\n"
                "600000,浦发银行,1999-11-10,银行,0,0,sse,02001,\n"
                "000001,平安银行,1991-04-03,银行,0,0,szse,02001,\n"
                "300001,特锐德,2009-10-30,电力设备,0,0,szse,02003,\n"
                "688001,华兴源创,2019-07-22,机械,0,0,sse,02004,\n"
                "600001,*ST测试,2000-01-01,未知,1,0,sse,02001,\n"
                "000002,退市测试,2000-01-01,未知,0,1,szse,02001,\n"
                "510300,沪深300ETF,2012-05-28,基金,0,0,sse,03001,\n"
                "110000,测试转债,2020-01-01,债券,0,0,sse,04001,\n",
                encoding="utf-8",
            )

            result = build_symbol_pool(root)
            output = root / "data" / "processed" / "yinhe_symbols.txt"

            self.assertEqual(result["source_rows"], 8)
            self.assertEqual(result["symbols"], 4)
            self.assertEqual(
                output.read_text(encoding="utf-8").splitlines(),
                ["000001", "300001", "600000", "688001"],
            )

    def test_build_symbol_pool_can_include_risk_names(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            securities = root / "securities.csv"
            securities.write_text(
                "symbol,name,list_date,industry,is_st,is_delisting_risk,market,security_type,security_status\n"
                "600001,*ST测试,2000-01-01,未知,1,0,sse,02001,\n"
                "000002,退市测试,2000-01-01,未知,0,1,szse,02001,\n",
                encoding="utf-8",
            )

            result = build_symbol_pool(
                root,
                securities_path=securities,
                include_st=True,
                include_delisting=True,
            )

            self.assertEqual(result["symbols"], 2)

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

    def test_audit_daily_coverage_writes_missing_symbols_and_prefix_counts(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            daily = root / "data" / "processed" / "yinhe_daily" / "20260722.csv"
            daily.parent.mkdir(parents=True)
            daily.write_text(
                "symbol,trade_date,close\n"
                "000001,20260722,10\n"
                "600000,20260722,11\n"
                "002001,20260722,12\n",
                encoding="utf-8",
            )
            securities = root / "data" / "processed" / "yinhe_securities.csv"
            securities.write_text(
                "symbol,name,market,security_type,security_status\n"
                "002001,新和成,szse,99002,1\n",
                encoding="utf-8",
            )

            result = audit_daily_coverage(
                root,
                "20260722",
                ["000001", "300001", "600000", "688001"],
            )

            self.assertEqual(result["observed_symbols"], 2)
            self.assertEqual(result["missing_symbols"], 2)
            self.assertEqual(result["coverage_rate"], 0.5)
            self.assertEqual(result["missing_by_prefix"], {"300": 1, "688": 1})
            self.assertEqual(result["observed_outside_pool"], 1)
            self.assertEqual(result["outside_by_security_type"], {"99002": 1})
            self.assertEqual(result["outside_samples"][0]["name"], "新和成")
            self.assertEqual(
                (root / "data" / "processed" / "yinhe_daily_missing" / "20260722.txt")
                .read_text(encoding="utf-8")
                .splitlines(),
                ["300001", "688001"],
            )

    def test_fetch_daily_skips_symbols_with_empty_data(self) -> None:
        config = type("Config", (), {})()
        client = YinheClient(config)
        client._tgw = FakeTgw
        with patch.object(
            client,
            "query_kline",
            side_effect=[
                YinheUpstreamError("银河 K 线查询失败：数据为空"),
                [{"证券代码": "000001", "收盘价": "10"}],
            ],
        ):
            rows = client.fetch_daily(["600001", "000001"], "20260722")

        self.assertEqual(rows, [{"证券代码": "000001", "收盘价": "10"}])

    def test_fetch_daily_keeps_nonempty_errors_fatal(self) -> None:
        config = type("Config", (), {})()
        client = YinheClient(config)
        client._tgw = FakeTgw
        with patch.object(
            client,
            "query_kline",
            side_effect=YinheUpstreamError("银河 K 线查询失败：数据无权限"),
        ):
            with self.assertRaisesRegex(YinheUpstreamError, "数据无权限"):
                client.fetch_daily(["600000"], "20260722")

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

    def test_backfill_daily_skips_existing_dates_and_writes_outputs(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            existing = root / "data" / "processed" / "yinhe_daily" / "20260701.csv"
            existing.parent.mkdir(parents=True)
            existing.write_text("symbol,trade_date\n600000,20260701\n", encoding="utf-8")

            seen_dates: list[str] = []

            def fetcher(day: str) -> list[dict[str, str]]:
                seen_dates.append(day)
                return [
                    {
                        "证券代码": "600000",
                        "交易日期": day,
                        "开盘价": "10",
                        "收盘价": "11",
                    }
                ]

            result = backfill_daily(
                root,
                "20260701",
                "20260703",
                symbols=["600000"],
                max_days=2,
                fetcher=fetcher,
            )

            self.assertEqual(seen_dates, ["20260702", "20260703"])
            self.assertEqual(result["trade_dates"], 3)
            self.assertEqual(result["pending"], 2)
            self.assertEqual(result["completed"], 2)
            self.assertTrue((root / "data" / "processed" / "yinhe_daily" / "20260702.csv").exists())
            self.assertTrue((root / "data" / "raw" / "yinhe" / "20260703" / "daily.json").exists())


if __name__ == "__main__":
    unittest.main()

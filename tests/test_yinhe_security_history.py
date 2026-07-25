from __future__ import annotations

import csv
import json
import sqlite3
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from aplan.yinhe_security_history import (
    reconcile_security_history,
    sync_security_history,
)


class YinheSecurityHistoryTests(unittest.TestCase):
    def test_builds_point_in_time_master_status_and_st_intervals(self) -> None:
        with TemporaryDirectory() as tmp:
            project = Path(tmp)
            calendar = project / "data" / "processed" / "trade_calendar.csv"
            calendar.parent.mkdir(parents=True)
            calendar.write_text(
                "trade_date,is_open\n20230102,1\n20230103,1\n",
                encoding="utf-8",
            )

            result = sync_security_history(
                project,
                start_date="20230102",
                end_date="20230103",
                chunk_size=2,
                code_fetcher=lambda start, end, cache: ["600000.SH", "000001.SZ"],
                basic_fetcher=lambda codes: [
                    {
                        "MARKET_CODE": "600000.SH",
                        "SECURITY_NAME": "浦发银行",
                        "LISTDATE": 19991110,
                        "DELISTDATE": None,
                    },
                    {
                        "MARKET_CODE": "000001.SZ",
                        "SECURITY_NAME": "平安银行",
                        "LISTDATE": 19910403,
                        "DELISTDATE": None,
                    },
                ],
                history_fetcher=lambda codes, start, end, cache: [
                    {
                        "MARKET_CODE": code,
                        "TRADE_DATE": day,
                        "IS_ST_SEC": int(code.startswith("600000") and day == "20230102"),
                        "IS_SUSP_SEC": int(code.startswith("000001") and day == "20230103"),
                        "IS_WD_SEC": 0,
                        "IS_XR_SEC": 0,
                        "PRECLOSE": 10,
                        "HIGH_LIMITED": 11,
                        "LOW_LIMITED": 9,
                    }
                    for code in codes
                    for day in ("20230102", "20230103")
                ],
            )

            self.assertEqual(result["status"], "validated")
            self.assertTrue(result["point_in_time"])
            self.assertEqual(result["security_count"], 2)
            self.assertEqual(result["status_rows"], 4)
            self.assertEqual(result["missing_trade_dates"], 0)
            output = project / "data" / "processed" / "security_history"
            with (output / "name_history.csv").open(encoding="utf-8") as handle:
                intervals = list(csv.DictReader(handle))
            self.assertEqual(intervals[0]["ts_code"], "600000.SH")
            self.assertEqual(intervals[0]["start_date"], "20230102")
            self.assertEqual(intervals[0]["end_date"], "20230102")
            connection = sqlite3.connect(output / "daily_status.sqlite3")
            suspended = connection.execute(
                "SELECT is_suspended FROM daily_status "
                "WHERE symbol = '000001' AND trade_date = '20230103'"
            ).fetchone()[0]
            connection.close()
            self.assertEqual(suspended, 1)
            manifest = json.loads((output / "manifest.json").read_text(encoding="utf-8"))
            self.assertFalse(manifest["strict_availability_lag"])
            self.assertIn("suspension", manifest["status_fields"])

    def test_missing_listing_date_blocks_point_in_time_claim(self) -> None:
        with TemporaryDirectory() as tmp:
            project = Path(tmp)
            calendar = project / "data" / "processed" / "trade_calendar.csv"
            calendar.parent.mkdir(parents=True)
            calendar.write_text("trade_date,is_open\n20230102,1\n", encoding="utf-8")

            result = sync_security_history(
                project,
                start_date="20230102",
                end_date="20230102",
                code_fetcher=lambda start, end, cache: ["600000.SH"],
                basic_fetcher=lambda codes: [
                    {"MARKET_CODE": "600000.SH", "SECURITY_NAME": "浦发银行"}
                ],
                history_fetcher=lambda codes, start, end, cache: [
                    {
                        "MARKET_CODE": "600000.SH",
                        "TRADE_DATE": "20230102",
                        "IS_ST_SEC": 0,
                        "IS_SUSP_SEC": 0,
                    }
                ],
            )

            self.assertEqual(result["status"], "failed_validation")
            self.assertFalse(result["point_in_time"])
            self.assertEqual(result["missing_list_dates"], 1)

    def test_reconcile_code_alias_without_redownloading_status_history(self) -> None:
        with TemporaryDirectory() as tmp:
            project = Path(tmp)
            calendar = project / "data" / "processed" / "trade_calendar.csv"
            calendar.parent.mkdir(parents=True)
            calendar.write_text(
                "trade_date,is_open\n20250214,1\n20250217,1\n",
                encoding="utf-8",
            )
            initial = sync_security_history(
                project,
                start_date="20250214",
                end_date="20250217",
                code_fetcher=lambda start, end, cache: ["300114.SZ", "302132.SZ"],
                basic_fetcher=lambda codes: [
                    {
                        "MARKET_CODE": "302132.SZ",
                        "SECURITY_NAME": "中航成飞",
                        "LISTDATE": 20100827,
                    }
                ],
                history_fetcher=lambda codes, start, end, cache: [
                    {
                        "MARKET_CODE": "302132.SZ",
                        "TRADE_DATE": day,
                        "IS_ST_SEC": 0,
                        "IS_SUSP_SEC": 0,
                    }
                    for day in ("20250214", "20250217")
                    if "302132.SZ" in codes
                ],
            )
            self.assertFalse(initial["point_in_time"])

            config = project / "config" / "security_aliases.csv"
            config.parent.mkdir()
            config.write_text(
                "old_symbol,new_symbol,last_old_date,first_new_date,"
                "entity_name,reason,source_url\n"
                "300114,302132,20250214,20250217,"
                "AVIC Chengdu Aircraft,security_code_change,https://example.com\n",
                encoding="utf-8",
            )

            result = reconcile_security_history(
                project,
                start_date="20250214",
                end_date="20250217",
            )

            self.assertTrue(result["point_in_time"])
            self.assertEqual(result["reconciled_aliases"], 1)
            self.assertEqual(result["security_count"], 1)
            output = project / "data" / "processed" / "security_history"
            with (output / "security_master.csv").open(encoding="utf-8") as handle:
                master = list(csv.DictReader(handle))
            with (output / "security_aliases.csv").open(encoding="utf-8") as handle:
                aliases = list(csv.DictReader(handle))
            self.assertEqual([row["symbol"] for row in master], ["302132"])
            self.assertEqual(aliases[0]["old_symbol"], "300114")
            self.assertEqual(aliases[0]["new_symbol"], "302132")

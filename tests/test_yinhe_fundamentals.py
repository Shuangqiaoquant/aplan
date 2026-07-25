from __future__ import annotations

import json
from pathlib import Path
import sqlite3
from tempfile import TemporaryDirectory
import unittest

from aplan.yinhe_fundamentals import TABLES, _market_code, sync_fundamentals


def _calendar(project: Path) -> None:
    path = project / "data" / "processed" / "trade_calendar.csv"
    path.parent.mkdir(parents=True)
    path.write_text(
        "trade_date,is_open\n"
        "20230102,1\n"
        "20230103,1\n"
        "20230104,1\n"
        "20230105,1\n",
        encoding="utf-8",
    )


def _fetch(
    table_name: str,
    codes: list[str],
    start: str,
    end: str,
    cache: Path,
) -> list[dict[str, object]]:
    common: dict[str, object] = {
        "MARKET_CODE": f"{codes[0]}.SH",
        "REPORTING_PERIOD": "20221231",
        "REPORT_TYPE": "年报",
        "STATEMENT_TYPE": "合并",
        "ANN_DATE": "20230102",
        "ACTUAL_ANN_DATE": "20230103",
    }
    if table_name == "balance_sheet":
        return [{**common, "TOTAL_ASSETS": 100.0, "TOTAL_LIAB": 40.0}]
    if table_name == "income":
        return [{**common, "TOT_OPERA_REV": 80.0, "NET_PRO_EXCL_MIN_INT_INC": 8.0}]
    if table_name == "cash_flow":
        return [{**common, "NET_CASH_FLOWS_OPERA_ACT": 9.0}]
    if table_name == "profit_express":
        return [{
            **common,
            "ROE_WEIGHTED": 10.0,
            "YOY_GR_GROSS_REV": 12.0,
            "YOY_GR_NET_PROFIT_PARENT": 15.0,
        }]
    return [{
        **common,
        "FIRST_ANN_DATE": "20230102",
        "ANN_DATE": "20230104",
        "P_TYPECODE": "10",
        "P_CHANGE_MIN": 10.0,
        "P_CHANGE_MAX": 20.0,
        "P_SUMMARY": "预增",
    }]


class YinheFundamentalTests(unittest.TestCase):
    def test_formats_exchange_suffixes_for_vendor_queries(self) -> None:
        self.assertEqual(_market_code("600000"), "600000.SH")
        self.assertEqual(_market_code("688001"), "688001.SH")
        self.assertEqual(_market_code("000001"), "000001.SZ")
        self.assertEqual(_market_code("300001"), "300001.SZ")

    def test_preserves_versions_and_applies_next_trade_day_availability(self) -> None:
        with TemporaryDirectory() as tmp:
            project = Path(tmp)
            _calendar(project)
            result = sync_fundamentals(
                project,
                start_date="20220101",
                end_date="20231231",
                symbols=["600000"],
                chunk_size=1,
                fetcher=_fetch,
            )

            self.assertEqual(result["status"], "validated")
            self.assertEqual(result["rows"], 5)
            self.assertEqual(set(result["rows_by_table"]), set(TABLES))
            self.assertEqual(result["invalid_timing_rows"], 0)
            self.assertEqual(result["correction_versions"], 5)
            self.assertTrue(result["strict_availability_lag"])

            database = (
                project / "data" / "processed" / "yinhe_fundamentals"
                / "financial_facts.sqlite3"
            )
            connection = sqlite3.connect(database)
            income = connection.execute(
                "SELECT first_ann_date, actual_ann_date, usable_from_trade_date, "
                "revenue, net_profit FROM financial_facts WHERE table_name='income'"
            ).fetchone()
            notice = connection.execute(
                "SELECT first_ann_date, actual_ann_date, usable_from_trade_date, "
                "notice_type, summary FROM financial_facts "
                "WHERE table_name='profit_notice'"
            ).fetchone()
            connection.close()
            self.assertEqual(income, ("20230102", "20230103", "20230104", 80.0, 8.0))
            self.assertEqual(notice, ("20230102", "20230104", "20230105", "10", "预增"))

    def test_completed_chunks_are_reused_for_same_symbol_pool(self) -> None:
        with TemporaryDirectory() as tmp:
            project = Path(tmp)
            _calendar(project)
            calls: list[str] = []

            def fetch(*args: object) -> list[dict[str, object]]:
                calls.append(str(args[0]))
                return _fetch(*args)  # type: ignore[arg-type]

            common = {
                "project": project,
                "start_date": "20220101",
                "end_date": "20231231",
                "symbols": ["600000"],
                "chunk_size": 1,
                "fetcher": fetch,
            }
            first = sync_fundamentals(**common)
            second = sync_fundamentals(**common)

            self.assertEqual(len(calls), len(TABLES))
            self.assertEqual(first["rows"], second["rows"])
            self.assertEqual(second["inserted_this_run"], 0)
            manifest = json.loads(
                (
                    project / "data" / "processed" / "yinhe_fundamentals"
                    / "manifest.json"
                ).read_text(encoding="utf-8")
            )
            self.assertEqual(manifest["completed_chunks"], 1)
            self.assertTrue(manifest["symbol_pool_sha256"])


if __name__ == "__main__":
    unittest.main()

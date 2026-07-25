from __future__ import annotations

import csv
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from aplan.yinhe_benchmarks import MARKET_INDICES, sync_benchmarks


def _market_rows(start: str, end: str) -> list[dict[str, object]]:
    return [
        {
            "INDEX_CODE": code,
            "TRADE_DATE": day,
            "OPEN": 100.0,
            "HIGH": 102.0,
            "LOW": 99.0,
            "CLOSE": 101.0,
            "PRE_CLOSE": 100.0,
            "VOLUME": 1_000,
            "AMOUNT": 100_000,
        }
        for code in MARKET_INDICES
        for day in (start, end)
    ]


def _industry_base() -> list[dict[str, object]]:
    return [
        {
            "INDEX_CODE": "801010.SI",
            "INDUSTRY_CODE": "110000",
            "LEVEL_TYPE": 1,
            "LEVEL1_NAME": "农林牧渔",
            "IS_PUB": 1,
        },
        {
            "INDEX_CODE": "850111.SI",
            "INDUSTRY_CODE": "110101",
            "LEVEL_TYPE": 3,
            "LEVEL1_NAME": "农林牧渔",
            "LEVEL3_NAME": "种子",
            "IS_PUB": 1,
        },
    ]


def _industry_daily(
    codes: list[str], start: str, end: str
) -> list[dict[str, object]]:
    return [
        {
            "INDEX_CODE": code,
            "TRADE_DATE": day,
            "OPEN": 1_000.0,
            "HIGH": 1_020.0,
            "LOW": 990.0,
            "CLOSE": 1_010.0,
            "PRE_CLOSE": 1_000.0,
            "VOLUME": 2_000,
            "AMOUNT": 2_000_000,
        }
        for code in codes
        for day in (start, end)
    ]


class YinheBenchmarkTests(unittest.TestCase):
    def test_builds_compact_market_and_point_in_time_industry_layer(self) -> None:
        with TemporaryDirectory() as tmp:
            project = Path(tmp)
            result = sync_benchmarks(
                project,
                start_date="20230102",
                end_date="20230103",
                refresh_reference=True,
                market_fetcher=_market_rows,
                base_fetcher=_industry_base,
                constituent_fetcher=lambda codes: [
                    {
                        "INDEX_CODE": codes[0],
                        "INDEX_NAME": "农林牧渔",
                        "CON_CODE": "000001.SZ",
                        "INDATE": "20220101",
                        "OUTDATE": "",
                    }
                ],
                daily_fetcher=_industry_daily,
            )

            self.assertEqual(result["status"], "validated")
            self.assertEqual(result["market_index_count"], len(MARKET_INDICES))
            self.assertEqual(result["market_rows"], len(MARKET_INDICES) * 2)
            self.assertEqual(result["industry_level1_count"], 1)
            self.assertEqual(result["industry_constituent_rows"], 1)
            self.assertTrue(result["point_in_time_constituents"])
            self.assertFalse(result["daily_weight_downloaded"])
            self.assertEqual(
                result["market_coverage_by_index"]["000300.SH"]["last_date"],
                "20230103",
            )

    def test_complete_range_is_not_downloaded_twice(self) -> None:
        with TemporaryDirectory() as tmp:
            project = Path(tmp)
            calls = {"market": 0, "daily": 0}

            def market(start: str, end: str) -> list[dict[str, object]]:
                calls["market"] += 1
                return _market_rows(start, end)

            def daily(
                codes: list[str], start: str, end: str
            ) -> list[dict[str, object]]:
                calls["daily"] += 1
                return _industry_daily(codes, start, end)

            common = {
                "project": project,
                "start_date": "20230102",
                "end_date": "20230103",
                "market_fetcher": market,
                "base_fetcher": _industry_base,
                "constituent_fetcher": lambda codes: [
                    {
                        "INDEX_CODE": codes[0],
                        "CON_CODE": "000001.SZ",
                        "INDATE": "20220101",
                    }
                ],
                "daily_fetcher": daily,
            }
            sync_benchmarks(refresh_reference=True, **common)
            sync_benchmarks(refresh_reference=False, **common)

            self.assertEqual(calls, {"market": 1, "daily": 1})

    def test_missing_tail_for_one_index_triggers_incremental_fetch(self) -> None:
        with TemporaryDirectory() as tmp:
            project = Path(tmp)
            common = {
                "project": project,
                "start_date": "20230102",
                "end_date": "20230103",
                "base_fetcher": _industry_base,
                "constituent_fetcher": lambda codes: [
                    {
                        "INDEX_CODE": codes[0],
                        "CON_CODE": "000001.SZ",
                        "INDATE": "20220101",
                    }
                ],
                "daily_fetcher": _industry_daily,
            }
            sync_benchmarks(
                market_fetcher=_market_rows,
                refresh_reference=True,
                **common,
            )
            market_path = (
                project / "data" / "processed" / "benchmarks" / "market_indices.csv"
            )
            with market_path.open(encoding="utf-8") as handle:
                rows = list(csv.DictReader(handle))
            rows = [
                row for row in rows
                if not (
                    row["index_code"] == "000300.SH"
                    and row["trade_date"] == "20230103"
                )
            ]
            with market_path.open("w", encoding="utf-8", newline="") as handle:
                writer = csv.DictWriter(handle, fieldnames=list(rows[0]))
                writer.writeheader()
                writer.writerows(rows)

            requested: list[tuple[str, str]] = []

            def market(start: str, end: str) -> list[dict[str, object]]:
                requested.append((start, end))
                return _market_rows(start, end)

            result = sync_benchmarks(
                market_fetcher=market,
                refresh_reference=False,
                **common,
            )

            self.assertEqual(requested, [("20230103", "20230103")])
            self.assertEqual(result["status"], "validated")


if __name__ == "__main__":
    unittest.main()

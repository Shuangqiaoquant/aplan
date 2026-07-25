from __future__ import annotations

import csv
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from aplan.io import load_fundamentals_csv
from aplan.yinhe_fundamental_snapshots import build_fundamental_snapshots
from aplan.yinhe_fundamentals import sync_fundamentals
from tests.test_yinhe_fundamentals import _calendar, _fetch


class YinheFundamentalSnapshotTests(unittest.TestCase):
    def test_builds_model_ready_snapshots_and_profit_notice_events(self) -> None:
        with TemporaryDirectory() as tmp:
            project = Path(tmp)
            _calendar(project)

            def fetch(
                table_name: str,
                codes: list[str],
                start: str,
                end: str,
                cache: Path,
            ) -> list[dict[str, object]]:
                rows = _fetch(table_name, codes, start, end, cache)
                for row in rows:
                    row["STATEMENT_TYPE"] = "1"
                if table_name == "income":
                    rows.append(
                        {
                            **rows[0],
                            "STATEMENT_TYPE": "6",
                            "TOT_OPERA_REV": 8_000.0,
                            "NET_PRO_EXCL_MIN_INT_INC": 800.0,
                        }
                    )
                return rows

            source = sync_fundamentals(
                project,
                start_date="20220101",
                end_date="20231231",
                symbols=["600000"],
                chunk_size=1,
                fetcher=fetch,
            )
            self.assertEqual(source["status"], "validated")

            result = build_fundamental_snapshots(project)

            self.assertEqual(result["status"], "validated")
            self.assertEqual(result["symbols"], 1)
            self.assertEqual(result["snapshots"], 1)
            self.assertEqual(result["profit_notice_events"], 1)
            self.assertEqual(result["duplicate_keys"], 0)

            output = (
                project / "data" / "processed" / "yinhe_fundamentals"
            )
            snapshots = load_fundamentals_csv(
                output / "fundamental_snapshots.csv"
            )
            self.assertEqual(len(snapshots), 1)
            self.assertEqual(snapshots[0].publish_time.hour, 9)
            self.assertEqual(
                snapshots[0].operating_cashflow_to_profit,
                9.0 / 8.0,
            )
            self.assertAlmostEqual(snapshots[0].revenue_growth or 0, 0.12)
            self.assertAlmostEqual(snapshots[0].net_profit_growth or 0, 0.15)
            self.assertAlmostEqual(snapshots[0].roe or 0, 0.10)
            self.assertAlmostEqual(snapshots[0].debt_to_assets or 0, 0.40)

            with (output / "profit_notice_events.csv").open(
                encoding="utf-8", newline=""
            ) as handle:
                notices = list(csv.DictReader(handle))
            self.assertEqual(notices[0]["publish_time"], "2023-01-05T09:30:00+08:00")
            self.assertEqual(notices[0]["notice_type"], "10")


if __name__ == "__main__":
    unittest.main()

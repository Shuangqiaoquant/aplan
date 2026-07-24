from __future__ import annotations

import csv
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from aplan.yinhe_adjustment import (
    DAILY_FIELDS,
    _connect,
    _quarantine_allowed,
    build_forward_adjusted_daily,
)


def _write_day(path: Path, day: str, close: float) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=DAILY_FIELDS)
        writer.writeheader()
        writer.writerow(
            {
                "symbol": "600000",
                "trade_date": day,
                "open": close,
                "high": close,
                "low": close,
                "close": close,
                "volume": 1000,
                "turnover": close * 1000,
                "is_suspended": 0,
                "is_limit_up": 0,
                "is_limit_down": 0,
            }
        )


class YinheAdjustmentTests(unittest.TestCase):
    def test_quarantine_requires_a_tiny_affected_share(self) -> None:
        self.assertTrue(_quarantine_allowed({"603097"}, 4995))
        self.assertFalse(_quarantine_allowed({"603097"}, 100))
        self.assertFalse(
            _quarantine_allowed(
                {"000001", "000002", "000003", "000004", "000005", "000006"},
                10_000,
            )
        )

    def test_builds_forward_adjusted_prices_and_preserves_raw(self) -> None:
        with TemporaryDirectory() as tmp:
            project = Path(tmp)
            raw = project / "data" / "processed" / "yinhe_daily"
            _write_day(raw / "20230102.csv", "20230102", 10.0)
            _write_day(raw / "20230103.csv", "20230103", 5.0)
            calendar = project / "data" / "processed" / "trade_calendar.csv"
            calendar.parent.mkdir(parents=True, exist_ok=True)
            calendar.write_text(
                "trade_date,is_open\n20230102,1\n20230103,1\n",
                encoding="utf-8",
            )
            database = (
                project
                / "data"
                / "processed"
                / "yinhe_adj_factor"
                / "backward_factors.sqlite3"
            )
            connection = _connect(database)
            with connection:
                connection.executemany(
                    "INSERT INTO backward_factors VALUES (?, ?, ?)",
                    [
                        ("20230102", "600000", 1.0),
                        ("20230103", "600000", 2.0),
                    ],
                )
            connection.close()

            result = build_forward_adjusted_daily(
                project,
                start_date="20230102",
                end_date="20230103",
            )

            with (
                project
                / "data"
                / "processed"
                / "yinhe_daily_qfq"
                / "20230102.csv"
            ).open(encoding="utf-8") as handle:
                adjusted = next(csv.DictReader(handle))
            with (raw / "20230102.csv").open(encoding="utf-8") as handle:
                original = next(csv.DictReader(handle))
            manifest = json.loads(Path(result["manifest_path"]).read_text(encoding="utf-8"))

            self.assertEqual(float(adjusted["close"]), 5.0)
            self.assertEqual(float(original["close"]), 10.0)
            self.assertEqual(manifest["continuity_breaks"], 0)
            self.assertEqual(manifest["factor_change_events"], 1)
            self.assertTrue(manifest["raw_prices_preserved"])
            self.assertEqual(
                json.loads(
                    (
                        project
                        / "data"
                        / "processed"
                        / "yinhe_adj_factor"
                        / "continuity_issues.json"
                    ).read_text(encoding="utf-8")
                ),
                [],
            )

    def test_missing_factor_blocks_validation(self) -> None:
        with TemporaryDirectory() as tmp:
            project = Path(tmp)
            raw = project / "data" / "processed" / "yinhe_daily"
            _write_day(raw / "20230102.csv", "20230102", 10.0)
            calendar = project / "data" / "processed" / "trade_calendar.csv"
            calendar.parent.mkdir(parents=True, exist_ok=True)
            calendar.write_text("trade_date,is_open\n20230102,1\n", encoding="utf-8")
            database = (
                project
                / "data"
                / "processed"
                / "yinhe_adj_factor"
                / "backward_factors.sqlite3"
            )
            connection = _connect(database)
            connection.close()

            result = build_forward_adjusted_daily(
                project,
                start_date="20230102",
                end_date="20230102",
            )

            self.assertEqual(result["status"], "failed_validation")
            self.assertEqual(result["missing_factor_rows"], 1)
            self.assertEqual(result["continuity_breaks"], 0)

    def test_continuity_report_exposes_worsened_adjustment_jump(self) -> None:
        with TemporaryDirectory() as tmp:
            project = Path(tmp)
            raw = project / "data" / "processed" / "yinhe_daily"
            _write_day(raw / "20230102.csv", "20230102", 10.0)
            _write_day(raw / "20230103.csv", "20230103", 5.0)
            calendar = project / "data" / "processed" / "trade_calendar.csv"
            calendar.parent.mkdir(parents=True, exist_ok=True)
            calendar.write_text(
                "trade_date,is_open\n20230102,1\n20230103,1\n",
                encoding="utf-8",
            )
            database = (
                project
                / "data"
                / "processed"
                / "yinhe_adj_factor"
                / "backward_factors.sqlite3"
            )
            connection = _connect(database)
            with connection:
                connection.executemany(
                    "INSERT INTO backward_factors VALUES (?, ?, ?)",
                    [
                        ("20230102", "600000", 1.0),
                        ("20230103", "600000", 0.5),
                    ],
                )
            connection.close()

            result = build_forward_adjusted_daily(
                project,
                start_date="20230102",
                end_date="20230103",
            )
            issues = json.loads(
                Path(result["continuity_issues_path"]).read_text(encoding="utf-8")
            )

            self.assertEqual(result["continuity_breaks"], 1)
            self.assertEqual(result["continuity_worsened_events"], 1)
            self.assertTrue(issues[0]["adjustment_worsened_jump"])
            self.assertAlmostEqual(issues[0]["raw_return"], -0.5)
            self.assertAlmostEqual(issues[0]["adjusted_return"], -0.75)

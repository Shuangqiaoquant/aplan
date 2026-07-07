from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from aplan.quality import validate_daily_snapshot


def row(symbol: str = "300001.SZ", trade_date: str = "20260706") -> dict[str, object]:
    return {
        "ts_code": symbol,
        "trade_date": trade_date,
        "open": 10,
        "high": 11,
        "low": 9,
        "close": 10.5,
        "pre_close": 10,
        "pct_chg": 5,
        "vol": 100,
        "amount": 1_000,
    }


class QualityTests(unittest.TestCase):
    def write_snapshot(self, rows: list[dict[str, object]]) -> tuple[tempfile.TemporaryDirectory[str], Path]:
        directory = tempfile.TemporaryDirectory()
        path = Path(directory.name) / "daily.json"
        path.write_text(json.dumps({"rows": rows}), encoding="utf-8")
        return directory, path

    def test_valid_snapshot_passes(self) -> None:
        directory, path = self.write_snapshot([row()])
        try:
            report = validate_daily_snapshot(path, "20260706", minimum_rows=1)
            self.assertTrue(report.passed)
            self.assertEqual(report.unique_symbols, 1)
            self.assertEqual(len(report.sha256), 64)
        finally:
            directory.cleanup()

    def test_duplicate_and_bad_ohlc_fail(self) -> None:
        bad = row()
        bad["high"] = 8
        directory, path = self.write_snapshot([bad, bad])
        try:
            report = validate_daily_snapshot(path, "20260706", minimum_rows=1)
            self.assertFalse(report.passed)
            self.assertTrue(any("重复" in error for error in report.errors))
            self.assertTrue(any("OHLC" in error for error in report.errors))
        finally:
            directory.cleanup()


if __name__ == "__main__":
    unittest.main()


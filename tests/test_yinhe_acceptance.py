from __future__ import annotations

import csv
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from aplan.validation_protocol import freeze_protocol
from aplan.yinhe_acceptance import run_yinhe_acceptance


FIELDS = (
    "symbol",
    "trade_date",
    "open",
    "high",
    "low",
    "close",
    "volume",
    "turnover",
    "is_suspended",
    "is_limit_up",
    "is_limit_down",
)


def _write_daily(path: Path, day: str, rows: list[tuple[str, float]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=FIELDS)
        writer.writeheader()
        for symbol, close in rows:
            writer.writerow(
                {
                    "symbol": symbol,
                    "trade_date": day,
                    "open": close,
                    "high": close * 1.01,
                    "low": close * 0.99,
                    "close": close,
                    "volume": 1000,
                    "turnover": close * 1000,
                    "is_suspended": 0,
                    "is_limit_up": 0,
                    "is_limit_down": 0,
                }
            )


class YinheAcceptanceTests(unittest.TestCase):
    def _project(self, root: Path) -> Path:
        source = Path(__file__).resolve().parents[1] / "config" / "validation_protocol.toml"
        config = root / "config"
        config.mkdir()
        (config / "validation_protocol.toml").write_bytes(source.read_bytes())
        freeze_protocol(root, change_reason="acceptance test")
        processed = root / "data" / "processed"
        processed.mkdir(parents=True)
        (processed / "yinhe_symbols.txt").write_text(
            "300001\n301001\n600000\n",
            encoding="utf-8",
        )
        (processed / "yinhe_securities.csv").write_text(
            "symbol,name,list_date,industry,is_st,is_delisting_risk,market,security_type,security_status\n"
            "300001,特锐德,2009-10-30,电力设备,0,0,szse,02003,\n"
            "301001,凯淳股份,2021-05-28,商贸零售,0,0,szse,02003,\n"
            "600000,浦发银行,1999-11-10,银行,0,0,sse,02001,\n",
            encoding="utf-8",
        )
        (processed / "trade_calendar.csv").write_text(
            "trade_date,is_open\n20260721,1\n20260722,1\n",
            encoding="utf-8",
        )
        return root

    def test_acceptance_profiles_data_and_writes_hash_manifest(self) -> None:
        with TemporaryDirectory() as tmp:
            project = self._project(Path(tmp))
            daily = project / "data" / "processed" / "yinhe_daily"
            rows = [("300001", 10.0), ("301001", 20.0), ("600000", 12.0)]
            _write_daily(daily / "20260721.csv", "20260721", rows)
            _write_daily(daily / "20260722.csv", "20260722", rows)

            result = run_yinhe_acceptance(
                project,
                start_date="20260721",
                end_date="20260722",
            )

            self.assertTrue(result["readiness"]["raw_price_research_ready"])
            self.assertFalse(result["readiness"]["strict_backtest_ready"])
            self.assertEqual(result["profile"]["row_count"], 6)
            self.assertEqual(result["profile"]["duplicate_keys"], 0)
            self.assertIn("survivorship_bias", result["blocked_checks"])
            manifest = json.loads(Path(result["paths"]["manifest"]).read_text(encoding="utf-8"))
            self.assertEqual(len(manifest["files"]), 2)
            self.assertEqual(len(manifest["aggregate_sha256"]), 64)
            self.assertTrue(Path(result["paths"]["markdown"]).exists())

    def test_acceptance_fails_duplicate_primary_key(self) -> None:
        with TemporaryDirectory() as tmp:
            project = self._project(Path(tmp))
            daily = project / "data" / "processed" / "yinhe_daily"
            rows = [
                ("300001", 10.0),
                ("300001", 10.0),
                ("301001", 20.0),
                ("600000", 12.0),
            ]
            _write_daily(daily / "20260721.csv", "20260721", rows)
            _write_daily(daily / "20260722.csv", "20260722", rows)

            result = run_yinhe_acceptance(
                project,
                start_date="20260721",
                end_date="20260722",
            )

            self.assertEqual(result["status"], "failed")
            self.assertIn("daily_primary_key_uniqueness", result["failed_checks"])


if __name__ == "__main__":
    unittest.main()

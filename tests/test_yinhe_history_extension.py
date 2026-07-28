from __future__ import annotations

import csv
import json
import tempfile
import unittest
from pathlib import Path

from aplan.yinhe_history_extension import (
    audit_history_coverage,
    build_historical_symbol_pool,
)


class YinheHistoryExtensionTests(unittest.TestCase):
    def test_pool_includes_delisted_during_requested_window(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            project = Path(temporary)
            master = project / "data" / "processed" / "security_history" / "security_master.csv"
            master.parent.mkdir(parents=True)
            master.write_text(
                "ts_code,symbol,name,industry,list_date,delist_date\n"
                "000001.SZ,000001,A,x,19910101,\n"
                "000002.SZ,000002,B,x,20000101,20210601\n"
                "000003.SZ,000003,C,x,20230101,\n"
                "000004.SZ,000004,D,x,20000101,20201231\n",
                encoding="utf-8",
            )
            output = project / "pool.txt"

            result = build_historical_symbol_pool(
                project,
                start_date="20210101",
                end_date="20221231",
                output_path=output,
            )

            self.assertEqual(result["symbols"], 2)
            self.assertEqual(output.read_text().splitlines(), ["000001", "000002"])

    def test_audit_reports_yearly_coverage_and_quality(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            project = Path(temporary)
            processed = project / "data" / "processed"
            raw = processed / "yinhe_daily"
            qfq = processed / "yinhe_daily_qfq"
            raw.mkdir(parents=True)
            qfq.mkdir(parents=True)
            (processed / "trade_calendar.csv").write_text(
                "trade_date,is_open\n20210104,1\n20210105,1\n",
                encoding="utf-8",
            )
            fields = [
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
            ]
            for folder in (raw, qfq):
                with (folder / "20210104.csv").open("w", newline="", encoding="utf-8") as handle:
                    writer = csv.DictWriter(handle, fieldnames=fields)
                    writer.writeheader()
                    writer.writerow(
                        {
                            "symbol": "000001",
                            "trade_date": "20210104",
                            "open": 10,
                            "high": 11,
                            "low": 9,
                            "close": 10,
                            "volume": 1,
                            "turnover": 10,
                            "is_suspended": 0,
                            "is_limit_up": 0,
                            "is_limit_down": 0,
                        }
                    )
            factor = processed / "yinhe_adj_factor"
            factor.mkdir()
            (factor / "manifest.json").write_text(
                json.dumps(
                    {
                        "status": "validated",
                        "coverage_start": "20210104",
                        "coverage_end": "20210104",
                        "missing_factor_rows": 0,
                    }
                ),
                encoding="utf-8",
            )
            security = processed / "security_history"
            security.mkdir()
            (security / "manifest.json").write_text(
                json.dumps({"point_in_time": True}),
                encoding="utf-8",
            )

            result = audit_history_coverage(
                project,
                start_date="20210101",
                end_date="20210105",
                output_dir=project / "reports",
            )

            self.assertEqual(result["status"], "failed_validation")
            self.assertIn("2021:missing_trade_date_files", result["failed_checks"])
            self.assertEqual(result["years"]["2021"]["raw_files"], 1)
            self.assertEqual(result["years"]["2021"]["missing_raw_dates"], ["20210105"])
            self.assertEqual(result["years"]["2021"]["raw_profile"]["duplicate_keys"], 0)
            self.assertAlmostEqual(
                result["years"]["2021"]["raw_profile"]["unit_inference_median"],
                1.0,
            )


if __name__ == "__main__":
    unittest.main()

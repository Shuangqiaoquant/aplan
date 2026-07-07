from __future__ import annotations

import unittest
from pathlib import Path

from aplan.daily_report import render_daily_audit


class DailyReportTests(unittest.TestCase):
    def test_report_makes_non_executable_status_visible(self) -> None:
        audit = {
            "trade_date": "20260706",
            "status": "passed_research_only",
            "mode": "research_only",
            "strategy_approved": False,
            "stages": {
                "quality": {
                    "passed": True,
                    "row_count": 5_000,
                    "unique_symbols": 5_000,
                    "sha256": "a" * 64,
                    "metrics": {},
                    "errors": [],
                    "warnings": [],
                },
                "strategy": {
                    "status": "no_registered_strategy",
                    "registered_count": 0,
                    "signal_count": 0,
                    "execution_allowed": False,
                },
                "portfolio": {"status": "not_initialized", "orders": 0},
            },
        }
        report = render_daily_audit(audit, Path("audit.json"))
        self.assertIn("允许执行：否", report)
        self.assertIn("仅用于研究", report)


if __name__ == "__main__":
    unittest.main()


from __future__ import annotations

import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from aplan.email_notify import build_email, normalize_recipients, send_file


class EmailNotifyTests(unittest.TestCase):
    def test_email_clearly_marks_research_only(self) -> None:
        audit = {
            "trade_date": "20260706",
            "status": "passed_research_only",
            "stages": {
                "quality": {"passed": True, "row_count": 5517},
                "announcements": {"announcement_count": 335, "high_risk_count": 26},
                "announcement_fulltext": {"completed": 4, "failed": 0, "needs_ocr": 0},
                "strategy": {
                    "status": "no_registered_strategy",
                    "execution_allowed": False,
                    "signal_count": 0,
                },
                "portfolio": {"status": "not_initialized", "orders": 0},
            },
        }
        subject, body = build_email(Path("/nonexistent"), audit)
        self.assertIn("高风险 26", subject)
        self.assertIn("仅用于研究", body)
        self.assertIn("可执行信号：0", body)

    def test_send_file_uses_attachment_without_touching_daily_state(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            script = root / "scripts" / "send_mail.applescript"
            script.parent.mkdir(parents=True)
            script.write_text("-- fake", encoding="utf-8")
            report = root / "reports" / "special.md"
            report.parent.mkdir(parents=True)
            report.write_text("report", encoding="utf-8")
            with patch("aplan.email_notify.subprocess.run") as run:
                run.return_value.stdout = "sent\n"
                result = send_file(
                    root,
                    report_path=report,
                    subject="专项报告",
                    body="正文",
                    recipient="to@example.com",
                    sender="from@example.com",
                )

            self.assertEqual(result["status"], "sent")
            self.assertEqual(result["report_path"], str(report))
            self.assertFalse((root / "state" / "notifications" / "email.json").exists())
            self.assertEqual(run.call_args.args[0][2], "to@example.com")
            self.assertEqual(run.call_args.args[0][4], "专项报告")

    def test_recipient_list_accepts_comma_or_semicolon(self) -> None:
        self.assertEqual(
            normalize_recipients("a@example.com, b@example.com; c@example.com"),
            ["a@example.com", "b@example.com", "c@example.com"],
        )


if __name__ == "__main__":
    unittest.main()

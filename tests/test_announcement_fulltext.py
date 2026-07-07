from __future__ import annotations

import unittest

from aplan.announcement_fulltext import analyze_fulltext


class AnnouncementFulltextTests(unittest.TestCase):
    def test_analysis_keeps_risk_and_never_creates_signal(self) -> None:
        event = {
            "announcement_id": "1",
            "symbol": "300001",
            "event_type": "regulatory_action",
            "risk_level": "high",
            "confidence": 0.9,
        }
        announcement = {
            "source_url": "https://example.test/a.pdf",
        }
        text = "公司收到立案告知书。相关事项存在重大不确定性。截至目前尚未结案。"
        analysis = analyze_fulltext(event, announcement, text, "a" * 64)
        self.assertEqual(analysis.conclusion, "risk_review_required")
        self.assertTrue(analysis.negative_evidence)
        self.assertFalse(analysis.actionable_signal_created)
        self.assertLessEqual(analysis.confidence, 0.7)

    def test_short_text_requires_ocr_or_review(self) -> None:
        event = {
            "announcement_id": "2",
            "symbol": "300001",
            "event_type": "other",
            "risk_level": "low",
            "confidence": 0.3,
        }
        analysis = analyze_fulltext(
            event,
            {"source_url": "https://example.test/b.pdf"},
            "很短",
            "b" * 64,
        )
        self.assertTrue(any("文本过少" in item for item in analysis.uncertainties))


if __name__ == "__main__":
    unittest.main()


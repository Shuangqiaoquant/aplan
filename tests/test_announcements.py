from __future__ import annotations

import unittest

from aplan.announcements import (
    Announcement,
    EventImpact,
    RiskLevel,
    classify_title,
    parse_announcement,
)


class AnnouncementTests(unittest.TestCase):
    def test_parse_cninfo_metadata_and_url(self) -> None:
        item = {
            "announcementId": "123",
            "secCode": "300001",
            "secName": "测试股份",
            "announcementTitle": "关于<em>回购</em>股份的公告",
            "announcementTime": 1_788_000_000_000,
            "adjunctUrl": "finalpage/2026-07-06/test.PDF",
        }
        value = parse_announcement(item)
        self.assertIsNotNone(value)
        self.assertEqual(value.title, "关于回购股份的公告")  # type: ignore[union-attr]
        self.assertTrue(value.source_url.startswith("https://static.cninfo.com.cn/"))  # type: ignore[union-attr]

    def test_risk_rule_precedes_positive_language(self) -> None:
        announcement = Announcement(
            "1",
            "300001",
            "测试",
            "关于股票交易异常波动暨风险提示公告",
            "2026-07-06T10:00:00Z",
            "https://example.test/a.pdf",
        )
        event = classify_title(announcement)
        self.assertEqual(event.event_type, "market_risk_warning")
        self.assertEqual(event.impact_hint, EventImpact.NEGATIVE)
        self.assertEqual(event.risk_level, RiskLevel.HIGH)
        self.assertTrue(event.requires_fulltext)

    def test_unknown_title_never_claims_direction(self) -> None:
        announcement = Announcement(
            "2",
            "300001",
            "测试",
            "第六届董事会会议决议公告",
            "2026-07-06T10:00:00Z",
            "https://example.test/b.pdf",
        )
        event = classify_title(announcement)
        self.assertEqual(event.impact_hint, EventImpact.UNKNOWN)
        self.assertLess(event.confidence, 0.5)

    def test_regulatory_negation_is_not_negative(self) -> None:
        announcement = Announcement(
            "3",
            "300001",
            "测试",
            "关于最近五年不存在被证券监管部门处罚情况的公告",
            "2026-07-06T10:00:00Z",
            "https://example.test/c.pdf",
        )
        event = classify_title(announcement)
        self.assertEqual(event.event_type, "compliance_statement")
        self.assertEqual(event.impact_hint, EventImpact.NEUTRAL)


if __name__ == "__main__":
    unittest.main()

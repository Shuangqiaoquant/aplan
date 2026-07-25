from __future__ import annotations

import csv
import http.client
import json
import tempfile
import unittest
from pathlib import Path

from aplan.announcements import (
    Announcement,
    CninfoError,
    EventImpact,
    RiskLevel,
    backfill_announcements,
    build_announcement_archive,
    classify_title,
    parse_announcement,
    sync_announcements,
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

    def test_client_wraps_incomplete_http_reads_for_retry(self) -> None:
        from unittest.mock import patch

        from aplan.announcements import CninfoClient

        with patch(
            "aplan.announcements.urllib.request.urlopen",
            side_effect=http.client.IncompleteRead(b"partial", 10),
        ):
            with self.assertRaisesRegex(CninfoError, "IncompleteRead"):
                CninfoClient().query_page(
                    "20230103",
                    column="szse",
                    page_num=1,
                )

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

    def test_backfill_includes_calendar_days_and_sets_next_trade_day(self) -> None:
        class FakeClient:
            def query_page(
                self,
                trade_date: str,
                *,
                column: str,
                page_num: int,
                page_size: int = 30,
            ) -> dict[str, object]:
                rows = []
                if trade_date == "20230107" and column == "szse":
                    rows = [
                        {
                            "announcementId": "weekend-1",
                            "secCode": "300001",
                            "secName": "测试股份",
                            "announcementTitle": "关于回购股份的公告",
                            "announcementTime": 1_673_020_800_000,
                            "adjunctUrl": "finalpage/test.pdf",
                        }
                    ]
                return {
                    "totalpages": 1,
                    "announcements": rows,
                }

        with tempfile.TemporaryDirectory() as directory:
            project = Path(directory)
            calendar = project / "data" / "processed" / "trade_calendar.csv"
            calendar.parent.mkdir(parents=True)
            with calendar.open("w", encoding="utf-8", newline="") as handle:
                writer = csv.writer(handle)
                writer.writerow(["trade_date"])
                writer.writerows([["20230106"], ["20230109"], ["20230110"]])
            result = backfill_announcements(
                project,
                start="20230107",
                end="20230108",
                calendar_file=calendar,
                request_delay=0,
                day_delay=0,
                client=FakeClient(),  # type: ignore[arg-type]
            )
            self.assertEqual(result["completed"], 2)
            document = json.loads(
                (
                    project
                    / "data"
                    / "processed"
                    / "announcements"
                    / "20230107.json"
                ).read_text(encoding="utf-8")
            )
            self.assertEqual(
                document["announcements"][0]["usable_from_trade_date"],
                "20230109",
            )
            self.assertEqual(result["archive"]["status"], "validated")

    def test_archive_deduplicates_repeated_announcement_ids(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            project = Path(directory)
            source = project / "data" / "processed" / "announcements"
            source.mkdir(parents=True)
            row = {
                "announcement_id": "a1",
                "symbol": "600000",
                "company_name": "浦发银行",
                "title": "测试公告",
                "published_at": "2023-01-03T00:00:00+00:00",
                "usable_from_trade_date": "20230104",
                "source_url": "https://example.test/a.pdf",
                "source": "cninfo",
            }
            event = {
                "announcement_id": "a1",
                "symbol": "600000",
                "published_at": row["published_at"],
                "usable_from_trade_date": "20230104",
                "event_type": "other",
                "impact_hint": "unknown",
                "risk_level": "low",
                "confidence": 0.3,
                "summary": "测试",
                "source_url": row["source_url"],
                "requires_fulltext": True,
                "analyzer": "title_rules_v1",
            }
            for day in ("20230103", "20230104"):
                (source / f"{day}.json").write_text(
                    json.dumps(
                        {"announcements": [row], "events": [event]},
                        ensure_ascii=False,
                    ),
                    encoding="utf-8",
                )
            result = build_announcement_archive(
                project,
                start="20230103",
                end="20230104",
            )
            self.assertEqual(result["announcements"], 1)
            self.assertEqual(result["events"], 1)
            self.assertEqual(result["duplicate_announcements_skipped"], 1)
            self.assertEqual(result["duplicate_events_skipped"], 1)

    def test_archive_treats_calendar_right_edge_as_pending(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            project = Path(directory)
            source = project / "data" / "processed" / "announcements"
            source.mkdir(parents=True)
            row = {
                "announcement_id": "edge-1",
                "symbol": "600000",
                "published_at": "2026-07-24T12:00:00+08:00",
                "usable_from_trade_date": "",
            }
            (source / "20260724.json").write_text(
                json.dumps(
                    {
                        "announcements": [row],
                        "events": [row],
                    }
                ),
                encoding="utf-8",
            )
            result = build_announcement_archive(
                project,
                start="20260724",
                end="20260724",
                trade_calendar=["20260724"],
            )
            self.assertEqual(result["status"], "validated")
            self.assertEqual(result["missing_availability_rows"], 0)
            self.assertEqual(result["pending_availability_rows"], 2)

    def test_sync_rejects_incomplete_reported_pagination(self) -> None:
        class IncompleteClient:
            def query_page(
                self,
                trade_date: str,
                *,
                column: str,
                page_num: int,
                page_size: int = 30,
            ) -> dict[str, object]:
                return {
                    "totalpages": 1,
                    "totalAnnouncement": 2,
                    "announcements": [
                        {
                            "announcementId": f"{column}-1",
                            "secCode": "300001",
                            "secName": "测试股份",
                            "announcementTitle": "测试公告",
                            "announcementTime": 1_673_020_800_000,
                            "adjunctUrl": "finalpage/test.pdf",
                        }
                    ],
                }

        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaisesRegex(CninfoError, "分页不完整"):
                sync_announcements(
                    Path(directory),
                    "20230107",
                    trade_calendar=["20230109"],
                    request_delay=0,
                    client=IncompleteClient(),  # type: ignore[arg-type]
                )

    def test_sync_uses_reported_count_when_page_count_is_too_small(self) -> None:
        class UnderreportedPagesClient:
            def query_page(
                self,
                trade_date: str,
                *,
                column: str,
                page_num: int,
                page_size: int = 30,
            ) -> dict[str, object]:
                size = 30 if page_num == 1 else 1
                return {
                    "totalpages": 1,
                    "totalAnnouncement": 31,
                    "announcements": [
                        {
                            "announcementId": f"{column}-{page_num}-{index}",
                            "secCode": "300001",
                            "secName": "测试股份",
                            "announcementTitle": "测试公告",
                            "announcementTime": 1_673_020_800_000,
                            "adjunctUrl": f"finalpage/{column}-{page_num}-{index}.pdf",
                        }
                        for index in range(size)
                    ],
                }

        with tempfile.TemporaryDirectory() as directory:
            result = sync_announcements(
                Path(directory),
                "20230107",
                trade_calendar=["20230109"],
                request_delay=0,
                client=UnderreportedPagesClient(),  # type: ignore[arg-type]
            )
            self.assertEqual(result["page_stats"]["szse"]["reported_pages"], 1)
            self.assertEqual(result["page_stats"]["szse"]["requested_pages"], 2)
            self.assertEqual(result["page_stats"]["szse"]["received_rows"], 31)


if __name__ == "__main__":
    unittest.main()

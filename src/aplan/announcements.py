from __future__ import annotations

import argparse
import bisect
import csv
import hashlib
import http.client
import json
import ssl
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import asdict, dataclass
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from pathlib import Path
from typing import Any, Protocol


class EventImpact(StrEnum):
    POSITIVE = "positive"
    NEGATIVE = "negative"
    MIXED = "mixed"
    NEUTRAL = "neutral"
    UNKNOWN = "unknown"


class RiskLevel(StrEnum):
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"
    CRITICAL = "critical"


@dataclass(frozen=True, slots=True)
class Announcement:
    announcement_id: str
    symbol: str
    company_name: str
    title: str
    published_at: str
    source_url: str
    source: str = "cninfo"


@dataclass(frozen=True, slots=True)
class AnnouncementEvent:
    announcement_id: str
    symbol: str
    event_type: str
    impact_hint: EventImpact
    risk_level: RiskLevel
    confidence: float
    summary: str
    evidence: tuple[str, ...]
    source_url: str
    published_at: str
    requires_fulltext: bool
    analyzer: str = "title_rules_v1"

    def to_dict(self) -> dict[str, Any]:
        value = asdict(self)
        value["impact_hint"] = self.impact_hint.value
        value["risk_level"] = self.risk_level.value
        return value


class InformationAgent(Protocol):
    agent_id: str
    version: str

    def analyze(
        self,
        announcement: Announcement,
        fulltext: str,
    ) -> AnnouncementEvent:
        """输出结构化事件；不得直接生成交易订单。"""
        ...


class CninfoError(RuntimeError):
    pass


class CninfoClient:
    endpoint = "https://www.cninfo.com.cn/new/hisAnnouncement/query"

    @staticmethod
    def _ssl_context() -> ssl.SSLContext:
        system_ca = Path("/etc/ssl/cert.pem")
        return (
            ssl.create_default_context(cafile=str(system_ca))
            if system_ca.exists()
            else ssl.create_default_context()
        )

    def query_page(
        self,
        trade_date: str,
        *,
        column: str,
        page_num: int,
        page_size: int = 30,
    ) -> dict[str, Any]:
        iso_date = datetime.strptime(trade_date, "%Y%m%d").strftime("%Y-%m-%d")
        payload = urllib.parse.urlencode(
            {
                "pageNum": page_num,
                "pageSize": page_size,
                "column": column,
                "tabName": "fulltext",
                "plate": "",
                "stock": "",
                "searchkey": "",
                "secid": "",
                "category": "",
                "trade": "",
                "seDate": f"{iso_date}~{iso_date}",
                "sortName": "",
                "sortType": "",
                "isHLtitle": "true",
            }
        ).encode()
        request = urllib.request.Request(
            self.endpoint,
            data=payload,
            headers={
                "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8",
                "Referer": "https://www.cninfo.com.cn/new/commonUrl?url=disclosure/list/notice",
                "User-Agent": "Mozilla/5.0 APlanResearch/0.1",
                "X-Requested-With": "XMLHttpRequest",
            },
        )
        try:
            with urllib.request.urlopen(
                request,
                timeout=30,
                context=self._ssl_context(),
            ) as response:
                return json.loads(response.read().decode("utf-8"))
        except (
            urllib.error.URLError,
            TimeoutError,
            json.JSONDecodeError,
            http.client.HTTPException,
        ) as exc:
            raise CninfoError(f"巨潮公告请求失败：{exc}") from exc


def _clean_title(title: str) -> str:
    return title.replace("<em>", "").replace("</em>", "").strip()


def parse_announcement(item: dict[str, Any]) -> Announcement | None:
    symbol = str(item.get("secCode") or "").strip()
    if len(symbol) != 6 or not symbol.isdigit():
        return None
    timestamp = item.get("announcementTime")
    if isinstance(timestamp, (int, float)):
        published_at = datetime.fromtimestamp(timestamp / 1000, UTC).isoformat()
    else:
        published_at = str(timestamp or "")
    adjunct = str(item.get("adjunctUrl") or "").lstrip("/")
    return Announcement(
        announcement_id=str(item.get("announcementId") or adjunct),
        symbol=symbol,
        company_name=str(item.get("secName") or "").strip(),
        title=_clean_title(str(item.get("announcementTitle") or "")),
        published_at=published_at,
        source_url=f"https://static.cninfo.com.cn/{adjunct}",
    )


RULES: tuple[
    tuple[tuple[str, ...], str, EventImpact, RiskLevel, float],
    ...,
] = (
    (("退市", "终止上市"), "delisting_risk", EventImpact.NEGATIVE, RiskLevel.CRITICAL, 0.95),
    (("立案", "处罚", "监管措施"), "regulatory_action", EventImpact.NEGATIVE, RiskLevel.HIGH, 0.90),
    (("风险提示", "异常波动"), "market_risk_warning", EventImpact.NEGATIVE, RiskLevel.HIGH, 0.85),
    (("减持",), "shareholder_reduction", EventImpact.NEGATIVE, RiskLevel.HIGH, 0.85),
    (("诉讼", "仲裁"), "litigation", EventImpact.NEGATIVE, RiskLevel.HIGH, 0.80),
    (("担保",), "guarantee", EventImpact.MIXED, RiskLevel.MEDIUM, 0.70),
    (("业绩预亏", "业绩下降", "亏损"), "earnings_warning", EventImpact.NEGATIVE, RiskLevel.HIGH, 0.85),
    (("业绩预增", "扭亏为盈"), "earnings_improvement", EventImpact.POSITIVE, RiskLevel.MEDIUM, 0.80),
    (("回购",), "share_buyback", EventImpact.POSITIVE, RiskLevel.MEDIUM, 0.75),
    (("增持",), "shareholder_increase", EventImpact.POSITIVE, RiskLevel.MEDIUM, 0.75),
    (("重大合同", "中标", "项目定点"), "major_business", EventImpact.POSITIVE, RiskLevel.MEDIUM, 0.70),
    (("重组", "重大资产重组"), "restructuring", EventImpact.MIXED, RiskLevel.HIGH, 0.75),
    (("停牌", "复牌"), "trading_status", EventImpact.MIXED, RiskLevel.HIGH, 0.85),
    (("解除限售", "限售股上市流通"), "share_unlock", EventImpact.NEGATIVE, RiskLevel.MEDIUM, 0.75),
    (("分红", "权益分派"), "dividend", EventImpact.NEUTRAL, RiskLevel.LOW, 0.70),
)

SCOPE_PREFIXES = ("000", "001", "002", "003", "300", "301", "600", "601", "603", "605")
ANNOUNCEMENT_INDEX_FIELDS = (
    "announcement_id",
    "symbol",
    "company_name",
    "title",
    "published_at",
    "usable_from_trade_date",
    "source_url",
    "source",
)
EVENT_INDEX_FIELDS = (
    "announcement_id",
    "symbol",
    "published_at",
    "usable_from_trade_date",
    "event_type",
    "impact_hint",
    "risk_level",
    "confidence",
    "summary",
    "source_url",
    "requires_fulltext",
    "analyzer",
)


def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _load_trade_calendar(path: Path) -> list[str]:
    if not path.exists():
        raise ValueError(f"缺少官方交易日历：{path}")
    with path.open(encoding="utf-8-sig", newline="") as handle:
        rows = list(csv.DictReader(handle))
    dates: set[str] = set()
    for row in rows:
        value = str(
            row.get("trade_date")
            or row.get("cal_date")
            or row.get("date")
            or ""
        ).replace("-", "")
        is_open = str(row.get("is_open") or "1").strip().lower()
        if len(value) == 8 and value.isdigit() and is_open not in {"0", "false"}:
            dates.add(value)
    if not dates:
        raise ValueError(f"官方交易日历为空：{path}")
    return sorted(dates)


def _next_trade_date(calendar: list[str], day: str) -> str:
    index = bisect.bisect_right(calendar, day)
    return calendar[index] if index < len(calendar) else ""


def _calendar_dates(start: str, end: str) -> list[str]:
    first = datetime.strptime(start, "%Y%m%d").date()
    last = datetime.strptime(end, "%Y%m%d").date()
    if first > last:
        raise ValueError("开始日期不能晚于结束日期")
    result: list[str] = []
    current = first
    while current <= last:
        result.append(current.strftime("%Y%m%d"))
        current += timedelta(days=1)
    return result


def classify_title(announcement: Announcement) -> AnnouncementEvent:
    title = announcement.title
    if (
        ("不存在" in title or "未受到" in title)
        and ("处罚" in title or "监管措施" in title)
    ):
        return AnnouncementEvent(
            announcement.announcement_id,
            announcement.symbol,
            "compliance_statement",
            EventImpact.NEUTRAL,
            RiskLevel.LOW,
            0.85,
            "标题说明不存在监管处罚或措施",
            (f"公告标题：{title}",),
            announcement.source_url,
            announcement.published_at,
            True,
        )
    if any(
        phrase in title
        for phrase in ("撤销退市风险警示", "解除退市风险警示", "申请撤销退市风险警示")
    ):
        return AnnouncementEvent(
            announcement.announcement_id,
            announcement.symbol,
            "delisting_risk_removal",
            EventImpact.POSITIVE,
            RiskLevel.MEDIUM,
            0.80,
            "标题涉及撤销或解除退市风险警示",
            (f"公告标题：{title}",),
            announcement.source_url,
            announcement.published_at,
            True,
        )
    for keywords, event_type, impact, risk, confidence in RULES:
        matched = tuple(keyword for keyword in keywords if keyword in title)
        if matched:
            return AnnouncementEvent(
                announcement_id=announcement.announcement_id,
                symbol=announcement.symbol,
                event_type=event_type,
                impact_hint=impact,
                risk_level=risk,
                confidence=confidence,
                summary=f"标题命中事件规则：{event_type}",
                evidence=tuple(f"标题包含“{keyword}”" for keyword in matched),
                source_url=announcement.source_url,
                published_at=announcement.published_at,
                requires_fulltext=True,
            )
    return AnnouncementEvent(
        announcement_id=announcement.announcement_id,
        symbol=announcement.symbol,
        event_type="other",
        impact_hint=EventImpact.UNKNOWN,
        risk_level=RiskLevel.LOW,
        confidence=0.30,
        summary="标题规则无法确定事件影响",
        evidence=(f"公告标题：{title}",),
        source_url=announcement.source_url,
        published_at=announcement.published_at,
        requires_fulltext=True,
    )


def build_processed_announcements(
    project: Path,
    trade_date: str,
    page_counts: dict[str, int] | None = None,
    *,
    trade_calendar: list[str] | None = None,
    page_stats: dict[str, dict[str, int | None]] | None = None,
) -> dict[str, Any]:
    raw_directory = project / "data" / "raw" / "cninfo" / trade_date
    announcements: dict[str, Announcement] = {}
    actual_page_counts: dict[str, int] = {}
    for column in ("szse", "sse"):
        paths = sorted(raw_directory.glob(f"{column}_*.json"))
        actual_page_counts[column] = len(paths)
        for path in paths:
            document = json.loads(path.read_text(encoding="utf-8"))
            for item in document.get("announcements") or []:
                announcement = parse_announcement(item)
                if announcement:
                    announcements[announcement.announcement_id] = announcement

    ordered = sorted(
        announcements.values(),
        key=lambda item: (item.published_at, item.symbol, item.announcement_id),
    )
    events = [classify_title(item) for item in ordered]
    usable_date = (
        _next_trade_date(trade_calendar, trade_date)
        if trade_calendar
        else ""
    )
    announcement_rows = [
        {
            **asdict(item),
            "in_scope": item.symbol.startswith(SCOPE_PREFIXES),
            "usable_from_trade_date": usable_date,
        }
        for item in ordered
    ]
    event_rows = [
        {
            **event.to_dict(),
            "usable_from_trade_date": usable_date,
        }
        for event in events
    ]
    output = {
        "schema_version": 1,
        "trade_date": trade_date,
        "downloaded_at": datetime.now(UTC).isoformat(),
        "source": "https://www.cninfo.com.cn/",
        "availability_rule": (
            "Conservative next official trading day after announcement date."
            if trade_calendar
            else "Unavailable: official trading calendar was not supplied."
        ),
        "page_counts": page_counts or actual_page_counts,
        "page_stats": page_stats or {},
        "announcement_count": len(ordered),
        "scope_announcement_count": sum(
            item.symbol.startswith(SCOPE_PREFIXES) for item in ordered
        ),
        "event_count": len(events),
        "scope_event_count": sum(
            event.symbol.startswith(SCOPE_PREFIXES) for event in events
        ),
        "announcements": announcement_rows,
        "events": event_rows,
    }
    processed = project / "data" / "processed" / "announcements"
    processed.mkdir(parents=True, exist_ok=True)
    path = processed / f"{trade_date}.json"
    path.write_text(json.dumps(output, ensure_ascii=False, indent=2), encoding="utf-8")
    output["processed_path"] = str(path)
    return output


def _query_with_retries(
    client: CninfoClient,
    trade_date: str,
    *,
    column: str,
    page_num: int,
    retries: int,
    retry_delay: float,
    page_size: int,
) -> dict[str, Any]:
    last_error: Exception | None = None
    for attempt in range(retries + 1):
        try:
            return client.query_page(
                trade_date,
                column=column,
                page_num=page_num,
                page_size=page_size,
            )
        except CninfoError as exc:
            last_error = exc
            if attempt < retries:
                time.sleep(retry_delay * (attempt + 1))
    assert last_error is not None
    raise last_error


def _reported_total(document: dict[str, Any]) -> int | None:
    return next(
        (
            int(document[key])
            for key in (
                "totalAnnouncement",
                "totalRecordNum",
                "totalRecords",
                "total",
            )
            if document.get(key) not in (None, "")
        ),
        None,
    )


def sync_announcements(
    project: Path,
    trade_date: str,
    *,
    trade_calendar: list[str] | None = None,
    retries: int = 3,
    retry_delay: float = 5,
    request_delay: float = 0.2,
    page_size: int = 30,
    client: CninfoClient | None = None,
) -> dict[str, Any]:
    if not 1 <= page_size <= 30:
        raise ValueError("巨潮历史公告 page_size 必须在 1 到 30 之间")
    client = client or CninfoClient()
    raw_directory = project / "data" / "raw" / "cninfo" / trade_date
    raw_directory.mkdir(parents=True, exist_ok=True)
    page_counts: dict[str, int] = {}
    page_stats: dict[str, dict[str, int | None]] = {}
    for column in ("szse", "sse"):
        first = _query_with_retries(
            client,
            trade_date,
            column=column,
            page_num=1,
            retries=retries,
            retry_delay=retry_delay,
            page_size=page_size,
        )
        reported_pages = int(
            first.get("totalpages") or first.get("totalPages") or 1
        )
        expected_raw = _reported_total(first)
        count_based_pages = (
            (expected_raw + page_size - 1) // page_size
            if expected_raw is not None
            else 1
        )
        total_pages = max(reported_pages, count_based_pages)
        pages = [first]
        for page_num in range(2, total_pages + 1):
            if request_delay:
                time.sleep(request_delay)
            pages.append(
                _query_with_retries(
                    client,
                    trade_date,
                    column=column,
                    page_num=page_num,
                    retries=retries,
                    retry_delay=retry_delay,
                    page_size=page_size,
                )
            )
        page_counts[column] = len(pages)
        raw_rows = sum(
            len(document.get("announcements") or [])
            for document in pages
        )
        page_stats[column] = {
            "reported_total": expected_raw,
            "received_rows": raw_rows,
            "reported_pages": reported_pages,
            "requested_pages": total_pages,
            "received_pages": len(pages),
        }
        if expected_raw is not None and raw_rows < expected_raw:
            raise CninfoError(
                f"{trade_date} {column} 分页不完整："
                f"reported={expected_raw}，received={raw_rows}"
            )
        for index, document in enumerate(pages, 1):
            path = raw_directory / f"{column}_{index:04d}.json"
            path.write_text(json.dumps(document, ensure_ascii=False, indent=2), encoding="utf-8")
    return build_processed_announcements(
        project,
        trade_date,
        page_counts,
        trade_calendar=trade_calendar,
        page_stats=page_stats,
    )


def build_announcement_archive(
    project: Path,
    *,
    start: str,
    end: str,
    trade_calendar: list[str] | None = None,
) -> dict[str, Any]:
    source = project / "data" / "processed" / "announcements"
    announcement_path = source / "announcement_index.csv"
    event_path = source / "event_index.csv"
    seen_announcements: set[str] = set()
    seen_events: set[tuple[str, str]] = set()
    announcement_count = 0
    event_count = 0
    duplicate_announcements = 0
    duplicate_events = 0
    missing_availability = 0
    pending_availability = 0
    calendar_right_edge = trade_calendar[-1] if trade_calendar else ""
    files = [
        path
        for path in sorted(source.glob("*.json"))
        if path.stem.isdigit() and start <= path.stem <= end
    ]
    with (
        announcement_path.open("w", encoding="utf-8", newline="") as ann_handle,
        event_path.open("w", encoding="utf-8", newline="") as event_handle,
    ):
        ann_writer = csv.DictWriter(
            ann_handle,
            fieldnames=ANNOUNCEMENT_INDEX_FIELDS,
        )
        event_writer = csv.DictWriter(
            event_handle,
            fieldnames=EVENT_INDEX_FIELDS,
        )
        ann_writer.writeheader()
        event_writer.writeheader()
        for path in files:
            document = json.loads(path.read_text(encoding="utf-8"))
            metadata: dict[str, dict[str, Any]] = {}
            for row in document.get("announcements", []):
                announcement_id = str(row.get("announcement_id") or "")
                metadata[announcement_id] = row
                if announcement_id in seen_announcements:
                    duplicate_announcements += 1
                    continue
                seen_announcements.add(announcement_id)
                if not row.get("usable_from_trade_date"):
                    if calendar_right_edge and path.stem >= calendar_right_edge:
                        pending_availability += 1
                    else:
                        missing_availability += 1
                ann_writer.writerow(
                    {field: row.get(field, "") for field in ANNOUNCEMENT_INDEX_FIELDS}
                )
                announcement_count += 1
            for row in document.get("events", []):
                announcement_id = str(row.get("announcement_id") or "")
                key = (announcement_id, str(row.get("symbol") or ""))
                if key in seen_events:
                    duplicate_events += 1
                    continue
                seen_events.add(key)
                available = str(
                    row.get("usable_from_trade_date")
                    or metadata.get(announcement_id, {}).get(
                        "usable_from_trade_date"
                    )
                    or ""
                )
                if not available:
                    if calendar_right_edge and path.stem >= calendar_right_edge:
                        pending_availability += 1
                    else:
                        missing_availability += 1
                output = {
                    **row,
                    "usable_from_trade_date": available,
                }
                event_writer.writerow(
                    {field: output.get(field, "") for field in EVENT_INDEX_FIELDS}
                )
                event_count += 1
    manifest = {
        "schema_version": 1,
        "status": (
            "validated"
            if files and not missing_availability
            else "failed_validation"
        ),
        "start_date": start,
        "end_date": end,
        "daily_files": len(files),
        "announcements": announcement_count,
        "events": event_count,
        "duplicate_announcements_skipped": duplicate_announcements,
        "duplicate_events_skipped": duplicate_events,
        "missing_availability_rows": missing_availability,
        "pending_availability_rows": pending_availability,
        "availability_rule": (
            "Conservative next official trading day after announcement date."
        ),
        "paths": {
            "announcement_index": str(announcement_path),
            "event_index": str(event_path),
        },
        "hashes": {
            "announcement_index.csv": _file_sha256(announcement_path),
            "event_index.csv": _file_sha256(event_path),
        },
        "generated_at": datetime.now(UTC).isoformat(),
    }
    manifest_path = source / "manifest.json"
    manifest_path.write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    manifest["manifest_path"] = str(manifest_path)
    return manifest


def backfill_announcements(
    project: Path,
    *,
    start: str,
    end: str,
    calendar_file: Path,
    max_days: int = 0,
    retries: int = 3,
    retry_delay: float = 5,
    request_delay: float = 0.2,
    day_delay: float = 0.5,
    page_size: int = 30,
    overwrite: bool = False,
    build_archive_after: bool = True,
    client: CninfoClient | None = None,
) -> dict[str, Any]:
    calendar = _load_trade_calendar(calendar_file)
    source = project / "data" / "processed" / "announcements"
    source.mkdir(parents=True, exist_ok=True)
    pending = [
        day
        for day in _calendar_dates(start, end)
        if overwrite or not (source / f"{day}.json").exists()
    ]
    selected = pending[:max_days] if max_days > 0 else pending
    completed = 0
    failed = 0
    for index, day in enumerate(selected, 1):
        try:
            result = sync_announcements(
                project,
                day,
                trade_calendar=calendar,
                retries=retries,
                retry_delay=retry_delay,
                request_delay=request_delay,
                page_size=page_size,
                client=client,
            )
            completed += 1
            print(
                f"[{index}/{len(selected)}] {day}："
                f"announcements={result['announcement_count']}，"
                f"events={result['event_count']}",
                flush=True,
            )
        except CninfoError as exc:
            failed += 1
            print(f"[{index}/{len(selected)}] {day}：failed={exc}", flush=True)
            break
        if day_delay and index < len(selected):
            time.sleep(day_delay)
    archive = (
        build_announcement_archive(
            project,
            start=start,
            end=end,
            trade_calendar=calendar,
        )
        if build_archive_after
        else {"status": "deferred"}
    )
    return {
        "status": "completed" if not failed else "partial",
        "start_date": start,
        "end_date": end,
        "calendar_days": len(_calendar_dates(start, end)),
        "pending_before_run": len(pending),
        "selected": len(selected),
        "completed": completed,
        "failed": failed,
        "archive": archive,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="同步和分类巨潮公告")
    parser.add_argument(
        "command",
        choices=["sync", "rebuild", "summary", "backfill", "build-archive"],
    )
    parser.add_argument("--date", help="YYYYMMDD")
    parser.add_argument("--start", help="YYYYMMDD")
    parser.add_argument("--end", help="YYYYMMDD")
    parser.add_argument("--root", default=".")
    parser.add_argument("--calendar-file")
    parser.add_argument("--max-days", type=int, default=0)
    parser.add_argument("--retries", type=int, default=3)
    parser.add_argument("--retry-delay", type=float, default=5)
    parser.add_argument("--request-delay", type=float, default=0.2)
    parser.add_argument("--day-delay", type=float, default=0.5)
    parser.add_argument("--page-size", type=int, default=30)
    parser.add_argument("--skip-archive", action="store_true")
    parser.add_argument("--overwrite", action="store_true")
    args = parser.parse_args()
    project = Path(args.root).resolve()
    calendar_file = (
        Path(args.calendar_file).expanduser().resolve()
        if args.calendar_file
        else project / "data" / "processed" / "trade_calendar.csv"
    )
    if args.command == "sync":
        if not args.date:
            parser.error("sync 需要 --date")
        result = sync_announcements(
            project,
            args.date,
            trade_calendar=_load_trade_calendar(calendar_file),
            retries=args.retries,
            retry_delay=args.retry_delay,
            request_delay=args.request_delay,
            page_size=args.page_size,
        )
    elif args.command == "rebuild":
        if not args.date:
            parser.error("rebuild 需要 --date")
        result = build_processed_announcements(
            project,
            args.date,
            trade_calendar=_load_trade_calendar(calendar_file),
        )
    elif args.command == "summary":
        if not args.date:
            parser.error("summary 需要 --date")
        path = project / "data" / "processed" / "announcements" / f"{args.date}.json"
        if not path.exists():
            raise SystemExit(f"公告数据不存在：{path}")
        result = json.loads(path.read_text(encoding="utf-8"))
    elif args.command == "build-archive":
        if not args.start or not args.end:
            parser.error("build-archive 需要 --start 和 --end")
        result = build_announcement_archive(
            project,
            start=args.start,
            end=args.end,
            trade_calendar=_load_trade_calendar(calendar_file),
        )
    else:
        if not args.start or not args.end:
            parser.error("backfill 需要 --start 和 --end")
        result = backfill_announcements(
            project,
            start=args.start,
            end=args.end,
            calendar_file=calendar_file,
            max_days=args.max_days,
            retries=args.retries,
            retry_delay=args.retry_delay,
            request_delay=args.request_delay,
            day_delay=args.day_delay,
            page_size=args.page_size,
            overwrite=args.overwrite,
            build_archive_after=not args.skip_archive,
        )
    if args.command in {"backfill", "build-archive"}:
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return
    events = result.get("events", [])
    risk_counts = {
        level.value: sum(event["risk_level"] == level.value for event in events)
        for level in RiskLevel
    }
    print(
        f"公告 {result.get('announcement_count', 0)}（范围内 {result.get('scope_announcement_count', 0)}），"
        f"事件 {len(events)}，"
        f"风险分布 {risk_counts}"
    )


if __name__ == "__main__":
    main()

from __future__ import annotations

import argparse
import json
import ssl
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
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
        except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as exc:
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
    output = {
        "schema_version": 1,
        "trade_date": trade_date,
        "downloaded_at": datetime.now(UTC).isoformat(),
        "source": "https://www.cninfo.com.cn/",
        "page_counts": page_counts or actual_page_counts,
        "announcement_count": len(ordered),
        "scope_announcement_count": sum(
            item.symbol.startswith(SCOPE_PREFIXES) for item in ordered
        ),
        "event_count": len(events),
        "scope_event_count": sum(
            event.symbol.startswith(SCOPE_PREFIXES) for event in events
        ),
        "announcements": [
            {**asdict(item), "in_scope": item.symbol.startswith(SCOPE_PREFIXES)}
            for item in ordered
        ],
        "events": [event.to_dict() for event in events],
    }
    processed = project / "data" / "processed" / "announcements"
    processed.mkdir(parents=True, exist_ok=True)
    path = processed / f"{trade_date}.json"
    path.write_text(json.dumps(output, ensure_ascii=False, indent=2), encoding="utf-8")
    output["processed_path"] = str(path)
    return output


def sync_announcements(project: Path, trade_date: str) -> dict[str, Any]:
    client = CninfoClient()
    raw_directory = project / "data" / "raw" / "cninfo" / trade_date
    raw_directory.mkdir(parents=True, exist_ok=True)
    page_counts: dict[str, int] = {}
    for column in ("szse", "sse"):
        first = client.query_page(trade_date, column=column, page_num=1)
        total_pages = int(first.get("totalpages") or first.get("totalPages") or 1)
        pages = [first]
        for page_num in range(2, total_pages + 1):
            pages.append(client.query_page(trade_date, column=column, page_num=page_num))
        page_counts[column] = len(pages)
        for index, document in enumerate(pages, 1):
            path = raw_directory / f"{column}_{index:04d}.json"
            path.write_text(json.dumps(document, ensure_ascii=False, indent=2), encoding="utf-8")
    return build_processed_announcements(project, trade_date, page_counts)


def main() -> None:
    parser = argparse.ArgumentParser(description="同步和分类巨潮公告")
    parser.add_argument("command", choices=["sync", "rebuild", "summary"])
    parser.add_argument("--date", required=True, help="YYYYMMDD")
    parser.add_argument("--root", default=".")
    args = parser.parse_args()
    project = Path(args.root).resolve()
    if args.command == "sync":
        result = sync_announcements(project, args.date)
    elif args.command == "rebuild":
        result = build_processed_announcements(project, args.date)
    else:
        path = project / "data" / "processed" / "announcements" / f"{args.date}.json"
        if not path.exists():
            raise SystemExit(f"公告数据不存在：{path}")
        result = json.loads(path.read_text(encoding="utf-8"))
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

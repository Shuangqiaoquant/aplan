from __future__ import annotations

import argparse
import hashlib
import json
import re
import ssl
import urllib.error
import urllib.request
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any


@dataclass(frozen=True, slots=True)
class FulltextRecord:
    announcement_id: str
    symbol: str
    title: str
    source_url: str
    pdf_path: str
    pdf_sha256: str
    page_count: int
    character_count: int
    extraction_status: str
    text_path: str


@dataclass(frozen=True, slots=True)
class FulltextAnalysis:
    announcement_id: str
    symbol: str
    event_type: str
    conclusion: str
    confidence: float
    facts: tuple[str, ...]
    positive_evidence: tuple[str, ...]
    negative_evidence: tuple[str, ...]
    uncertainties: tuple[str, ...]
    source_url: str
    pdf_sha256: str
    analyzer: str = "fulltext_rules_v1"
    actionable_signal_created: bool = False


def _ssl_context() -> ssl.SSLContext:
    system_ca = Path("/etc/ssl/cert.pem")
    return (
        ssl.create_default_context(cafile=str(system_ca))
        if system_ca.exists()
        else ssl.create_default_context()
    )


def download_pdf(url: str, path: Path) -> str:
    request = urllib.request.Request(
        url,
        headers={
            "User-Agent": "Mozilla/5.0 APlanResearch/0.1",
            "Referer": "https://www.cninfo.com.cn/",
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=60, context=_ssl_context()) as response:
            content = response.read()
    except (urllib.error.URLError, TimeoutError) as exc:
        raise RuntimeError(f"PDF下载失败：{exc}") from exc
    if not content.startswith(b"%PDF"):
        raise RuntimeError("下载内容不是PDF")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(content)
    return hashlib.sha256(content).hexdigest()


def extract_pdf_text(path: Path) -> tuple[str, int]:
    try:
        from pypdf import PdfReader
    except ImportError as exc:
        raise RuntimeError(
            "缺少 pypdf；请使用工作区捆绑Python或安装项目的 pdf 可选依赖"
        ) from exc
    reader = PdfReader(str(path))
    pages: list[str] = []
    for page in reader.pages:
        pages.append(page.extract_text() or "")
    return "\n\n".join(pages), len(reader.pages)


def _snippets(text: str, keywords: tuple[str, ...], limit: int = 5) -> tuple[str, ...]:
    lines = [
        re.sub(r"\s+", " ", line).strip()
        for line in text.splitlines()
        if line.strip()
    ]
    matches: list[str] = []
    for line in lines:
        if any(keyword in line for keyword in keywords):
            excerpt = line[:240]
            if excerpt not in matches:
                matches.append(excerpt)
            if len(matches) >= limit:
                break
    return tuple(matches)


def analyze_fulltext(
    event: dict[str, Any],
    announcement: dict[str, Any],
    text: str,
    pdf_sha256: str,
) -> FulltextAnalysis:
    facts = _snippets(
        text,
        ("人民币", "万元", "亿元", "%", "股", "截至", "预计", "收到", "决定"),
        6,
    )
    negative = _snippets(
        text,
        (
            "风险",
            "不确定性",
            "亏损",
            "立案",
            "处罚",
            "减持",
            "诉讼",
            "终止上市",
            "退市",
            "无法",
            "尚未",
        ),
        6,
    )
    positive = _snippets(
        text,
        ("回购", "增持", "中标", "增长", "扭亏", "合同", "批准", "通过"),
        4,
    )
    uncertainties: list[str] = []
    if len(text.strip()) < 200:
        uncertainties.append("可提取文本过少，可能是扫描件或受保护PDF")
    if not facts:
        uncertainties.append("未提取到包含金额、比例或关键日期的事实句")
    uncertainties.append("规则分析不能判断信息是否已被市场价格充分反映")
    uncertainties.append("必须由事件回测验证该类公告是否具有可交易超额收益")

    event_type = str(event.get("event_type", "other"))
    high_risk = str(event.get("risk_level")) in {"high", "critical"}
    conclusion = "risk_review_required" if high_risk else "human_review_required"
    confidence = min(float(event.get("confidence", 0.3)), 0.70)
    return FulltextAnalysis(
        announcement_id=str(event["announcement_id"]),
        symbol=str(event["symbol"]),
        event_type=event_type,
        conclusion=conclusion,
        confidence=confidence,
        facts=facts,
        positive_evidence=positive,
        negative_evidence=negative,
        uncertainties=tuple(uncertainties),
        source_url=str(announcement["source_url"]),
        pdf_sha256=pdf_sha256,
    )


def process_fulltexts(
    project: Path,
    trade_date: str,
    *,
    risk_levels: set[str],
    limit: int,
) -> dict[str, Any]:
    source_path = (
        project
        / "data"
        / "processed"
        / "announcements"
        / f"{trade_date}.json"
    )
    source = json.loads(source_path.read_text(encoding="utf-8"))
    announcements = {
        item["announcement_id"]: item
        for item in source.get("announcements", [])
    }
    candidates = [
        event
        for event in source.get("events", [])
        if event.get("risk_level") in risk_levels
        and announcements.get(event["announcement_id"], {}).get("in_scope")
    ]
    risk_order = {"critical": 0, "high": 1, "medium": 2, "low": 3}
    candidates.sort(
        key=lambda event: (
            risk_order.get(str(event.get("risk_level")), 9),
            str(event.get("symbol")),
            str(event.get("announcement_id")),
        )
    )
    candidates = candidates[:limit]

    records: list[FulltextRecord] = []
    analyses: list[FulltextAnalysis] = []
    failures: list[dict[str, str]] = []
    for event in candidates:
        announcement = announcements[event["announcement_id"]]
        announcement_id = str(event["announcement_id"])
        pdf_path = (
            project
            / "data"
            / "raw"
            / "cninfo"
            / trade_date
            / "pdfs"
            / f"{announcement_id}.pdf"
        )
        text_path = (
            project
            / "data"
            / "processed"
            / "announcement_text"
            / trade_date
            / f"{announcement_id}.txt"
        )
        try:
            pdf_hash = (
                hashlib.sha256(pdf_path.read_bytes()).hexdigest()
                if pdf_path.exists()
                else download_pdf(announcement["source_url"], pdf_path)
            )
            text, pages = extract_pdf_text(pdf_path)
            text_path.parent.mkdir(parents=True, exist_ok=True)
            text_path.write_text(text, encoding="utf-8")
            status = (
                "needs_ocr"
                if pages and len(text.strip()) / pages < 50
                else "extracted"
            )
            record = FulltextRecord(
                announcement_id,
                str(event["symbol"]),
                str(announcement["title"]),
                str(announcement["source_url"]),
                str(pdf_path),
                pdf_hash,
                pages,
                len(text),
                status,
                str(text_path),
            )
            records.append(record)
            analyses.append(
                analyze_fulltext(event, announcement, text, pdf_hash)
            )
        except Exception as exc:  # 单份公告失败不能丢弃其他结果
            failures.append(
                {
                    "announcement_id": announcement_id,
                    "symbol": str(event["symbol"]),
                    "error": str(exc),
                }
            )

    output = {
        "schema_version": 1,
        "trade_date": trade_date,
        "processed_at": datetime.now(UTC).isoformat(),
        "requested": len(candidates),
        "completed": len(records),
        "failed": len(failures),
        "needs_ocr": sum(record.extraction_status == "needs_ocr" for record in records),
        "records": [asdict(record) for record in records],
        "analyses": [asdict(analysis) for analysis in analyses],
        "failures": failures,
    }
    output_path = (
        project
        / "data"
        / "processed"
        / "announcement_analysis"
        / f"{trade_date}.json"
    )
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(
        json.dumps(output, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    output["output_path"] = str(output_path)
    return output


def main() -> None:
    parser = argparse.ArgumentParser(description="下载并分析巨潮公告PDF全文")
    parser.add_argument("command", choices=["process", "summary"])
    parser.add_argument("--date", required=True)
    parser.add_argument("--risk", default="critical,high")
    parser.add_argument("--limit", type=int, default=10)
    parser.add_argument("--root", default=".")
    args = parser.parse_args()
    project = Path(args.root).resolve()
    if args.command == "process":
        result = process_fulltexts(
            project,
            args.date,
            risk_levels={item.strip() for item in args.risk.split(",") if item.strip()},
            limit=args.limit,
        )
    else:
        path = (
            project
            / "data"
            / "processed"
            / "announcement_analysis"
            / f"{args.date}.json"
        )
        result = json.loads(path.read_text(encoding="utf-8"))
    print(
        f"请求 {result['requested']}，完成 {result['completed']}，"
        f"失败 {result['failed']}，需OCR {result['needs_ocr']}"
    )


if __name__ == "__main__":
    main()


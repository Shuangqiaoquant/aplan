from __future__ import annotations

import argparse
import hashlib
import json
import os
import subprocess
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from .audit import audit_files
from .report_insights import build_daily_insights

DEFAULT_RECIPIENT = os.environ.get("APLAN_EMAIL_RECIPIENT", "").strip()
DEFAULT_SENDER = os.environ.get("APLAN_EMAIL_SENDER", "").strip()


def normalize_recipients(recipient: str) -> list[str]:
    recipients = [
        item.strip()
        for item in recipient.replace(";", ",").split(",")
        if item.strip()
    ]
    if not recipients:
        raise ValueError("至少需要一个收件人")
    return recipients


def recipients_arg(recipient: str) -> str:
    return ",".join(normalize_recipients(recipient))


def latest_audit(project: Path) -> tuple[Path, dict[str, Any]]:
    files = audit_files(project)
    if not files:
        raise RuntimeError("没有可发送的审计记录")
    path = files[-1]
    return path, json.loads(path.read_text(encoding="utf-8"))


def corresponding_report(project: Path, audit: dict[str, Any], audit_path: Path) -> Path:
    exact = (
        project
        / "reports"
        / "daily"
        / audit["trade_date"]
        / f"{audit_path.stem}.md"
    )
    if exact.exists():
        return exact
    candidates = sorted(
        (project / "reports" / "daily" / audit["trade_date"]).glob("*.md")
    )
    if not candidates:
        raise RuntimeError("找不到对应的每日研究报告")
    return candidates[-1]


def _important_events(project: Path, trade_date: str, limit: int = 10) -> list[dict[str, Any]]:
    path = (
        project
        / "data"
        / "processed"
        / "announcements"
        / f"{trade_date}.json"
    )
    if not path.exists():
        return []
    document = json.loads(path.read_text(encoding="utf-8"))
    announcements = {
        item["announcement_id"]: item
        for item in document.get("announcements", [])
    }
    risk_order = {"critical": 0, "high": 1}
    events = [
        event
        for event in document.get("events", [])
        if event.get("risk_level") in risk_order
        and announcements.get(event["announcement_id"], {}).get("in_scope")
    ]
    events.sort(
        key=lambda event: (
            risk_order[event["risk_level"]],
            event["symbol"],
            event["announcement_id"],
        )
    )
    return [
        {
            **event,
            "title": announcements[event["announcement_id"]]["title"],
        }
        for event in events[:limit]
    ]


def build_email(
    project: Path,
    audit: dict[str, Any],
) -> tuple[str, str]:
    stages = audit.get("stages", {})
    quality = stages.get("quality", {})
    announcements = stages.get("announcements", {})
    fulltext = stages.get("announcement_fulltext", {})
    strategy = stages.get("strategy", {})
    portfolio = stages.get("portfolio", {})
    paper_watchlist = stages.get("paper_watchlist", {})
    important = _important_events(project, audit["trade_date"])
    if audit.get("insights"):
        insights = audit["insights"]
    else:
        try:
            insights = build_daily_insights(project, audit["trade_date"])
        except Exception:
            insights = {}
    data_ok = bool(quality.get("passed"))
    has_high_risk = announcements.get("high_risk_count", 0) > 0
    execution_open = (
        audit.get("mode") != "research_only"
        and bool(audit.get("strategy_approved"))
        and bool(strategy.get("execution_allowed"))
        and bool(portfolio.get("execution_allowed"))
    )
    if audit.get("status") == "failed" or not data_ok:
        verdict = "🔴 今日不操作：数据/流程未通过"
    elif has_high_risk:
        verdict = "🟡 今日先看风险公告"
    elif execution_open:
        verdict = "🟢 执行闸门已开，但仍需人工复核"
    else:
        verdict = "🟢 研究流程正常，仅观察"
    subject = (
        f"[APlan] {audit['trade_date']} 每日研究摘要"
        f"｜高风险 {announcements.get('high_risk_count', 0)}"
    )
    lines = [
        f"APlan 每日研究摘要 - {audit['trade_date']}",
        "",
        f"今日结论：{verdict}",
        "",
        "【一屏摘要】",
        f"- 总状态：{audit.get('status')}",
        f"- 数据质量：{'通过' if data_ok else '未通过'}｜行情 {quality.get('row_count', 0):,} 行｜证券 {quality.get('unique_symbols', 0):,} 只",
        f"- 策略信号：{strategy.get('signal_count', 0)}｜可执行信号：{strategy.get('signal_count', 0) if strategy.get('execution_allowed') else 0}｜策略状态：{strategy.get('status', '未运行')}",
        f"- 模拟观察：新增 {paper_watchlist.get('added', 0)}｜总观察 {paper_watchlist.get('total', 0)}｜状态 {paper_watchlist.get('status', '未运行')}",
        f"- 风险公告：高风险 {announcements.get('high_risk_count', 0)}｜公告总数 {announcements.get('announcement_count', 0)}",
        f"- 全文处理：完成/失败/OCR {fulltext.get('completed', 0)}/{fulltext.get('failed', 0)}/{fulltext.get('needs_ocr', 0)}",
        f"- 组合订单：{len(portfolio.get('accepted_orders', [])) if 'accepted_orders' in portfolio else portfolio.get('orders', 0)}｜执行闸门：{'打开' if execution_open else '关闭'}",
        "",
        "【候选股前 3】",
    ]
    candidate_top = insights.get("candidate_top") or {}
    candidate_items = candidate_top.get("items", []) if candidate_top.get("status") == "available" else []
    if candidate_items:
        for item in candidate_items[:3]:
            lines.append(
                f"- {item.get('symbol')}｜{float(item.get('score', 0)):.1f}分｜{item.get('action')}｜{item.get('decision_band')}"
            )
    else:
        lines.append(f"- 暂无：{candidate_top.get('reason', '候选数据不可用')}")
    lines.extend(
        [
            "",
            "【持仓风险前 3】",
        ]
    )
    holding_risk = insights.get("holding_risk") or {}
    holding_items = holding_risk.get("items", []) if holding_risk.get("status") == "available" else []
    if holding_items:
        for item in holding_items[:3]:
            ret = item.get("unrealized_return")
            ret_text = "未知" if ret is None else f"{float(ret):.1%}"
            lines.append(
                f"- {item.get('symbol')}｜{item.get('risk_level')}｜{item.get('action')}｜浮动收益 {ret_text}"
            )
    else:
        lines.append(f"- 暂无：{holding_risk.get('reason', '没有登记持仓')}")
    lines.extend(
        [
            "",
            "【今日动作】",
            "- 若数据质量未通过：不依据本报告买卖，先修复数据。",
            "- 若出现高风险公告：先看风险，再看机会。",
            "- 未通过策略/模拟盘/运行模式三重闸门前，所有信号只进入研究或观察。",
            "",
            "重点风险公告：",
        ]
    )
    if important:
        for event in important:
            lines.extend(
                [
                    f"- {event['symbol']} [{event['risk_level']}] {event['title']}",
                    f"  类型：{event['event_type']}",
                    f"  原文：{event['source_url']}",
                ]
            )
    else:
        lines.append("- 无")
    lines.extend(
        [
            "",
            "详细Markdown报告已作为附件。",
            "当前系统仅用于研究，不构成投资建议；没有连接真实券商。",
        ]
    )
    return subject, "\n".join(lines)


def notification_state_path(project: Path) -> Path:
    return project / "state" / "notifications" / "email.json"


def send_latest(
    project: Path,
    *,
    recipient: str = DEFAULT_RECIPIENT,
    sender: str = DEFAULT_SENDER,
    force: bool = False,
) -> dict[str, Any]:
    audit_path, audit = latest_audit(project)
    report_path = corresponding_report(project, audit, audit_path)
    audit_hash = hashlib.sha256(audit_path.read_bytes()).hexdigest()
    state_path = notification_state_path(project)
    if state_path.exists() and not force:
        state = json.loads(state_path.read_text(encoding="utf-8"))
        if (
            state.get("audit_sha256") == audit_hash
            and state.get("recipients", [state.get("recipient")])
            == normalize_recipients(recipient)
        ):
            return {
                "status": "skipped_duplicate",
                "audit_path": str(audit_path),
                "recipient": recipients_arg(recipient),
                "recipients": normalize_recipients(recipient),
            }

    subject, body = build_email(project, audit)
    script = project / "scripts" / "send_mail.applescript"
    completed = subprocess.run(
        [
            "osascript",
            str(script),
            recipients_arg(recipient),
            sender,
            subject,
            body,
            str(report_path),
        ],
        check=True,
        capture_output=True,
        text=True,
    )
    state_path.parent.mkdir(parents=True, exist_ok=True)
    state = {
        "status": "sent",
        "sent_at": datetime.now(UTC).isoformat(),
        "recipient": recipients_arg(recipient),
        "recipients": normalize_recipients(recipient),
        "sender": sender,
        "subject": subject,
        "audit_path": str(audit_path),
        "audit_sha256": audit_hash,
        "report_path": str(report_path),
        "mailer_result": completed.stdout.strip(),
    }
    state_path.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")
    return state


def send_test(
    project: Path,
    *,
    recipient: str = DEFAULT_RECIPIENT,
    sender: str = DEFAULT_SENDER,
) -> dict[str, Any]:
    script = project / "scripts" / "send_mail.applescript"
    subject = "[APlan] 邮件通知配置测试"
    body = (
        "APlan 邮件通知已成功调用 Mac 邮件App。\n\n"
        "后续系统会在每日固定时间发送研究摘要和报告附件。\n"
        "当前系统仅用于研究，不会自动创建交易订单。"
    )
    completed = subprocess.run(
        ["osascript", str(script), recipients_arg(recipient), sender, subject, body, ""],
        check=True,
        capture_output=True,
        text=True,
    )
    return {
        "status": "sent",
        "recipient": recipients_arg(recipient),
        "recipients": normalize_recipients(recipient),
        "sender": sender,
        "subject": subject,
        "mailer_result": completed.stdout.strip(),
    }


def send_file(
    project: Path,
    *,
    report_path: str | Path,
    subject: str,
    body: str,
    recipient: str = DEFAULT_RECIPIENT,
    sender: str = DEFAULT_SENDER,
) -> dict[str, Any]:
    attachment = Path(report_path)
    if not attachment.is_absolute():
        attachment = project / attachment
    if not attachment.exists():
        raise FileNotFoundError(attachment)
    script = project / "scripts" / "send_mail.applescript"
    completed = subprocess.run(
        [
            "osascript",
            str(script),
            recipients_arg(recipient),
            sender,
            subject,
            body,
            str(attachment),
        ],
        check=True,
        capture_output=True,
        text=True,
    )
    return {
        "status": "sent",
        "sent_at": datetime.now(UTC).isoformat(),
        "recipient": recipients_arg(recipient),
        "recipients": normalize_recipients(recipient),
        "sender": sender,
        "subject": subject,
        "report_path": str(attachment),
        "mailer_result": completed.stdout.strip(),
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="通过Mac邮件App发送APlan通知")
    parser.add_argument("command", choices=["test", "send-latest", "send-file"])
    parser.add_argument("--recipient", default=DEFAULT_RECIPIENT)
    parser.add_argument("--sender", default=DEFAULT_SENDER)
    parser.add_argument("--root", default=".")
    parser.add_argument("--force", action="store_true")
    parser.add_argument("--file", help="send-file 附件路径")
    parser.add_argument("--subject", help="send-file 邮件主题")
    parser.add_argument("--body", help="send-file 邮件正文")
    args = parser.parse_args()
    project = Path(args.root).resolve()
    if args.command == "test":
        result = send_test(project, recipient=args.recipient, sender=args.sender)
    elif args.command == "send-latest":
        result = send_latest(
            project,
            recipient=args.recipient,
            sender=args.sender,
            force=args.force,
        )
    else:
        if not args.file or not args.subject:
            raise SystemExit("send-file 必须提供 --file 和 --subject")
        result = send_file(
            project,
            report_path=args.file,
            subject=args.subject,
            body=args.body or "专项研究报告见附件。\n\n当前系统仅用于研究，不构成投资建议。",
            recipient=args.recipient,
            sender=args.sender,
        )
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()

from __future__ import annotations

from pathlib import Path
from typing import Any

from .report_insights import build_daily_insights


def _yes(value: bool) -> str:
    return "是" if value else "否"


def _status_badge(status: str | None) -> str:
    if not status:
        return "⚪ 未运行"
    if status in {"passed", "passed_research_only", "completed", "available", "planned", "updated"}:
        return f"🟢 {status}"
    if status in {"skipped", "skipped_market_closed", "not_synced", "not_processed", "not_initialized", "no_registered_strategy"}:
        return f"🟡 {status}"
    if status in {"failed", "blocked"}:
        return f"🔴 {status}"
    return f"⚪ {status}"


def _risk_badge(level: str) -> str:
    return {
        "low": "🟢 低",
        "medium": "🟡 中",
        "high": "🔴 高",
    }.get(level, f"⚪ {level}")


def _bar(value: int, maximum: int, *, width: int = 10) -> str:
    if maximum <= 0:
        return "░" * width
    filled = max(0, min(width, round(width * value / maximum)))
    return "█" * filled + "░" * (width - filled)


def _stage_icon(status: str | None) -> str:
    if status in {"passed", "completed", "available", "planned"}:
        return "✅"
    if status in {"skipped", "not_synced", "not_processed", "not_initialized", "no_registered_strategy"}:
        return "⏸️"
    if status in {"failed", "blocked"}:
        return "⚠️"
    return "⬜"


def _stage_health_bar(status: str | None) -> str:
    if status in {"passed", "completed", "available", "planned"}:
        return _bar(10, 10)
    if status in {"skipped", "not_synced", "not_processed", "not_initialized", "no_registered_strategy"}:
        return _bar(5, 10)
    if status in {"failed", "blocked"}:
        return _bar(2, 10)
    return _bar(0, 10)


def _execution_gate(audit: dict[str, Any], strategy: dict[str, Any], portfolio: dict[str, Any]) -> bool:
    return (
        audit.get("mode") != "research_only"
        and bool(audit.get("strategy_approved"))
        and bool(strategy.get("execution_allowed"))
        and bool(portfolio.get("execution_allowed"))
    )


def _today_verdict(
    audit: dict[str, Any],
    quality: dict[str, Any],
    strategy: dict[str, Any],
    portfolio: dict[str, Any],
    announcements: dict[str, Any],
) -> tuple[str, str]:
    if audit.get("status") == "failed" or not quality.get("passed", False):
        return "🔴 今日不操作：先修复数据/流程", "high"
    if announcements.get("high_risk_count", 0):
        return "🟡 今日重点看风险公告，候选只观察", "medium"
    if _execution_gate(audit, strategy, portfolio):
        return "🟢 执行闸门已打开：仍需人工复核", "medium"
    return "🟢 研究流程正常：仅观察，不交易", "low"


def _action_items(
    audit: dict[str, Any],
    quality: dict[str, Any],
    strategy: dict[str, Any],
    portfolio: dict[str, Any],
    announcements: dict[str, Any],
    fulltext: dict[str, Any],
) -> list[str]:
    actions: list[str] = []
    if audit.get("status") == "failed" or not quality.get("passed", False):
        actions.append("先处理数据质量/流程失败；今天不根据本报告做任何买卖动作。")
    if announcements.get("high_risk_count", 0):
        actions.append(f"优先审阅 {announcements.get('high_risk_count', 0)} 条高风险公告。")
    if fulltext.get("failed", 0) or fulltext.get("needs_ocr", 0):
        actions.append(
            f"公告全文处理存在缺口：失败 {fulltext.get('failed', 0)}，需要 OCR {fulltext.get('needs_ocr', 0)}。"
        )
    if strategy.get("signal_count", 0):
        actions.append(f"复核 {strategy.get('signal_count', 0)} 个研究信号；未通过闸门前只进入观察账本。")
    if portfolio.get("circuit_breaker"):
        actions.append("组合触发回撤熔断，任何加仓想法都应暂停。")
    if not actions:
        actions.append("无紧急动作；保持研究观察，等待候选、风险或数据出现新变化。")
    actions.append("当前系统不连接真实券商，不自动下单。")
    return actions


def _safe_daily_insights(project: Path | None, audit: dict[str, Any]) -> dict[str, Any]:
    if audit.get("insights"):
        return audit["insights"]
    if project is None:
        return {}
    try:
        return build_daily_insights(project, str(audit["trade_date"]))
    except Exception as exc:  # pragma: no cover - report rendering must not break email delivery
        return {"status": "failed", "reason": str(exc)}


def _format_return(value: float | None) -> str:
    return "未知" if value is None else f"{value:.1%}"


def _render_candidate_section(insights: dict[str, Any]) -> list[str]:
    candidate_top = insights.get("candidate_top") or {}
    lines = ["", "## 候选股 Top N", ""]
    if candidate_top.get("status") != "available":
        lines.append(f"- 暂不可用：{candidate_top.get('reason', '缺少候选数据')}")
        return lines
    lines.append(
        f"- 数据日期：{candidate_top.get('as_of')}｜周期：{candidate_top.get('horizon', 'swing')}｜来源：本地 processed 日线"
    )
    items = candidate_top.get("items", [])
    if not items:
        lines.append("- 今日没有满足条件的候选股。")
        return lines
    lines.extend(
        [
            "",
            "| 股票 | 分数 | 分层 | 风格 | 今日动作 | 主要理由 | 主要风险/缺口 |",
            "|---|---:|---|---|---|---|---|",
        ]
    )
    for item in items:
        reasons = "；".join(item.get("reasons") or []) or "无"
        risks = "；".join(item.get("risks") or []) or "无"
        lines.append(
            f"| {item.get('symbol')} | {float(item.get('score', 0)):.1f} | "
            f"{item.get('decision_band', '未知')} | {item.get('entry_style', '未知')} | "
            f"{item.get('action', '观察')} | {reasons} | {risks} |"
        )
    return lines


def _render_holding_risk_section(insights: dict[str, Any]) -> list[str]:
    holding_risk = insights.get("holding_risk") or {}
    lines = ["", "## 持仓风险雷达", ""]
    if holding_risk.get("status") != "available":
        lines.append(f"- 暂不可用：{holding_risk.get('reason', '缺少持仓或行情数据')}")
        return lines
    lines.append(f"- 审查行情日期：{holding_risk.get('as_of')}｜来源：本地 processed 日线 + 观察/组合账本")
    items = holding_risk.get("items", [])
    if not items:
        lines.append("- 当前没有登记的纸面组合持仓或人工风险持仓。")
        return lines
    lines.extend(
        [
            "",
            "| 股票 | 来源 | 浮动收益 | 动作 | 风险 | 主要原因 |",
            "|---|---|---:|---|---|---|",
        ]
    )
    for item in items:
        reasons = "；".join(item.get("reasons") or []) or "无"
        lines.append(
            f"| {item.get('symbol')} | {item.get('source')} | {_format_return(item.get('unrealized_return'))} | "
            f"{item.get('action')} | {_risk_badge(str(item.get('risk_level', 'unknown')))} | {reasons} |"
        )
    return lines


def render_daily_audit(audit: dict[str, Any], audit_path: Path, project: Path | None = None) -> str:
    quality = audit.get("stages", {}).get("quality", {})
    strategy = audit.get("stages", {}).get("strategy", {})
    portfolio = audit.get("stages", {}).get("portfolio", {})
    paper_watchlist = audit.get("stages", {}).get("paper_watchlist", {})
    paper = audit.get("stages", {}).get("paper_simulation", {})
    announcements = audit.get("stages", {}).get("announcements", {})
    fulltext = audit.get("stages", {}).get("announcement_fulltext", {})
    warnings = quality.get("warnings", [])
    errors = quality.get("errors", [])
    execution_allowed = _execution_gate(audit, strategy, portfolio)
    verdict, risk_level = _today_verdict(audit, quality, strategy, portfolio, announcements)
    insights = _safe_daily_insights(project, audit)
    stage_statuses = [
        ("数据", quality.get("status", "passed" if quality.get("passed") else "failed")),
        ("策略", strategy.get("status", "未运行")),
        ("组合", portfolio.get("status", "未运行")),
        ("公告", announcements.get("status", "未运行")),
        ("全文", fulltext.get("status", "未运行")),
    ]
    lines = [
        f"# APlan 每日研究驾驶舱｜{audit['trade_date']}",
        "",
        f"> 今日结论：{verdict}",
        "",
        "## 一屏摘要",
        "",
        "| 模块 | 状态 | 关键数字 | 解读 |",
        "|---|---|---:|---|",
        f"| 总状态 | {_status_badge(audit.get('status'))} | - | 运行模式 `{audit['mode']}` |",
        f"| 今日风险 | {_risk_badge(risk_level)} | 高风险公告 {announcements.get('high_risk_count', 0)} | 风险先于机会 |",
        f"| 数据质量 | {'🟢 通过' if quality.get('passed') else '🔴 未通过'} | {quality.get('row_count', 0):,} 行 | 唯一证券 {quality.get('unique_symbols', 0):,} |",
        f"| 策略信号 | {_status_badge(strategy.get('status', '未运行'))} | {strategy.get('signal_count', 0)} 个 | 注册策略 {strategy.get('registered_count', 0)} |",
        f"| 组合订单 | {_status_badge(portfolio.get('status', '未运行'))} | {len(portfolio.get('accepted_orders', [])) if 'accepted_orders' in portfolio else portfolio.get('orders', 0)} 个 | 回撤熔断：{_yes(bool(portfolio.get('circuit_breaker')))} |",
        f"| 模拟观察 | {_status_badge(paper_watchlist.get('status', '未运行'))} | 新增 {paper_watchlist.get('added', 0)} 个 | 总观察 {paper_watchlist.get('total', 0)} 个 |",
        f"| 执行闸门 | {'🟢 允许' if execution_allowed else '🔴 关闭'} | - | 策略批准：{_yes(bool(audit.get('strategy_approved')))}；允许执行：{_yes(execution_allowed)} |",
        "",
        "## 今日行动卡片",
        "",
    ]
    lines.extend(f"- {item}" for item in _action_items(audit, quality, strategy, portfolio, announcements, fulltext))
    lines.extend(_render_candidate_section(insights))
    lines.extend(_render_holding_risk_section(insights))
    lines.extend(
        [
            "",
            "## 阶段健康度",
            "",
            "| 阶段 | 结果 | 可视化 |",
            "|---|---|---|",
        ]
    )
    for name, status in stage_statuses:
        lines.append(f"| {name} | {_stage_icon(status)} `{status}` | `{_stage_health_bar(status)}` |")
    lines.extend(
        [
            "",
            "## 数据质量",
            "",
            "| 项目 | 结果 |",
            "|---|---:|",
            f"| 检查通过 | {_yes(bool(quality.get('passed')))} |",
            f"| 行数 | {quality.get('row_count', 0):,} |",
            f"| 唯一证券 | {quality.get('unique_symbols', 0):,} |",
            f"| 重复记录 | {int(quality.get('metrics', {}).get('duplicate_rows', 0))} |",
            f"| OHLC异常 | {int(quality.get('metrics', {}).get('invalid_ohlc_rows', 0))} |",
            f"| 数据SHA-256 | `{quality.get('sha256', '无')}` |",
        ]
    )
    lines.extend(
        [
            "",
            "## 策略与信号",
            "",
            f"- 状态：`{strategy.get('status', '未运行')}`",
            f"- 注册策略：{strategy.get('registered_count', 0)}",
            f"- 信号数量：{strategy.get('signal_count', 0)}",
            f"- 允许执行：{_yes(bool(strategy.get('execution_allowed')))}",
            f"- 信号集SHA-256：`{strategy.get('signal_set_sha256', '无')}`",
        ]
    )
    runs = strategy.get("runs", [])
    if runs:
        lines.extend(["", "| 策略 | 版本 | 状态 | 信号 | 阻断原因 |", "|---|---|---|---:|---|"])
        for run in runs:
            lines.append(
                f"| {run.get('strategy_id', '未知')} | {run.get('version', '未知')} | "
                f"{run.get('status', '未知')} | {run.get('signal_count', 0)} | {run.get('blocked_reason') or '无'} |"
            )
    lines.extend(
        [
            "",
            "## 组合与订单",
            "",
            f"- 状态：`{portfolio.get('status', '未运行')}`",
            f"- 订单数量：{len(portfolio.get('accepted_orders', [])) if 'accepted_orders' in portfolio else portfolio.get('orders', 0)}",
            f"- 允许执行：{_yes(bool(portfolio.get('execution_allowed')))}",
            f"- 回撤熔断：{_yes(bool(portfolio.get('circuit_breaker')))}",
        ]
    )
    if portfolio.get("reason"):
        lines.append(f"- 说明：{portfolio.get('reason')}")
    lines.extend(
        [
            "",
            "## 纸面模拟",
            "",
            f"- 观察账本：`{paper_watchlist.get('status', '未运行')}`；新增 {paper_watchlist.get('added', 0)}，总数 {paper_watchlist.get('total', 0)}",
            f"- 观察账本文件：`{paper_watchlist.get('path', '无')}`",
            f"- 状态：`{paper.get('status', '未运行')}`",
            f"- 修改组合状态：{_yes(bool(paper.get('state_mutated')))}",
            f"- 连接真实券商：{_yes(bool(paper.get('real_broker_connected')))}",
            f"- 原因：{paper.get('reason', '无')}",
            "",
            "## 公告与资讯Agent",
            "",
            f"- 状态：`{announcements.get('status', '未运行')}`",
            f"- 公告数量：{announcements.get('announcement_count', 0)}",
            f"- 结构化事件：{announcements.get('event_count', 0)}",
            f"- 高风险事件：{announcements.get('high_risk_count', 0)}",
            f"- 生成可执行信号：{announcements.get('actionable_signals_created', 0)}",
            f"- 全文处理：`{fulltext.get('status', '未运行')}`",
            f"- 全文完成/失败：{fulltext.get('completed', 0)}/{fulltext.get('failed', 0)}",
            f"- 需要OCR：{fulltext.get('needs_ocr', 0)}",
            "",
            "## 异常与提示",
            "",
        ]
    )
    if not warnings and not errors and not audit.get("error"):
        lines.append("无。")
    else:
        if audit.get("error"):
            lines.append(f"- 流程错误：{audit.get('error')}")
        lines.extend(f"- 错误：{item}" for item in errors)
        lines.extend(f"- 警告：{item}" for item in warnings)
    lines.extend(
        [
            "",
            "## 审计与安全边界",
            "",
            f"- 审计文件：`{audit_path}`",
            f"- 总状态：`{audit['status']}`",
            f"- 运行模式：`{audit['mode']}`",
            f"- 策略已批准：{_yes(bool(audit.get('strategy_approved')))}",
            "",
            "> 当前系统仅用于研究。除非策略、模拟盘审批和运行模式三重闸门全部通过，"
            "任何候选或订单计划都不构成可执行交易指令。",
            "",
        ]
    )
    return "\n".join(lines)


def write_daily_report(
    project: Path,
    audit: dict[str, Any],
    audit_path: Path,
) -> Path:
    directory = project / "reports" / "daily" / audit["trade_date"]
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"{audit_path.stem}.md"
    path.write_text(render_daily_audit(audit, audit_path, project=project), encoding="utf-8")
    return path

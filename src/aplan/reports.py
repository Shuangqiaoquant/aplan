from __future__ import annotations

from datetime import date

from .models import Candidate
from .pretrade import evaluate_pretrade


def render_daily_report(as_of: date, candidates: list[Candidate]) -> str:
    lines = [
        f"# APlan 每日候选股（{as_of.isoformat()} 收盘后）",
        "",
        "> 仅用于研究，不构成投资建议。所有信号最早在下一交易日执行。",
        "",
    ]
    if not candidates:
        return "\n".join(lines + ["今日没有满足条件的候选股。", ""])
    for rank, item in enumerate(sorted(candidates, key=lambda x: x.score, reverse=True), 1):
        pretrade = evaluate_pretrade(item)
        breakdown = "；".join(
            f"{name} {value:.1f}" for name, value in item.score_breakdown
        ) or "暂无分项"
        pretrade_detail = (
            f"{pretrade.decision}"
            + (f"；阻断：{'；'.join(pretrade.blockers)}" if pretrade.blockers else "")
            + (f"；提示：{'；'.join(pretrade.warnings[:3])}" if pretrade.warnings else "")
        )
        lines.extend(
            [
                f"## {rank}. {item.symbol}｜{item.horizon}｜{item.score:.1f} 分",
                "",
                f"- 决策分层：{item.decision_band}",
                f"- 置信度：{item.confidence:.2f}",
                f"- 买入风格：{item.entry_style}",
                f"- 模拟买入前检查：{pretrade_detail}",
                f"- 分项评分：{breakdown}",
                f"- 支持证据：{'；'.join(item.reasons) or '无'}",
                f"- 反对证据：{'；'.join(item.risks) or '无'}",
                f"- 缺失证据：{'；'.join(item.evidence_gaps) or '无'}",
                f"- 失效条件：{'；'.join(item.invalidation) or '待策略层配置；不得由 Agent 事后修改'}",
                "",
            ]
        )
    return "\n".join(lines)

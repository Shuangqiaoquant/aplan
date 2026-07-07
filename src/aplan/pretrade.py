from __future__ import annotations

from dataclasses import dataclass

from .models import Candidate, DailyBar


@dataclass(frozen=True, slots=True)
class PreTradeCheck:
    symbol: str
    decision: str
    blockers: tuple[str, ...]
    warnings: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class NextDayConfirmation:
    symbol: str
    decision: str
    blockers: tuple[str, ...]
    warnings: tuple[str, ...]
    gap_open: float
    intraday_return: float


def evaluate_pretrade(candidate: Candidate) -> PreTradeCheck:
    blockers: list[str] = []
    warnings: list[str] = []
    if candidate.decision_band in {"reject_ignore", "watch_only"}:
        blockers.append(f"决策分层为 {candidate.decision_band}，不得进入模拟买入")
    if candidate.decision_band == "research_candidate":
        blockers.append("决策分层仍为 research_candidate，只能观察，不能进入模拟买入确认")
    if candidate.entry_style in {"watch_only", "unclassified_watch", "mean_reversion_watch_unvalidated"}:
        blockers.append(f"买入风格 {candidate.entry_style} 尚未形成可执行规则")
    for risk in candidate.risks:
        if any(keyword in risk for keyword in ("市场环境风险", "高风险公告", "基本面风险", "估值偏高")):
            warnings.append(risk)
    for gap in candidate.evidence_gaps:
        if "尚未接入" in gap or "未找到" in gap or "样本不足" in gap:
            warnings.append(gap)
    if len(warnings) >= 3:
        blockers.append("证据缺口/风险提示过多，进入观察而非模拟买入")
    decision = "paper_trade_allowed_for_review" if not blockers else "watch_only"
    return PreTradeCheck(
        symbol=candidate.symbol,
        decision=decision,
        blockers=tuple(blockers),
        warnings=tuple(warnings),
    )


def evaluate_next_day_confirmation(
    candidate: Candidate,
    signal_bar: DailyBar,
    next_bar: DailyBar | None,
    *,
    max_gap_open: float = 0.05,
    min_breakout_turnover_ratio: float = 0.80,
) -> NextDayConfirmation:
    """确认信号次日是否仍允许模拟买入。

    信号日负责筛选候选，次日确认负责防止追高、跌破关键位或流动性突然萎缩。
    """
    if signal_bar.symbol != candidate.symbol:
        raise ValueError("signal_bar 与 candidate.symbol 不一致")
    if next_bar is not None and next_bar.symbol != candidate.symbol:
        raise ValueError("next_bar 与 candidate.symbol 不一致")

    base_check = evaluate_pretrade(candidate)
    blockers = list(base_check.blockers)
    warnings = list(base_check.warnings)
    if next_bar is None:
        blockers.append("缺少次日行情，暂不确认买入")
        return NextDayConfirmation(
            symbol=candidate.symbol,
            decision="pending_confirmation",
            blockers=tuple(blockers),
            warnings=tuple(warnings),
            gap_open=0.0,
            intraday_return=0.0,
        )

    gap_open = next_bar.open / signal_bar.close - 1
    intraday_return = next_bar.close / next_bar.open - 1
    if next_bar.is_suspended:
        blockers.append("次日停牌，无法执行模拟买入")
    if next_bar.is_limit_up or gap_open > max_gap_open:
        blockers.append(f"次日开盘涨幅 {gap_open:.1%}，超过追价上限 {max_gap_open:.1%}")
    if next_bar.is_limit_down:
        blockers.append("次日跌停，流动性和情绪风险过高")
    if next_bar.close < signal_bar.low:
        blockers.append("次日收盘跌破信号日低点，信号失效")
    elif next_bar.low < signal_bar.low:
        warnings.append("次日盘中跌破信号日低点，需降低优先级")

    if candidate.entry_style == "breakout_continuation_watch":
        if next_bar.close < signal_bar.close:
            blockers.append("突破延续型候选次日未站上信号日收盘价")
        if next_bar.turnover < signal_bar.turnover * min_breakout_turnover_ratio:
            warnings.append(
                f"次日成交额不足信号日 {min_breakout_turnover_ratio:.0%}，突破延续证据偏弱"
            )

    decision = "paper_trade_confirmed" if not blockers else "watch_only"
    return NextDayConfirmation(
        symbol=candidate.symbol,
        decision=decision,
        blockers=tuple(blockers),
        warnings=tuple(warnings),
        gap_open=gap_open,
        intraday_return=intraday_return,
    )

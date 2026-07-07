from __future__ import annotations

from datetime import UTC, date, datetime
from statistics import median

from .announcement_fulltext import FulltextAnalysis
from .announcements import AnnouncementEvent, EventImpact, RiskLevel
from .factors import annualized_volatility, momentum, score_candidate_components, turnover_trend
from .market_regime import assess_market_regime
from .models import Candidate, DailyBar, FundamentalSnapshot, Security, ValuationSnapshot
from .universe import eligible_securities


def select_candidates(
    securities: list[Security],
    bars: list[DailyBar],
    as_of: date,
    *,
    valuations: list[ValuationSnapshot] | None = None,
    fundamentals: list[FundamentalSnapshot] | None = None,
    announcement_events: list[AnnouncementEvent] | None = None,
    fulltext_analyses: list[FulltextAnalysis] | None = None,
    horizon: str = "swing",
    momentum_days: int = 20,
    top_n: int = 10,
    min_avg_turnover: float = 50_000_000,
    market_regime_min_sample: int = 50,
    industry_min_count: int = 3,
) -> list[Candidate]:
    eligible = eligible_securities(
        securities,
        bars,
        as_of,
        min_avg_turnover=min_avg_turnover,
    )
    allowed = {security.symbol for security in eligible}
    industry_by_symbol = {security.symbol: security.industry for security in eligible}
    histories: dict[str, list[DailyBar]] = {}
    for bar in bars:
        if bar.symbol in allowed and bar.trade_date <= as_of:
            histories.setdefault(bar.symbol, []).append(bar)
    components = score_candidate_components(histories, horizon=horizon, momentum_days=momentum_days)
    valuation_by_symbol = _latest_valuations(valuations or [], as_of, allowed)
    fundamental_by_symbol = _latest_fundamentals(fundamentals or [], as_of, allowed)
    events_by_symbol = _visible_announcement_events(announcement_events or [], as_of, allowed)
    market_regime = assess_market_regime(histories, min_sample=market_regime_min_sample)
    industry_ranks = _industry_relative_strength(histories, industry_by_symbol, min_count=industry_min_count)
    analysis_by_announcement = {
        analysis.announcement_id: analysis for analysis in (fulltext_analyses or [])
    }
    for symbol, snapshot in valuation_by_symbol.items():
        if symbol in components:
            components[symbol]["valuation_risk"] = _valuation_score(snapshot)
    raw_scores = {symbol: sum(parts.values()) for symbol, parts in components.items()}
    scores = {}
    for symbol, raw_score in raw_scores.items():
        score = _apply_announcement_score_cap(raw_score, events_by_symbol.get(symbol, ()))
        score = _apply_fundamental_score_cap(score, fundamental_by_symbol.get(symbol))
        if market_regime.score_cap is not None:
            score = min(score, market_regime.score_cap)
        industry_rank = industry_ranks.get(industry_by_symbol.get(symbol, "未知"))
        if industry_rank is not None and industry_rank <= 0.30:
            score = min(score, 74.0)
        scores[symbol] = score

    output: list[Candidate] = []
    for symbol, score in sorted(scores.items(), key=lambda item: item[1], reverse=True)[:top_n]:
        history = histories[symbol]
        mom = momentum(history, momentum_days) or 0
        vol = annualized_volatility(history) or 0
        flow = turnover_trend(history) or 0
        valuation = valuation_by_symbol.get(symbol)
        fundamental = fundamental_by_symbol.get(symbol)
        symbol_events = events_by_symbol.get(symbol, ())
        symbol_analyses = tuple(
            analysis_by_announcement[event.announcement_id]
            for event in symbol_events
            if event.announcement_id in analysis_by_announcement
        )
        score_parts = tuple(sorted(components[symbol].items()))
        reasons = [
            f"{momentum_days}日动量 {mom:.1%}",
            f"成交额趋势 {flow:.1%}",
        ]
        if valuation:
            reasons.append(
                f"估值快照 PE {valuation.pe:.1f} / PB {valuation.pb:.2f} / 总市值 {valuation.total_mv:.0f}万元"
            )
        if fundamental:
            reasons.append(_fundamental_summary(fundamental))
        for event in symbol_events:
            if event.impact_hint == EventImpact.POSITIVE:
                reasons.append(f"公告待全文确认：{event.event_type}（{event.summary}）")
        for analysis in symbol_analyses:
            if analysis.positive_evidence:
                reasons.append(
                    f"公告全文正面证据待验证：{analysis.event_type} / {analysis.positive_evidence[0]}"
                )
        risks = []
        if vol > 0.45:
            risks.append(f"年化波动率 {vol:.1%}")
        if market_regime.label in {"weak", "stress"}:
            risks.append(f"市场环境风险：{market_regime.reason}")
        industry = industry_by_symbol.get(symbol, "未知")
        industry_rank = industry_ranks.get(industry)
        if industry_rank is not None:
            if industry_rank <= 0.30:
                risks.append(f"行业相对弱势：{industry} 行业强度分位 {industry_rank:.0%}")
            elif industry_rank >= 0.70:
                reasons.append(f"行业相对强势：{industry} 行业强度分位 {industry_rank:.0%}")
        if valuation and (valuation.pe <= 0 or valuation.pb <= 0):
            risks.append("估值字段为非正值，不能按低估处理")
        elif valuation and (valuation.pe > 60 or valuation.pb > 8):
            risks.append("估值偏高，需更强成长或催化证据")
        if fundamental:
            risks.extend(_fundamental_risk_flags(fundamental))
        for event in symbol_events:
            if event.risk_level in {RiskLevel.HIGH, RiskLevel.CRITICAL}:
                risks.append(
                    f"高风险公告：{event.event_type} / {event.risk_level.value} / {event.summary}"
                )
            elif event.impact_hint in {EventImpact.NEGATIVE, EventImpact.MIXED}:
                risks.append(
                    f"公告风险待审阅：{event.event_type} / {event.impact_hint.value} / {event.summary}"
                )
        for analysis in symbol_analyses:
            if analysis.conclusion == "risk_review_required":
                detail = analysis.negative_evidence[0] if analysis.negative_evidence else "全文规则要求风险复核"
                risks.append(f"公告全文风险确认：{analysis.event_type} / {detail}")
            for uncertainty in analysis.uncertainties[:2]:
                risks.append(f"公告全文不确定性：{uncertainty}")
        risks.append("策略仍处 research_only，未通过隔离验证")
        evidence_gaps = ["基本面质量尚未接入本评分", "公告/催化证据尚未接入本评分"]
        if fundamentals is not None:
            evidence_gaps = [
                gap for gap in evidence_gaps if gap != "基本面质量尚未接入本评分"
            ]
            if fundamental:
                evidence_gaps.append("基本面风险降级已接入；质量加分尚未启用")
            else:
                evidence_gaps.append("未找到信号日前已发布的基本面快照")
        if not valuation:
            evidence_gaps.append("估值快照尚未接入本评分")
        else:
            evidence_gaps.append("估值行业/历史分位尚未接入，仅使用绝对 PE/PB 保守评分")
        if announcement_events is not None:
            evidence_gaps = [
                gap for gap in evidence_gaps if gap != "公告/催化证据尚未接入本评分"
            ]
            evidence_gaps.append("公告标题风险规则已接入；全文催化加分尚未启用")
        if fulltext_analyses is not None:
            evidence_gaps = [
                gap for gap in evidence_gaps if gap != "公告标题风险规则已接入；全文催化加分尚未启用"
            ]
            evidence_gaps.append("公告全文分析已接入；正向催化加分尚未启用")
        if market_regime.label == "insufficient":
            evidence_gaps.append(market_regime.reason)
        else:
            evidence_gaps.append(f"市场环境过滤已接入：{market_regime.reason}")
        if industry_ranks:
            evidence_gaps.append("行业相对强弱已接入为风险闸门；暂不做正向加分")
        else:
            evidence_gaps.append("行业相对强弱样本不足，尚未接入本次评分")
        invalidation = (
            "相对强度跌出同周期候选组",
            "成交额趋势转弱且价格无法维持趋势",
            "出现未审阅的高风险公告或交易限制",
        )
        output.append(
            Candidate(
                symbol=symbol,
                score=score,
                horizon=horizon,
                reasons=tuple(reasons),
                risks=tuple(risks),
                score_breakdown=score_parts,
                confidence=0.55 if valuation and fundamental else 0.50 if valuation or fundamental else 0.45,
                decision_band=_decision_band(score),
                entry_style=_entry_style(mom, flow, vol, score),
                evidence_gaps=tuple(evidence_gaps),
                invalidation=invalidation,
            )
        )
    return output


def _decision_band(score: float) -> str:
    if score < 50:
        return "reject_ignore"
    if score < 65:
        return "watch_only"
    if score < 75:
        return "research_candidate"
    if score < 85:
        return "paper_candidate_if_validated"
    return "high_priority_research"


def _entry_style(momentum_value: float, flow_value: float, volatility: float, score: float) -> str:
    """只用当前可见量价证据做粗分类；事件确认等到公告证据接入后再启用。"""
    if score < 65:
        return "watch_only"
    if momentum_value > 0.08 and flow_value > 0:
        return "breakout_continuation_watch"
    if momentum_value > 0 and flow_value <= 0 and volatility <= 0.45:
        return "pullback_in_uptrend_watch"
    if momentum_value < 0 and flow_value > 0 and volatility <= 0.35:
        return "mean_reversion_watch_unvalidated"
    return "unclassified_watch"


def _latest_valuations(
    valuations: list[ValuationSnapshot],
    as_of: date,
    allowed: set[str],
) -> dict[str, ValuationSnapshot]:
    latest: dict[str, ValuationSnapshot] = {}
    for snapshot in sorted(valuations, key=lambda item: item.trade_date):
        if snapshot.symbol in allowed and snapshot.trade_date <= as_of:
            latest[snapshot.symbol] = snapshot
    return latest


def _latest_fundamentals(
    fundamentals: list[FundamentalSnapshot],
    as_of: date,
    allowed: set[str],
) -> dict[str, FundamentalSnapshot]:
    latest: dict[str, FundamentalSnapshot] = {}
    cutoff = datetime.combine(as_of, datetime.max.time(), tzinfo=UTC)
    for snapshot in sorted(fundamentals, key=lambda item: (item.publish_time, item.period_end)):
        publish_time = snapshot.publish_time
        if publish_time.tzinfo is None:
            publish_time = publish_time.replace(tzinfo=UTC)
        if snapshot.symbol in allowed and publish_time <= cutoff:
            latest[snapshot.symbol] = snapshot
    return latest


def _format_optional_percent(value: float | None) -> str:
    return "缺失" if value is None else f"{value:.1%}"


def _fundamental_summary(snapshot: FundamentalSnapshot) -> str:
    return (
        f"基本面快照 {snapshot.period_end.isoformat()}："
        f"营收增速 {_format_optional_percent(snapshot.revenue_growth)} / "
        f"净利增速 {_format_optional_percent(snapshot.net_profit_growth)} / "
        f"ROE {_format_optional_percent(snapshot.roe)}"
    )


def _valuation_score(snapshot: ValuationSnapshot) -> float:
    """绝对估值保守分；负 PE/PB 不视为便宜。后续再替换为行业/历史分位。"""
    if snapshot.pe <= 0 or snapshot.pb <= 0:
        return 2.0
    pe_score = 5.0 if snapshot.pe <= 15 else 3.5 if snapshot.pe <= 35 else 1.5 if snapshot.pe <= 60 else 0.5
    pb_score = 5.0 if snapshot.pb <= 1.5 else 3.5 if snapshot.pb <= 3 else 1.5 if snapshot.pb <= 8 else 0.5
    return min(10.0, pe_score + pb_score)


def _industry_relative_strength(
    histories: dict[str, list[DailyBar]],
    industry_by_symbol: dict[str, str],
    *,
    min_count: int,
) -> dict[str, float]:
    industry_values: dict[str, list[float]] = {}
    for symbol, history in histories.items():
        if len(history) <= 20:
            continue
        industry = industry_by_symbol.get(symbol, "未知")
        if not industry or industry == "未知":
            continue
        value = history[-1].close / history[-21].close - 1
        industry_values.setdefault(industry, []).append(value)
    medians = {
        industry: median(values)
        for industry, values in industry_values.items()
        if values
    }
    if len(medians) < min_count:
        return {}
    ordered = sorted(medians, key=medians.get)
    denominator = len(ordered) - 1
    return {industry: index / denominator for index, industry in enumerate(ordered)}


def _fundamental_risk_flags(snapshot: FundamentalSnapshot) -> list[str]:
    risks: list[str] = []
    if snapshot.net_profit_growth is not None and snapshot.net_profit_growth < -0.30:
        risks.append("基本面风险：净利润同比大幅下滑")
    if snapshot.operating_cashflow_to_profit is not None and snapshot.operating_cashflow_to_profit < 0:
        risks.append("基本面风险：经营现金流与利润背离")
    if snapshot.debt_to_assets is not None and snapshot.debt_to_assets > 0.75:
        risks.append("基本面风险：资产负债率偏高")
    return risks


def _apply_fundamental_score_cap(score: float, snapshot: FundamentalSnapshot | None) -> float:
    """基本面只做风险闸门，不做正向加分；多项风险时降到 watch_only。"""
    if snapshot is None:
        return score
    risk_count = len(_fundamental_risk_flags(snapshot))
    if risk_count >= 2:
        return min(score, 64.0)
    if risk_count == 1:
        return min(score, 74.0)
    return score


def _visible_announcement_events(
    events: list[AnnouncementEvent],
    as_of: date,
    allowed: set[str],
) -> dict[str, tuple[AnnouncementEvent, ...]]:
    grouped: dict[str, list[AnnouncementEvent]] = {}
    cutoff = datetime.combine(as_of, datetime.max.time(), tzinfo=UTC)
    for event in events:
        if event.symbol not in allowed:
            continue
        try:
            published_at = datetime.fromisoformat(event.published_at)
        except ValueError:
            continue
        if published_at.tzinfo is None:
            published_at = published_at.replace(tzinfo=UTC)
        if published_at <= cutoff:
            grouped.setdefault(event.symbol, []).append(event)
    return {symbol: tuple(items) for symbol, items in grouped.items()}


def _apply_announcement_score_cap(score: float, events: tuple[AnnouncementEvent, ...]) -> float:
    if any(event.risk_level == RiskLevel.CRITICAL for event in events):
        return min(score, 49.0)
    if any(event.risk_level == RiskLevel.HIGH for event in events):
        return min(score, 64.0)
    if any(event.impact_hint in {EventImpact.NEGATIVE, EventImpact.MIXED} for event in events):
        return min(score, 74.0)
    return score

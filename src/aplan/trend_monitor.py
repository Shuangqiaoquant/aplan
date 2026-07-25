from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from statistics import mean

from .models import Candidate, DailyBar, Security
from .universe import eligible_securities


MODEL_ID = "trend_monitor_v0_1"


@dataclass(frozen=True, slots=True)
class TrendSnapshot:
    symbol: str
    close: float
    ma5: float
    ma20: float
    ma60: float
    volume_ratio: float
    breakout_reference: float
    signals: tuple[str, ...]


def _average(values: list[float]) -> float:
    return mean(values)


def _moving_average(history: list[DailyBar], window: int, *, offset: int = 0) -> float:
    end = len(history) - offset
    start = end - window
    if start < 0:
        raise ValueError(f"至少需要 {window + offset} 根日线")
    return _average([bar.close for bar in history[start:end]])


def detect_trend_signals(
    history: list[DailyBar],
    *,
    breakout_lookback: int = 20,
    volume_ratio_min: float = 1.5,
    pullback_tolerance: float = 0.02,
) -> TrendSnapshot | None:
    """识别纯量价趋势信号；只使用最后一根日线及其之前的数据。"""
    ordered = sorted(history, key=lambda item: item.trade_date)
    required = max(61, breakout_lookback + 1)
    if len(ordered) < required:
        return None
    latest = ordered[-1]
    if latest.is_suspended or latest.is_limit_up or latest.is_limit_down:
        return None

    ma5 = _moving_average(ordered, 5)
    ma20 = _moving_average(ordered, 20)
    ma60 = _moving_average(ordered, 60)
    previous_ma5 = _moving_average(ordered, 5, offset=1)
    previous_ma20 = _moving_average(ordered, 20, offset=1)
    previous_volume = _average([bar.volume for bar in ordered[-6:-1]])
    volume_ratio = latest.volume / previous_volume if previous_volume > 0 else 0.0
    breakout_reference = max(bar.high for bar in ordered[-breakout_lookback - 1 : -1])

    trend_not_bearish = latest.close >= ma20 and ma20 >= previous_ma20
    uptrend = latest.close > ma20 > ma60 and ma20 > previous_ma20
    near_ma20 = abs(latest.low / ma20 - 1) <= pullback_tolerance

    signals: list[str] = []
    if latest.close > breakout_reference and trend_not_bearish and volume_ratio >= volume_ratio_min:
        signals.append("B1_volume_breakout")
    if uptrend and near_ma20 and latest.close > ma20 and latest.close >= latest.open:
        signals.append("B2_ma20_pullback")
    if previous_ma5 <= previous_ma20 and ma5 > ma20 and latest.close > ma60:
        signals.append("B3_ma5_cross_ma20")
    if not signals:
        return None
    return TrendSnapshot(
        symbol=latest.symbol,
        close=latest.close,
        ma5=ma5,
        ma20=ma20,
        ma60=ma60,
        volume_ratio=volume_ratio,
        breakout_reference=breakout_reference,
        signals=tuple(signals),
    )


def select_trend_candidates(
    securities: list[Security],
    bars: list[DailyBar],
    as_of: date,
    *,
    horizon: str = "swing",
    top_n: int = 5,
    min_avg_turnover: float = 50_000_000,
    breakout_lookback: int = 20,
    volume_ratio_min: float = 1.5,
    pullback_tolerance: float = 0.02,
) -> list[Candidate]:
    """生成趋势基准模型的研究候选，不产生模拟或实盘指令。"""
    eligible = eligible_securities(
        securities,
        bars,
        as_of,
        min_avg_turnover=min_avg_turnover,
    )
    allowed = {security.symbol for security in eligible}
    histories: dict[str, list[DailyBar]] = {}
    for bar in bars:
        if bar.symbol in allowed and bar.trade_date <= as_of:
            histories.setdefault(bar.symbol, []).append(bar)

    ranked: list[tuple[float, TrendSnapshot]] = []
    for symbol, history in histories.items():
        ordered = sorted(history, key=lambda item: item.trade_date)
        if not ordered or ordered[-1].trade_date != as_of:
            continue
        snapshot = detect_trend_signals(
            ordered,
            breakout_lookback=breakout_lookback,
            volume_ratio_min=volume_ratio_min,
            pullback_tolerance=pullback_tolerance,
        )
        if snapshot is None:
            continue
        aligned = snapshot.close > snapshot.ma20 > snapshot.ma60
        score = 45.0
        score += 22.0 if "B1_volume_breakout" in snapshot.signals else 0.0
        score += 18.0 if "B2_ma20_pullback" in snapshot.signals else 0.0
        score += 15.0 if "B3_ma5_cross_ma20" in snapshot.signals else 0.0
        score += 8.0 if aligned else 0.0
        score += min(7.0, max(0.0, (snapshot.volume_ratio - 1.0) * 5.0))
        ranked.append((min(score, 95.0), snapshot))

    output: list[Candidate] = []
    for score, snapshot in sorted(ranked, key=lambda item: (-item[0], item[1].symbol))[:top_n]:
        signal_labels = {
            "B1_volume_breakout": "放量突破前期高点",
            "B2_ma20_pullback": "上升趋势中回踩 MA20 后收回",
            "B3_ma5_cross_ma20": "MA5 上穿 MA20",
        }
        reasons = [signal_labels[signal] for signal in snapshot.signals]
        reasons.extend(
            [
                f"收盘 {snapshot.close:.2f} / MA20 {snapshot.ma20:.2f} / MA60 {snapshot.ma60:.2f}",
                f"当日量比（相对前5日）{snapshot.volume_ratio:.2f}",
            ]
        )
        invalidation = [
            f"收盘跌破 MA20（信号日参考值 {snapshot.ma20:.2f}）",
            f"收盘跌破 MA60（信号日参考值 {snapshot.ma60:.2f}）",
        ]
        if "B1_volume_breakout" in snapshot.signals:
            invalidation.append(f"突破失败并跌回参考高点 {snapshot.breakout_reference:.2f} 下方")
        entry_style = (
            "trend_breakout_watch"
            if "B1_volume_breakout" in snapshot.signals
            else "trend_pullback_watch"
            if "B2_ma20_pullback" in snapshot.signals
            else "trend_cross_watch"
        )
        score_breakdown = (
            ("base", 45.0),
            ("B1_volume_breakout", 22.0 if "B1_volume_breakout" in snapshot.signals else 0.0),
            ("B2_ma20_pullback", 18.0 if "B2_ma20_pullback" in snapshot.signals else 0.0),
            ("B3_ma5_cross_ma20", 15.0 if "B3_ma5_cross_ma20" in snapshot.signals else 0.0),
            ("trend_alignment", 8.0 if snapshot.close > snapshot.ma20 > snapshot.ma60 else 0.0),
            ("volume_confirmation", min(7.0, max(0.0, (snapshot.volume_ratio - 1.0) * 5.0))),
        )
        output.append(
            Candidate(
                symbol=snapshot.symbol,
                score=score,
                horizon=horizon,
                reasons=tuple(reasons),
                risks=(
                    "趋势规则可能出现假突破、快速反转或高位接力风险",
                    "纯量价基准不判断基本面、估值、公告与资讯风险",
                    "模型仍处 research_only，未通过隔离验证",
                ),
                score_breakdown=score_breakdown,
                confidence=min(0.65, 0.42 + 0.08 * len(snapshot.signals)),
                decision_band="research_candidate",
                entry_style=entry_style,
                evidence_gaps=(
                    "未接入市场环境与行业相对强弱",
                    "未接入基本面、估值、公告及新闻证据",
                    "固定参数尚未经过样本外稳定性验证",
                ),
                invalidation=tuple(invalidation),
            )
        )
    return output

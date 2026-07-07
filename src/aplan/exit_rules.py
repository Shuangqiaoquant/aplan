from __future__ import annotations

import argparse
import json
from dataclasses import asdict, dataclass
from datetime import date
from pathlib import Path

from .models import DailyBar
from .research_workflow import _load_bars_source


@dataclass(frozen=True, slots=True)
class ExitReview:
    symbol: str
    as_of: str
    close: float
    action: str
    risk_level: str
    reasons: tuple[str, ...]
    invalidation: tuple[str, ...]

    def to_dict(self) -> dict[str, object]:
        return asdict(self)


def _ma(history: list[DailyBar], window: int) -> float | None:
    if len(history) < window:
        return None
    return sum(bar.close for bar in history[-window:]) / window


def _momentum(history: list[DailyBar], window: int) -> float | None:
    if len(history) <= window:
        return None
    return history[-1].close / history[-window - 1].close - 1


def review_exit(
    symbol: str,
    bars: list[DailyBar],
    as_of: date,
    *,
    cost_basis: float | None = None,
    entry_date: date | None = None,
    initial_stop_loss: float = 0.08,
    trailing_stop: float = 0.12,
    trailing_activation_return: float = 0.10,
    time_stop_days: int = 20,
    min_return_for_time_stop: float = 0.02,
) -> ExitReview:
    history = [bar for bar in sorted(bars, key=lambda item: item.trade_date) if bar.symbol == symbol and bar.trade_date <= as_of]
    if not history:
        raise ValueError(f"缺少 {symbol} 在 {as_of.isoformat()} 前的行情")
    latest = history[-1]
    ma20 = _ma(history, 20)
    ma60 = _ma(history, 60)
    mom20 = _momentum(history, 20)
    reasons: list[str] = []
    risk_points = 0
    if ma20 is not None and latest.close < ma20:
        risk_points += 1
        reasons.append(f"收盘价 {latest.close:.2f} 低于20日均线 {ma20:.2f}")
    if ma60 is not None and latest.close < ma60:
        risk_points += 1
        reasons.append(f"收盘价 {latest.close:.2f} 低于60日均线 {ma60:.2f}")
    if mom20 is not None and mom20 < -0.10:
        risk_points += 1
        reasons.append(f"20日动量 {mom20:.1%}，趋势明显转弱")
    if cost_basis is not None and cost_basis > 0:
        unrealized = latest.close / cost_basis - 1
        if unrealized <= -initial_stop_loss:
            risk_points += 2
            reasons.append(f"相对成本 {cost_basis:.2f} 浮亏 {unrealized:.1%}，触发初始止损阈值 {-initial_stop_loss:.1%}")
        elif unrealized < -0.10:
            risk_points += 1
            reasons.append(f"相对成本 {cost_basis:.2f} 浮亏 {unrealized:.1%}")
        if unrealized < -0.20:
            risk_points += 1
            reasons.append("浮亏超过20%，风险级别上调")
    if entry_date is not None:
        position_history = [bar for bar in history if bar.trade_date >= entry_date]
        if position_history:
            highest_close = max(bar.close for bar in position_history)
            entry_close = position_history[0].close
            drawdown_from_high = latest.close / highest_close - 1
            return_from_entry = latest.close / entry_close - 1
            highest_return = highest_close / entry_close - 1
            holding_days = len(position_history) - 1
            if highest_return >= trailing_activation_return and drawdown_from_high <= -trailing_stop:
                risk_points += 2
                reasons.append(
                    f"持仓后最高收盘回撤 {drawdown_from_high:.1%}，触发移动止盈/回撤阈值 {-trailing_stop:.1%}"
                )
            if holding_days >= time_stop_days and return_from_entry < min_return_for_time_stop:
                risk_points += 1
                reasons.append(
                    f"持仓 {holding_days} 个交易日收益 {return_from_entry:.1%}，未达到时间止损最低要求 {min_return_for_time_stop:.1%}"
                )
    if risk_points >= 4:
        action = "reduce_or_exit_review"
        risk_level = "high"
    elif risk_points >= 2:
        action = "reduce_review"
        risk_level = "medium"
    elif risk_points >= 1:
        action = "watch_tightly"
        risk_level = "low"
    else:
        action = "hold_review"
        risk_level = "normal"
        reasons.append("未触发基础退出风险")
    invalidation = (
        "收盘价持续低于20/60日均线且无法收复",
        "20日动量继续恶化",
        "基本面或公告风险新增",
        "单票仓位超过风险预算",
    )
    return ExitReview(
        symbol=symbol,
        as_of=latest.trade_date.isoformat(),
        close=latest.close,
        action=action,
        risk_level=risk_level,
        reasons=tuple(reasons),
        invalidation=invalidation,
    )


def main() -> None:
    parser = argparse.ArgumentParser(description="APlan 持仓退出/减仓审查")
    parser.add_argument("--root", default=".")
    parser.add_argument("--bars", required=True)
    parser.add_argument("--date", required=True)
    parser.add_argument("--symbol", required=True)
    parser.add_argument("--cost", type=float)
    parser.add_argument("--entry-date")
    parser.add_argument("--initial-stop-loss", type=float, default=0.08)
    parser.add_argument("--trailing-stop", type=float, default=0.12)
    parser.add_argument("--trailing-activation-return", type=float, default=0.10)
    parser.add_argument("--time-stop-days", type=int, default=20)
    parser.add_argument("--min-return-for-time-stop", type=float, default=0.02)
    parser.add_argument("--output")
    args = parser.parse_args()
    project = Path(args.root).resolve()
    as_of = date.fromisoformat(args.date)
    review = review_exit(
        args.symbol,
        _load_bars_source(project / args.bars if not Path(args.bars).is_absolute() else args.bars, as_of),
        as_of,
        cost_basis=args.cost,
        entry_date=date.fromisoformat(args.entry_date) if args.entry_date else None,
        initial_stop_loss=args.initial_stop_loss,
        trailing_stop=args.trailing_stop,
        trailing_activation_return=args.trailing_activation_return,
        time_stop_days=args.time_stop_days,
        min_return_for_time_stop=args.min_return_for_time_stop,
    )
    payload = json.dumps(review.to_dict(), ensure_ascii=False, indent=2)
    if args.output:
        output = Path(args.output)
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(payload + "\n", encoding="utf-8")
    else:
        print(payload)


if __name__ == "__main__":
    main()

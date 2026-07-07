from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from datetime import date
from pathlib import Path
from typing import Any

from .exit_rules import review_exit
from .observations import ObservationStore
from .portfolio import PortfolioStore
from .pretrade import evaluate_pretrade
from .research_workflow import _load_bars_source, run_research_report


@dataclass(frozen=True, slots=True)
class CandidateCard:
    symbol: str
    score: float
    decision_band: str
    entry_style: str
    action: str
    reasons: tuple[str, ...]
    risks: tuple[str, ...]

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True, slots=True)
class HoldingRiskCard:
    symbol: str
    source: str
    cost_basis: float | None
    current_price: float | None
    unrealized_return: float | None
    action: str
    risk_level: str
    reasons: tuple[str, ...]

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def latest_processed_trade_date(project: Path, trade_date: str) -> date | None:
    target = trade_date.replace("-", "")
    candidates = [
        path
        for path in (project / "data" / "processed" / "daily").glob("20??????.csv")
        if path.stem <= target and path.stat().st_size > 0
    ]
    if not candidates:
        return None
    key = max(candidates, key=lambda path: path.stem).stem
    return date(int(key[:4]), int(key[4:6]), int(key[6:8]))


def _optional_file(project: Path, relative: str) -> Path | None:
    path = project / relative
    return path if path.exists() else None


def build_candidate_cards(
    project: Path,
    trade_date: str,
    *,
    top_n: int = 5,
    horizon: str = "swing",
) -> dict[str, Any]:
    as_of = latest_processed_trade_date(project, trade_date)
    securities = project / "data" / "processed" / "securities.csv"
    bars = project / "data" / "processed" / "daily"
    if as_of is None or not securities.exists() or not bars.exists():
        return {
            "status": "unavailable",
            "reason": "缺少可用行情或证券基础数据",
            "as_of": None,
            "items": [],
        }
    key = as_of.strftime("%Y%m%d")
    try:
        result = run_research_report(
            project,
            bars_path=bars,
            securities_path=securities,
            as_of=as_of,
            horizon=horizon,
            top_n=top_n,
            valuations_path=_optional_file(project, f"data/processed/valuations/{key}.csv"),
            fundamentals_path=_optional_file(project, f"data/processed/akshare_fundamentals/{key}.csv"),
            announcements_path=_optional_file(project, f"data/processed/announcements/{key}.json"),
            announcement_analysis_path=_optional_file(project, f"data/processed/announcement_analysis/{key}.json"),
        )
    except (FileNotFoundError, ValueError, RuntimeError, json.JSONDecodeError) as exc:
        return {
            "status": "failed",
            "reason": str(exc),
            "as_of": as_of.isoformat(),
            "items": [],
        }
    cards: list[CandidateCard] = []
    for candidate in result.candidates[:top_n]:
        pretrade = evaluate_pretrade(candidate)
        if pretrade.decision == "paper_trade_allowed_for_review":
            action = "观察：等待次日确认"
        elif pretrade.blockers:
            action = "暂不买入：规则阻断"
        else:
            action = "观察：证据不足"
        cards.append(
            CandidateCard(
                symbol=candidate.symbol,
                score=round(candidate.score, 2),
                decision_band=candidate.decision_band,
                entry_style=candidate.entry_style,
                action=action,
                reasons=tuple(candidate.reasons[:2]),
                risks=tuple((candidate.risks + candidate.evidence_gaps)[:2]),
            )
        )
    return {
        "status": "available",
        "as_of": as_of.isoformat(),
        "horizon": horizon,
        "items": [card.to_dict() for card in cards],
    }


def _manual_holding_cards(project: Path, bars: list[Any], as_of: date) -> list[HoldingRiskCard]:
    cards: list[HoldingRiskCard] = []
    for observation in ObservationStore(project).load():
        if observation.entry_style != "manual_position":
            continue
        cost_basis = observation.outcomes.get("cost_basis")
        current_price = observation.outcomes.get("current_price")
        try:
            review = review_exit(
                observation.symbol,
                bars,
                as_of,
                cost_basis=float(cost_basis) if cost_basis else None,
            )
            cards.append(
                HoldingRiskCard(
                    symbol=observation.symbol,
                    source="manual_position",
                    cost_basis=float(cost_basis) if cost_basis else None,
                    current_price=review.close,
                    unrealized_return=(review.close / float(cost_basis) - 1) if cost_basis else None,
                    action=review.action,
                    risk_level=review.risk_level,
                    reasons=tuple(review.reasons[:3]),
                )
            )
        except (ValueError, ZeroDivisionError):
            cards.append(
                HoldingRiskCard(
                    symbol=observation.symbol,
                    source="manual_position",
                    cost_basis=float(cost_basis) if cost_basis else None,
                    current_price=float(current_price) if current_price else None,
                    unrealized_return=observation.outcomes.get("unrealized_return"),
                    action="risk_review",
                    risk_level="unknown",
                    reasons=("缺少可用行情，沿用人工风险案例",),
                )
            )
    return cards


def _portfolio_holding_cards(project: Path, bars: list[Any], as_of: date) -> list[HoldingRiskCard]:
    portfolio = PortfolioStore(project).load("paper-main")
    if portfolio is None:
        return []
    cards: list[HoldingRiskCard] = []
    for position in portfolio.positions.values():
        try:
            review = review_exit(
                position.symbol,
                bars,
                as_of,
                cost_basis=position.average_cost,
            )
            current_price = review.close
            unrealized = current_price / position.average_cost - 1 if position.average_cost > 0 else None
            action = review.action
            risk_level = review.risk_level
            reasons = tuple(review.reasons[:3])
        except (ValueError, ZeroDivisionError):
            current_price = position.market_price
            unrealized = current_price / position.average_cost - 1 if position.average_cost > 0 else None
            action = "review_needed"
            risk_level = "unknown"
            reasons = ("缺少行情，无法完成退出规则审查",)
        cards.append(
            HoldingRiskCard(
                symbol=position.symbol,
                source="paper_portfolio",
                cost_basis=position.average_cost,
                current_price=current_price,
                unrealized_return=unrealized,
                action=action,
                risk_level=risk_level,
                reasons=reasons,
            )
        )
    return cards


def build_holding_risk_cards(project: Path, trade_date: str) -> dict[str, Any]:
    as_of = latest_processed_trade_date(project, trade_date)
    if as_of is None:
        return {
            "status": "unavailable",
            "reason": "缺少可用行情",
            "as_of": None,
            "items": [],
        }
    bars = _load_bars_source(project / "data" / "processed" / "daily", as_of)
    cards = _portfolio_holding_cards(project, bars, as_of)
    seen = {card.symbol for card in cards}
    cards.extend(card for card in _manual_holding_cards(project, bars, as_of) if card.symbol not in seen)
    risk_order = {"high": 0, "medium": 1, "low": 2, "normal": 3, "unknown": 4}
    cards.sort(key=lambda card: (risk_order.get(card.risk_level, 9), card.symbol))
    return {
        "status": "available",
        "as_of": as_of.isoformat(),
        "items": [card.to_dict() for card in cards],
    }


def build_daily_insights(project: Path, trade_date: str) -> dict[str, Any]:
    return {
        "candidate_top": build_candidate_cards(project, trade_date),
        "holding_risk": build_holding_risk_cards(project, trade_date),
    }

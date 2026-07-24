from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any

from .models import (
    Candidate,
    DailyBar,
    FundamentalSnapshot,
    Security,
    ValuationSnapshot,
)
from .pipeline import select_candidates


STRATEGY_PROFILES = ("momentum", "event", "fundamental", "hybrid")


@dataclass(frozen=True, slots=True)
class UserConstraints:
    industries: tuple[str, ...] = ()
    prefixes: tuple[str, ...] = ()
    include_symbols: tuple[str, ...] = ()
    exclude_symbols: tuple[str, ...] = ()
    allow_star: bool = True
    allow_chinext: bool = True
    risk_preference: str = "balanced"

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True, slots=True)
class DataRequirement:
    dataset: str
    stage: str
    priority: str
    scope: str
    reason: str
    adapter: str
    availability: str

    def to_dict(self) -> dict[str, str]:
        return asdict(self)


@dataclass(frozen=True, slots=True)
class FunnelStage:
    stage_id: str
    label: str
    status: str
    input_count: int
    output_symbols: tuple[str, ...]
    eliminated_count: int
    elimination_reasons: tuple[tuple[str, int], ...] = ()
    human_confirmation_required: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            **asdict(self),
            "output_count": len(self.output_symbols),
            "elimination_reasons": dict(self.elimination_reasons),
        }


@dataclass(frozen=True, slots=True)
class ResearchFunnelRun:
    run_id: str
    model_id: str
    as_of: str
    strategy_profile: str
    status: str
    constraints: UserConstraints
    stages: tuple[FunnelStage, ...]
    data_requirements: tuple[DataRequirement, ...]
    final_candidates: tuple[Candidate, ...]
    generated_at: str
    mode: str = "research_only"

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": 1,
            "run_id": self.run_id,
            "model_id": self.model_id,
            "as_of": self.as_of,
            "strategy_profile": self.strategy_profile,
            "status": self.status,
            "mode": self.mode,
            "constraints": self.constraints.to_dict(),
            "stages": [stage.to_dict() for stage in self.stages],
            "data_requirements": [
                requirement.to_dict() for requirement in self.data_requirements
            ],
            "final_candidates": [asdict(candidate) for candidate in self.final_candidates],
            "generated_at": self.generated_at,
            "execution_allowed": False,
            "human_confirmation_required": self.status == "awaiting_user_confirmation",
        }


_PROFILE_REQUIREMENTS: dict[str, tuple[tuple[str, str, str, str, str], ...]] = {
    "momentum": (
        ("margin_trading", "refinement", "high", "candidate_pool", "杠杆资金方向和拥挤度", "yinhe_planned"),
        ("dragon_tiger", "refinement", "medium", "candidate_pool", "异常交易和活跃席位", "yinhe_planned"),
        ("realtime_l1", "fine_research", "high", "watch_pool", "成交加速、价差和盘口失衡", "yinhe_on_demand"),
    ),
    "event": (
        ("announcements", "refinement", "high", "candidate_pool", "识别事件催化和公告风险", "existing"),
        ("dragon_tiger", "refinement", "high", "candidate_pool", "事件后的异常交易行为", "yinhe_planned"),
        ("block_trades", "refinement", "medium", "candidate_pool", "机构交易和折溢价线索", "yinhe_planned"),
        ("announcement_fulltext", "fine_research", "high", "watch_pool", "核验事件细节和反面证据", "existing"),
        ("realtime_l1", "fine_research", "medium", "watch_pool", "事件发生后的盘中确认", "yinhe_on_demand"),
    ),
    "fundamental": (
        ("valuations", "refinement", "high", "candidate_pool", "估值水平和风险闸门", "existing"),
        ("fundamentals", "refinement", "high", "candidate_pool", "盈利质量、现金流和负债风险", "existing"),
        ("earnings_forecast", "refinement", "high", "candidate_pool", "业绩预告和快报的及时变化", "yinhe_planned"),
        ("shareholder_count", "fine_research", "medium", "watch_pool", "筹码集中度变化", "yinhe_planned"),
        ("pledge_unlock", "fine_research", "medium", "watch_pool", "质押与限售解禁风险", "yinhe_planned"),
    ),
    "hybrid": (
        ("valuations", "refinement", "high", "candidate_pool", "估值水平和风险闸门", "existing"),
        ("fundamentals", "refinement", "high", "candidate_pool", "盈利质量和财务风险", "existing"),
        ("announcements", "refinement", "high", "candidate_pool", "事件催化和公告风险", "existing"),
        ("margin_trading", "refinement", "medium", "candidate_pool", "杠杆资金方向和拥挤度", "yinhe_planned"),
        ("shareholder_count", "fine_research", "medium", "watch_pool", "筹码集中度变化", "yinhe_planned"),
        ("dragon_tiger", "fine_research", "medium", "watch_pool", "异常交易行为", "yinhe_planned"),
        ("realtime_l1", "fine_research", "medium", "watch_pool", "只对最终观察池做盘中确认", "yinhe_on_demand"),
    ),
}


def apply_user_constraints(
    securities: list[Security],
    constraints: UserConstraints,
) -> tuple[list[Security], dict[str, int]]:
    selected: list[Security] = []
    eliminated: dict[str, int] = {}
    include = set(constraints.include_symbols)
    exclude = set(constraints.exclude_symbols)
    industries = set(constraints.industries)

    for security in securities:
        reason = ""
        if include and security.symbol not in include:
            reason = "not_in_user_include_list"
        elif security.symbol in exclude:
            reason = "user_excluded_symbol"
        elif industries and security.industry not in industries:
            reason = "industry_constraint"
        elif constraints.prefixes and not security.symbol.startswith(constraints.prefixes):
            reason = "prefix_constraint"
        elif not constraints.allow_star and security.symbol.startswith(("688", "689")):
            reason = "star_market_disabled"
        elif not constraints.allow_chinext and security.symbol.startswith(("300", "301")):
            reason = "chinext_disabled"

        if reason:
            eliminated[reason] = eliminated.get(reason, 0) + 1
        else:
            selected.append(security)
    return selected, eliminated


def _requirements(
    profile: str,
    available_datasets: set[str],
) -> tuple[DataRequirement, ...]:
    if profile not in STRATEGY_PROFILES:
        raise ValueError(f"未知研究漏斗策略类型：{profile}")
    common = (
        DataRequirement(
            dataset="daily",
            stage="broad_screen",
            priority="required",
            scope="full_universe",
            reason="基础量价、流动性和趋势粗筛",
            adapter="existing",
            availability="available",
        ),
        DataRequirement(
            dataset="security_master",
            stage="user_scope",
            priority="required",
            scope="full_universe",
            reason="上市状态、行业、ST和退市风险约束",
            adapter="existing",
            availability="available",
        ),
    )
    profile_items = []
    for dataset, stage, priority, scope, reason, adapter in _PROFILE_REQUIREMENTS[profile]:
        if dataset in available_datasets:
            availability = "available"
        elif adapter == "yinhe_on_demand":
            availability = "on_demand_only"
        elif adapter == "yinhe_planned":
            availability = "adapter_not_implemented"
        else:
            availability = "missing"
        profile_items.append(
            DataRequirement(
                dataset=dataset,
                stage=stage,
                priority=priority,
                scope=scope,
                reason=reason,
                adapter=adapter,
                availability=availability,
            )
        )
    return common + tuple(profile_items)


def run_funnel(
    securities: list[Security],
    bars: list[DailyBar],
    as_of: date,
    *,
    strategy_profile: str = "hybrid",
    constraints: UserConstraints | None = None,
    broad_pool_size: int = 300,
    refined_pool_size: int = 50,
    final_top_n: int = 10,
    confirmed: bool = False,
    valuations: list[ValuationSnapshot] | None = None,
    fundamentals: list[FundamentalSnapshot] | None = None,
    announcement_events: list[Any] | None = None,
    fulltext_analyses: list[Any] | None = None,
    horizon: str = "swing",
    momentum_days: int = 20,
    available_datasets: set[str] | None = None,
) -> ResearchFunnelRun:
    if min(broad_pool_size, refined_pool_size, final_top_n) <= 0:
        raise ValueError("研究漏斗各层候选数量必须为正数")
    if not broad_pool_size >= refined_pool_size >= final_top_n:
        raise ValueError("候选数量必须满足 broad_pool_size >= refined_pool_size >= final_top_n")

    active_constraints = constraints or UserConstraints()
    scoped, eliminated = apply_user_constraints(securities, active_constraints)
    broad = select_candidates(
        scoped,
        bars,
        as_of,
        horizon=horizon,
        momentum_days=momentum_days,
        top_n=broad_pool_size,
    )
    stages: list[FunnelStage] = [
        FunnelStage(
            stage_id="user_scope",
            label="用户与策略约束",
            status="completed",
            input_count=len(securities),
            output_symbols=tuple(item.symbol for item in scoped),
            eliminated_count=len(securities) - len(scoped),
            elimination_reasons=tuple(sorted(eliminated.items())),
        ),
        FunnelStage(
            stage_id="broad_screen",
            label="低成本全市场粗筛",
            status="completed",
            input_count=len(scoped),
            output_symbols=tuple(candidate.symbol for candidate in broad),
            eliminated_count=max(0, len(scoped) - len(broad)),
            elimination_reasons=(("eligibility_liquidity_or_score", max(0, len(scoped) - len(broad))),),
            human_confirmation_required=not confirmed,
        ),
    ]

    requirements = _requirements(strategy_profile, available_datasets or set())
    final_candidates: tuple[Candidate, ...] = ()
    status = "awaiting_user_confirmation"
    if confirmed:
        broad_symbols = {candidate.symbol for candidate in broad}
        broad_securities = [security for security in scoped if security.symbol in broad_symbols]
        refined = select_candidates(
            broad_securities,
            bars,
            as_of,
            valuations=valuations,
            fundamentals=fundamentals,
            announcement_events=announcement_events,
            fulltext_analyses=fulltext_analyses,
            horizon=horizon,
            momentum_days=momentum_days,
            top_n=refined_pool_size,
        )
        final_candidates = tuple(refined[:final_top_n])
        stages.extend(
            (
                FunnelStage(
                    stage_id="refinement",
                    label="策略相关数据精筛",
                    status="completed",
                    input_count=len(broad),
                    output_symbols=tuple(candidate.symbol for candidate in refined),
                    eliminated_count=max(0, len(broad) - len(refined)),
                    elimination_reasons=(("refined_score_or_risk_gate", max(0, len(broad) - len(refined))),),
                ),
                FunnelStage(
                    stage_id="fine_research",
                    label="高成本精细研究",
                    status="research_candidates_ready",
                    input_count=len(refined),
                    output_symbols=tuple(candidate.symbol for candidate in final_candidates),
                    eliminated_count=max(0, len(refined) - len(final_candidates)),
                    elimination_reasons=(("final_rank_cutoff", max(0, len(refined) - len(final_candidates))),),
                    human_confirmation_required=True,
                ),
            )
        )
        status = "research_candidates_ready"

    generated_at = datetime.now(UTC).isoformat()
    identity = json.dumps(
        {
            "as_of": as_of.isoformat(),
            "profile": strategy_profile,
            "constraints": active_constraints.to_dict(),
            "generated_at": generated_at,
        },
        ensure_ascii=False,
        sort_keys=True,
    )
    return ResearchFunnelRun(
        run_id=hashlib.sha256(identity.encode()).hexdigest()[:20],
        model_id=f"{strategy_profile}_research_funnel_v0_1",
        as_of=as_of.isoformat(),
        strategy_profile=strategy_profile,
        status=status,
        constraints=active_constraints,
        stages=tuple(stages),
        data_requirements=requirements,
        final_candidates=final_candidates,
        generated_at=generated_at,
    )


def write_funnel_run(
    project: Path,
    run: ResearchFunnelRun,
    output_path: Path | None = None,
) -> Path:
    path = output_path or (
        project
        / "runs"
        / "research_funnel"
        / run.as_of.replace("-", "")
        / f"{run.run_id}.json"
    )
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(run.to_dict(), ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    return path

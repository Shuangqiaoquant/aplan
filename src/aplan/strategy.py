from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path
from typing import Protocol


class StrategyStatus(StrEnum):
    RESEARCH = "research"
    VALIDATED = "validated"
    RETIRED = "retired"


class SignalIntent(StrEnum):
    WATCH = "watch"
    ENTER = "enter"
    REDUCE = "reduce"
    EXIT = "exit"


@dataclass(frozen=True, slots=True)
class StrategyMetadata:
    strategy_id: str
    version: str
    name: str
    status: StrategyStatus = StrategyStatus.RESEARCH
    approved_for_simulation: bool = False
    approved_for_live: bool = False

    def __post_init__(self) -> None:
        if not self.strategy_id or any(char.isspace() for char in self.strategy_id):
            raise ValueError("strategy_id 必须是无空格的非空字符串")
        if self.approved_for_live and not self.approved_for_simulation:
            raise ValueError("实盘审批必须建立在模拟盘审批之上")
        if self.status != StrategyStatus.VALIDATED and (
            self.approved_for_simulation or self.approved_for_live
        ):
            raise ValueError("只有 validated 策略可以获得模拟盘或实盘审批")


@dataclass(frozen=True, slots=True)
class StrategyContext:
    trade_date: str
    project_root: Path
    data_sha256: str
    mode: str = "research_only"


@dataclass(frozen=True, slots=True)
class Evidence:
    kind: str
    summary: str
    source: str
    observed_at: str


@dataclass(frozen=True, slots=True)
class UnifiedSignal:
    signal_id: str
    strategy_id: str
    strategy_version: str
    symbol: str
    trade_date: str
    intent: SignalIntent
    horizon_days: int
    score: float
    confidence: float
    target_weight: float
    evidence: tuple[Evidence, ...]
    risks: tuple[str, ...]
    invalidation: tuple[str, ...]
    generated_at: str
    data_sha256: str
    actionable: bool = False

    def __post_init__(self) -> None:
        if len(self.symbol) != 6 or not self.symbol.isdigit():
            raise ValueError("symbol 必须是六位数字")
        if self.horizon_days <= 0:
            raise ValueError("horizon_days 必须为正数")
        if not 0 <= self.score <= 100:
            raise ValueError("score 必须在 0–100 之间")
        if not 0 <= self.confidence <= 1:
            raise ValueError("confidence 必须在 0–1 之间")
        if not 0 <= self.target_weight <= 1:
            raise ValueError("target_weight 必须在 0–1 之间")
        if not self.evidence:
            raise ValueError("信号必须包含至少一条证据")
        if not self.risks:
            raise ValueError("信号必须包含至少一条反对证据或风险")
        if not self.invalidation:
            raise ValueError("信号必须包含失效条件")

    def to_dict(self) -> dict[str, object]:
        value = asdict(self)
        value["intent"] = self.intent.value
        return value


class StrategyPlugin(Protocol):
    metadata: StrategyMetadata

    def generate(self, context: StrategyContext) -> list[UnifiedSignal]:
        """只根据 context.trade_date 当时可见的数据生成信号。"""
        ...


def make_signal_id(
    strategy_id: str,
    strategy_version: str,
    symbol: str,
    trade_date: str,
    intent: SignalIntent,
) -> str:
    raw = "|".join((strategy_id, strategy_version, symbol, trade_date, intent.value))
    return hashlib.sha256(raw.encode()).hexdigest()[:24]


def new_signal(
    *,
    metadata: StrategyMetadata,
    context: StrategyContext,
    symbol: str,
    intent: SignalIntent,
    horizon_days: int,
    score: float,
    confidence: float,
    target_weight: float,
    evidence: tuple[Evidence, ...],
    risks: tuple[str, ...],
    invalidation: tuple[str, ...],
) -> UnifiedSignal:
    return UnifiedSignal(
        signal_id=make_signal_id(
            metadata.strategy_id,
            metadata.version,
            symbol,
            context.trade_date,
            intent,
        ),
        strategy_id=metadata.strategy_id,
        strategy_version=metadata.version,
        symbol=symbol,
        trade_date=context.trade_date,
        intent=intent,
        horizon_days=horizon_days,
        score=score,
        confidence=confidence,
        target_weight=target_weight,
        evidence=evidence,
        risks=risks,
        invalidation=invalidation,
        generated_at=datetime.now(UTC).isoformat(),
        data_sha256=context.data_sha256,
        actionable=False,
    )


def signal_set_sha256(signals: list[UnifiedSignal]) -> str:
    canonical = json.dumps(
        [signal.to_dict() for signal in signals],
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(canonical.encode()).hexdigest()


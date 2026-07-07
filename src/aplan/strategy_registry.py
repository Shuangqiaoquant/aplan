from __future__ import annotations

from dataclasses import dataclass, replace

from .strategy import (
    SignalIntent,
    StrategyContext,
    StrategyMetadata,
    StrategyPlugin,
    StrategyStatus,
    UnifiedSignal,
)


@dataclass(slots=True)
class StrategyRun:
    metadata: StrategyMetadata
    signals: list[UnifiedSignal]
    execution_allowed: bool
    blocked_reason: str | None


class StrategyRegistry:
    def __init__(self) -> None:
        self._plugins: dict[str, StrategyPlugin] = {}

    def register(self, plugin: StrategyPlugin) -> None:
        strategy_id = plugin.metadata.strategy_id
        if strategy_id in self._plugins:
            raise ValueError(f"策略已注册：{strategy_id}")
        self._plugins[strategy_id] = plugin

    def list_metadata(self) -> list[StrategyMetadata]:
        return sorted(
            (plugin.metadata for plugin in self._plugins.values()),
            key=lambda metadata: metadata.strategy_id,
        )

    def run(
        self,
        context: StrategyContext,
        *,
        allow_simulation: bool = False,
        allow_live: bool = False,
    ) -> list[StrategyRun]:
        runs: list[StrategyRun] = []
        seen_signal_ids: set[str] = set()
        for plugin in self._plugins.values():
            metadata = plugin.metadata
            if metadata.status == StrategyStatus.RETIRED:
                continue
            signals = plugin.generate(context)
            for signal in signals:
                if signal.strategy_id != metadata.strategy_id:
                    raise ValueError("信号 strategy_id 与插件不一致")
                if signal.strategy_version != metadata.version:
                    raise ValueError("信号 strategy_version 与插件不一致")
                if signal.trade_date != context.trade_date:
                    raise ValueError("信号日期与运行上下文不一致")
                if signal.data_sha256 != context.data_sha256:
                    raise ValueError("信号数据哈希与运行上下文不一致")
                if signal.signal_id in seen_signal_ids:
                    raise ValueError(f"重复 signal_id：{signal.signal_id}")
                seen_signal_ids.add(signal.signal_id)

            execution_allowed = False
            reason: str | None = None
            if metadata.status != StrategyStatus.VALIDATED:
                reason = "策略仍处于研究状态"
            elif allow_live:
                execution_allowed = metadata.approved_for_live
                if not execution_allowed:
                    reason = "策略未获实盘审批"
            elif allow_simulation:
                execution_allowed = metadata.approved_for_simulation
                if not execution_allowed:
                    reason = "策略未获模拟盘审批"
            else:
                reason = "当前运行未开启模拟盘或实盘执行"

            safe_signals = [
                replace(
                    signal,
                    actionable=(
                        execution_allowed
                        and signal.intent
                        in {SignalIntent.ENTER, SignalIntent.REDUCE, SignalIntent.EXIT}
                    ),
                )
                for signal in signals
            ]
            runs.append(StrategyRun(metadata, safe_signals, execution_allowed, reason))
        return runs


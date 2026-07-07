from __future__ import annotations

import argparse
import hashlib
import json
from dataclasses import asdict, dataclass, field
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any

from .models import Candidate, DailyBar
from .research_workflow import run_research_report, _load_bars_source


@dataclass(frozen=True, slots=True)
class ObservationFeedback:
    recorded_at: str
    note: str
    action: str = "note"
    realized_return: float | None = None


@dataclass(slots=True)
class Observation:
    observation_id: str
    signal_date: str
    symbol: str
    horizon: str
    rank: int
    score: float
    decision_band: str
    entry_style: str
    initial_close: float
    status: str = "watching"
    outcomes: dict[str, float] = field(default_factory=dict)
    feedback: list[ObservationFeedback] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        value = asdict(self)
        value["feedback"] = [asdict(item) for item in self.feedback]
        return value

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> "Observation":
        return cls(
            **{
                **value,
                "feedback": [
                    ObservationFeedback(**item)
                    for item in value.get("feedback", [])
                ],
            }
        )


class ObservationStore:
    def __init__(self, project_root: Path) -> None:
        self.path = project_root / "state" / "observations" / "observations.json"

    def load(self) -> list[Observation]:
        if not self.path.exists():
            return []
        document = json.loads(self.path.read_text(encoding="utf-8"))
        return [Observation.from_dict(item) for item in document.get("observations", [])]

    def save(self, observations: list[Observation]) -> Path:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "schema_version": 1,
            "saved_at": datetime.now(UTC).isoformat(),
            "observations": [item.to_dict() for item in sorted(observations, key=lambda x: x.observation_id)],
        }
        temporary = self.path.with_suffix(".tmp")
        temporary.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        temporary.replace(self.path)
        return self.path


def _observation_id(signal_date: date, symbol: str, horizon: str) -> str:
    raw = f"{signal_date.isoformat()}|{symbol}|{horizon}"
    return hashlib.sha256(raw.encode()).hexdigest()[:20]


def _close_by_symbol_date(bars: list[DailyBar]) -> dict[tuple[str, date], float]:
    return {(bar.symbol, bar.trade_date): bar.close for bar in bars}


def register_candidates(
    project: Path,
    signal_date: date,
    candidates: list[Candidate],
    bars: list[DailyBar],
) -> list[Observation]:
    close_map = _close_by_symbol_date(bars)
    observations: list[Observation] = []
    for rank, candidate in enumerate(sorted(candidates, key=lambda item: item.score, reverse=True), 1):
        initial_close = close_map.get((candidate.symbol, signal_date))
        if initial_close is None:
            continue
        observations.append(
            Observation(
                observation_id=_observation_id(signal_date, candidate.symbol, candidate.horizon),
                signal_date=signal_date.isoformat(),
                symbol=candidate.symbol,
                horizon=candidate.horizon,
                rank=rank,
                score=round(candidate.score, 4),
                decision_band=candidate.decision_band,
                entry_style=candidate.entry_style,
                initial_close=initial_close,
            )
        )
    store = ObservationStore(project)
    existing = {item.observation_id: item for item in store.load()}
    for item in observations:
        existing.setdefault(item.observation_id, item)
    store.save(list(existing.values()))
    return observations


def register_manual_position(
    project: Path,
    *,
    as_of: date,
    symbol: str,
    cost_basis: float,
    current_price: float,
    note: str,
    horizon: str = "risk_review",
) -> Observation:
    if cost_basis <= 0 or current_price <= 0:
        raise ValueError("成本价和当前价必须为正")
    observation = Observation(
        observation_id=_observation_id(as_of, symbol, horizon),
        signal_date=as_of.isoformat(),
        symbol=symbol,
        horizon=horizon,
        rank=0,
        score=0.0,
        decision_band="manual_risk_review",
        entry_style="manual_position",
        initial_close=current_price,
        status="risk_review",
        outcomes={
            "cost_basis": cost_basis,
            "current_price": current_price,
            "unrealized_return": current_price / cost_basis - 1,
        },
        feedback=[
            ObservationFeedback(
                recorded_at=datetime.now(UTC).isoformat(),
                action="risk_review",
                note=note,
                realized_return=None,
            )
        ],
    )
    store = ObservationStore(project)
    existing = {item.observation_id: item for item in store.load()}
    if observation.observation_id in existing:
        existing_observation = existing[observation.observation_id]
        existing_observation.outcomes.update(observation.outcomes)
        existing_observation.status = observation.status
        existing_observation.feedback.extend(observation.feedback)
        observation = existing_observation
    else:
        existing[observation.observation_id] = observation
    store.save(list(existing.values()))
    return observation


def _history_by_symbol(bars: list[DailyBar]) -> dict[str, list[DailyBar]]:
    histories: dict[str, list[DailyBar]] = {}
    for bar in sorted(bars, key=lambda item: (item.symbol, item.trade_date)):
        histories.setdefault(bar.symbol, []).append(bar)
    return histories


def update_outcomes(
    project: Path,
    bars: list[DailyBar],
    *,
    as_of: date,
    horizons: tuple[int, ...] = (5, 20, 60),
) -> list[Observation]:
    store = ObservationStore(project)
    observations = store.load()
    histories = _history_by_symbol([bar for bar in bars if bar.trade_date <= as_of])
    for observation in observations:
        signal_date = date.fromisoformat(observation.signal_date)
        history = histories.get(observation.symbol, [])
        dates = [bar.trade_date for bar in history]
        if signal_date not in dates:
            continue
        index = dates.index(signal_date)
        for horizon in horizons:
            key = f"{horizon}d_close_return"
            if key in observation.outcomes:
                continue
            target_index = index + horizon
            if target_index < len(history):
                close = history[target_index].close
                observation.outcomes[key] = close / observation.initial_close - 1
        if any(key.endswith("_return") for key in observation.outcomes):
            observation.status = "measured"
    store.save(observations)
    return observations


def add_feedback(
    project: Path,
    observation_id: str,
    *,
    note: str,
    action: str = "note",
    realized_return: float | None = None,
) -> Observation:
    store = ObservationStore(project)
    observations = store.load()
    for observation in observations:
        if observation.observation_id == observation_id:
            observation.feedback.append(
                ObservationFeedback(
                    recorded_at=datetime.now(UTC).isoformat(),
                    note=note,
                    action=action,
                    realized_return=realized_return,
                )
            )
            if action in {"bought", "sold", "skipped", "rejected"}:
                observation.status = action
            store.save(observations)
            return observation
    raise ValueError(f"未找到观察记录：{observation_id}")


def summarize_observations(observations: list[Observation]) -> dict[str, Any]:
    measured = [item for item in observations if item.outcomes]
    manual = [item for item in observations if item.decision_band.startswith("manual_")]
    summary: dict[str, Any] = {
        "total": len(observations),
        "measured": len(measured),
        "system_candidates": len(observations) - len(manual),
        "manual_risk_cases": len(manual),
        "by_status": {},
        "by_decision_band": {},
        "by_entry_style": {},
        "by_horizon": {},
    }
    for item in observations:
        summary["by_status"][item.status] = summary["by_status"].get(item.status, 0) + 1
    for group_name, attribute in (
        ("by_decision_band", "decision_band"),
        ("by_entry_style", "entry_style"),
        ("by_horizon", "horizon"),
    ):
        groups: dict[str, list[Observation]] = {}
        for item in observations:
            groups.setdefault(str(getattr(item, attribute)), []).append(item)
        summary[group_name] = {
            key: _summarize_group(values)
            for key, values in sorted(groups.items())
        }
    return summary


def _summarize_group(items: list[Observation]) -> dict[str, Any]:
    result: dict[str, Any] = {"count": len(items)}
    keys = sorted({key for item in items for key in item.outcomes if key.endswith("_return")})
    for key in keys:
        values = [item.outcomes[key] for item in items if key in item.outcomes]
        if values:
            result[key] = {
                "count": len(values),
                "average": sum(values) / len(values),
                "win_rate": sum(1 for value in values if value > 0) / len(values),
            }
    return result


def render_observation_review(observations: list[Observation]) -> str:
    summary = summarize_observations(observations)
    lines = [
        "# APlan 候选观察复盘",
        "",
        f"- 观察记录总数：{summary['total']}",
        f"- 已有收益结果记录：{summary['measured']}",
        f"- 系统候选记录：{summary['system_candidates']}",
        f"- 人工风险案例：{summary['manual_risk_cases']}",
        "",
        "## 状态分布",
        "",
    ]
    for status, count in sorted(summary["by_status"].items()):
        lines.append(f"- {status}: {count}")
    lines.extend(["", "## 按决策分层", ""])
    lines.extend(_render_group_summary(summary["by_decision_band"]))
    lines.extend(["", "## 按买入风格", ""])
    lines.extend(_render_group_summary(summary["by_entry_style"]))
    lines.extend(["", "## 按观察周期", ""])
    lines.extend(_render_group_summary(summary["by_horizon"]))
    lines.extend(
        [
            "",
            "## 解释",
            "",
            "- 样本少时只作观察，不更新策略权重。",
            "- 胜率和平均收益用于发现候选类型差异，不能单独作为买入依据。",
            "- 人工风险案例会进入状态分布，但不与系统候选混同解释。",
            "",
        ]
    )
    return "\n".join(lines)


def _render_group_summary(groups: dict[str, Any]) -> list[str]:
    lines: list[str] = []
    for name, data in groups.items():
        lines.append(f"### {name}")
        lines.append("")
        lines.append(f"- 样本数：{data['count']}")
        return_keys = [key for key in data if key.endswith("_return")]
        if not return_keys:
            lines.append("- 暂无收益结果")
        for key in sorted(return_keys):
            value = data[key]
            lines.append(
                f"- {key}: 样本 {value['count']}，平均收益 {value['average']:.2%}，胜率 {value['win_rate']:.1%}"
            )
        lines.append("")
    return lines


def _parse_horizons(value: str) -> tuple[int, ...]:
    return tuple(int(item.strip()) for item in value.split(",") if item.strip())


def _print_summary(observations: list[Observation]) -> None:
    print(json.dumps([item.to_dict() for item in observations], ensure_ascii=False, indent=2))


def main() -> None:
    parser = argparse.ArgumentParser(description="APlan 候选观察账本")
    subparsers = parser.add_subparsers(dest="command", required=True)

    register = subparsers.add_parser("register", help="从研究候选生成观察记录")
    register.add_argument("--root", default=".")
    register.add_argument("--bars", required=True)
    register.add_argument("--securities", required=True)
    register.add_argument("--date", required=True)
    register.add_argument("--horizon", choices=["short", "swing", "medium"], default="swing")
    register.add_argument("--top", type=int, default=10)
    register.add_argument("--valuations")
    register.add_argument("--fundamentals")
    register.add_argument("--announcements")
    register.add_argument("--announcement-analysis")

    update = subparsers.add_parser("update", help="用后续行情更新观察收益")
    update.add_argument("--root", default=".")
    update.add_argument("--bars", required=True)
    update.add_argument("--date", required=True)
    update.add_argument("--horizons", default="5,20,60")

    feedback = subparsers.add_parser("feedback", help="追加用户反馈")
    feedback.add_argument("--root", default=".")
    feedback.add_argument("--id", required=True)
    feedback.add_argument("--note", required=True)
    feedback.add_argument("--action", default="note", choices=["note", "bought", "sold", "skipped", "rejected"])
    feedback.add_argument("--realized-return", type=float)

    manual = subparsers.add_parser("manual-position", help="登记人工持仓风险案例")
    manual.add_argument("--root", default=".")
    manual.add_argument("--date", required=True)
    manual.add_argument("--symbol", required=True)
    manual.add_argument("--cost", type=float, required=True)
    manual.add_argument("--current-price", type=float, required=True)
    manual.add_argument("--note", required=True)

    show = subparsers.add_parser("show", help="显示观察账本")
    show.add_argument("--root", default=".")

    review = subparsers.add_parser("review", help="生成观察复盘报告")
    review.add_argument("--root", default=".")
    review.add_argument("--output")

    args = parser.parse_args()
    project = Path(args.root).resolve()
    if args.command == "register":
        as_of = date.fromisoformat(args.date)
        result = run_research_report(
            project,
            bars_path=args.bars,
            securities_path=args.securities,
            as_of=as_of,
            horizon=args.horizon,
            top_n=args.top,
            valuations_path=args.valuations,
            fundamentals_path=args.fundamentals,
            announcements_path=args.announcements,
            announcement_analysis_path=args.announcement_analysis,
        )
        bars = _load_bars_source(args.bars, as_of)
        observations = register_candidates(project, as_of, result.candidates, bars)
        _print_summary(observations)
    elif args.command == "update":
        as_of = date.fromisoformat(args.date)
        observations = update_outcomes(
            project,
            _load_bars_source(args.bars, as_of),
            as_of=as_of,
            horizons=_parse_horizons(args.horizons),
        )
        _print_summary(observations)
    elif args.command == "feedback":
        observation = add_feedback(
            project,
            args.id,
            note=args.note,
            action=args.action,
            realized_return=args.realized_return,
        )
        _print_summary([observation])
    elif args.command == "manual-position":
        observation = register_manual_position(
            project,
            as_of=date.fromisoformat(args.date),
            symbol=args.symbol,
            cost_basis=args.cost,
            current_price=args.current_price,
            note=args.note,
        )
        _print_summary([observation])
    elif args.command == "show":
        _print_summary(ObservationStore(project).load())
    elif args.command == "review":
        report = render_observation_review(ObservationStore(project).load())
        if args.output:
            output = Path(args.output)
            output.parent.mkdir(parents=True, exist_ok=True)
            output.write_text(report, encoding="utf-8")
        else:
            print(report)


if __name__ == "__main__":
    main()

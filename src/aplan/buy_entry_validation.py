from __future__ import annotations

import argparse
import json
from dataclasses import asdict, dataclass
from datetime import date
from pathlib import Path
from typing import Any

from .io import load_securities_csv
from .models import Candidate, DailyBar
from .pipeline import select_candidates
from .pretrade import evaluate_next_day_confirmation
from .research_workflow import _load_bars_source


@dataclass(frozen=True, slots=True)
class BuyEntryValidationRecord:
    signal_date: str
    symbol: str
    score: float
    decision_band: str
    entry_style: str
    gate_decision: str
    blockers: tuple[str, ...]
    warnings: tuple[str, ...]
    gap_open: float
    forward_return: float

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def _history_by_symbol(bars: list[DailyBar]) -> dict[str, list[DailyBar]]:
    histories: dict[str, list[DailyBar]] = {}
    for bar in sorted(bars, key=lambda item: (item.symbol, item.trade_date)):
        histories.setdefault(bar.symbol, []).append(bar)
    return histories


def _bar_at_or_after(history: list[DailyBar], signal_date: date) -> tuple[int, DailyBar] | None:
    for index, bar in enumerate(history):
        if bar.trade_date == signal_date:
            return index, bar
    return None


def _gap_bucket(gap_open: float) -> str:
    if gap_open > 0.05:
        return "gap_gt_5pct"
    if gap_open > 0.02:
        return "gap_2_to_5pct"
    if gap_open >= -0.02:
        return "gap_-2_to_2pct"
    return "gap_lt_-2pct"


def validate_candidate_entries(
    candidates: list[Candidate],
    bars: list[DailyBar],
    signal_date: date,
    *,
    horizon_days: int = 20,
    slippage_rate: float = 0.001,
) -> list[BuyEntryValidationRecord]:
    histories = _history_by_symbol(bars)
    records: list[BuyEntryValidationRecord] = []
    for candidate in candidates:
        history = histories.get(candidate.symbol, [])
        located = _bar_at_or_after(history, signal_date)
        if located is None:
            continue
        signal_index, signal_bar = located
        entry_index = signal_index + 1
        exit_index = entry_index + horizon_days
        if exit_index >= len(history):
            continue
        next_bar = history[entry_index]
        exit_bar = history[exit_index]
        confirmation = evaluate_next_day_confirmation(candidate, signal_bar, next_bar)
        entry_price = next_bar.open * (1 + slippage_rate)
        forward_return = exit_bar.close / entry_price - 1
        records.append(
            BuyEntryValidationRecord(
                signal_date=signal_date.isoformat(),
                symbol=candidate.symbol,
                score=round(candidate.score, 4),
                decision_band=candidate.decision_band,
                entry_style=candidate.entry_style,
                gate_decision=confirmation.decision,
                blockers=confirmation.blockers,
                warnings=confirmation.warnings,
                gap_open=confirmation.gap_open,
                forward_return=forward_return,
            )
        )
    return records


def _summarize_group(records: list[BuyEntryValidationRecord]) -> dict[str, Any]:
    if not records:
        return {"count": 0}
    returns = [record.forward_return for record in records]
    return {
        "count": len(records),
        "average_return": sum(returns) / len(returns),
        "win_rate": sum(1 for value in returns if value > 0) / len(returns),
        "best_return": max(returns),
        "worst_return": min(returns),
    }


def summarize_records(records: list[BuyEntryValidationRecord]) -> dict[str, Any]:
    summary: dict[str, Any] = {
        "total": len(records),
        "by_gate_decision": {},
        "by_policy_variant": {},
        "by_decision_band": {},
        "by_entry_style": {},
        "by_gap_bucket": {},
        "top_blockers": {},
        "blocked_missed_winners": 0,
        "blocked_avoided_losers": 0,
    }
    for group_name, key_fn in (
        ("by_gate_decision", lambda item: item.gate_decision),
        ("by_decision_band", lambda item: item.decision_band),
        ("by_entry_style", lambda item: item.entry_style),
        ("by_gap_bucket", lambda item: _gap_bucket(item.gap_open)),
    ):
        groups: dict[str, list[BuyEntryValidationRecord]] = {}
        for record in records:
            groups.setdefault(str(key_fn(record)), []).append(record)
        summary[group_name] = {
            key: _summarize_group(values)
            for key, values in sorted(groups.items())
        }
    summary["by_policy_variant"] = _summarize_policy_variants(records)
    blocked = [record for record in records if record.gate_decision != "paper_trade_confirmed"]
    blocker_counts: dict[str, int] = {}
    for record in blocked:
        for blocker in record.blockers:
            key = _blocker_bucket(blocker)
            blocker_counts[key] = blocker_counts.get(key, 0) + 1
    summary["top_blockers"] = dict(sorted(blocker_counts.items(), key=lambda item: item[1], reverse=True))
    summary["blocked_missed_winners"] = sum(1 for record in blocked if record.forward_return > 0)
    summary["blocked_avoided_losers"] = sum(1 for record in blocked if record.forward_return <= 0)
    return summary


def _has_blocker(record: BuyEntryValidationRecord, buckets: set[str]) -> bool:
    return any(_blocker_bucket(blocker) in buckets for blocker in record.blockers)


def _variant_decision(record: BuyEntryValidationRecord, variant: str) -> str:
    technical_blockers = {
        "failed_breakout_continuation",
        "gap_chase_block",
        "signal_invalidated",
    }
    style_blockers = {"entry_style_unvalidated"}
    band_blockers = {"decision_band_research_only"}
    evidence_blockers = {"evidence_gap_too_many"}
    if variant == "strict_current":
        return "confirmed" if record.gate_decision == "paper_trade_confirmed" else "blocked"
    if variant == "ignore_evidence_gap":
        ignored = evidence_blockers
    elif variant == "ignore_evidence_and_research_band":
        ignored = evidence_blockers | band_blockers
    elif variant == "technical_only":
        ignored = evidence_blockers | band_blockers | style_blockers
    else:
        raise ValueError(f"未知验证变体：{variant}")
    active_blockers = {
        _blocker_bucket(blocker)
        for blocker in record.blockers
        if _blocker_bucket(blocker) not in ignored
    }
    if variant == "technical_only":
        return "confirmed" if not (active_blockers & technical_blockers) else "blocked"
    return "confirmed" if not active_blockers else "blocked"


def _summarize_policy_variants(records: list[BuyEntryValidationRecord]) -> dict[str, Any]:
    output: dict[str, Any] = {}
    for variant in (
        "strict_current",
        "ignore_evidence_gap",
        "ignore_evidence_and_research_band",
        "technical_only",
    ):
        confirmed = [record for record in records if _variant_decision(record, variant) == "confirmed"]
        blocked = [record for record in records if _variant_decision(record, variant) == "blocked"]
        output[variant] = {
            "confirmed": _summarize_group(confirmed),
            "blocked": _summarize_group(blocked),
        }
    return output


def _blocker_bucket(blocker: str) -> str:
    if "research_candidate" in blocker:
        return "decision_band_research_only"
    if "证据缺口" in blocker:
        return "evidence_gap_too_many"
    if "突破延续型" in blocker:
        return "failed_breakout_continuation"
    if "追价上限" in blocker:
        return "gap_chase_block"
    if "信号失效" in blocker or "信号日低点" in blocker:
        return "signal_invalidated"
    if "尚未形成可执行规则" in blocker:
        return "entry_style_unvalidated"
    return blocker[:40]


def render_validation_report(records: list[BuyEntryValidationRecord], *, horizon_days: int) -> str:
    summary = summarize_records(records)
    lines = [
        "# 买入策略 v0.1 验证报告",
        "",
        f"- 策略状态：`v0.1-research` / `research_only`",
        f"- 收益观察窗口：次日开盘买入后 {horizon_days} 个交易日收盘",
        f"- 样本数：{summary['total']}",
        f"- 阻断后错过上涨样本：{summary['blocked_missed_winners']}",
        f"- 阻断后避免下跌/持平样本：{summary['blocked_avoided_losers']}",
        "",
    ]
    lines.extend(["## 拆分规则验证", ""])
    lines.extend(_render_variant_table(summary["by_policy_variant"]))
    lines.extend(["", "## 初步策略解读", ""])
    lines.extend(_render_interpretation(summary["by_policy_variant"]))
    lines.extend(["", "## 按买入闸门", ""])
    lines.extend(_render_summary_table(summary["by_gate_decision"]))
    lines.extend(["", "## 按决策分层", ""])
    lines.extend(_render_summary_table(summary["by_decision_band"]))
    lines.extend(["", "## 按买入风格", ""])
    lines.extend(_render_summary_table(summary["by_entry_style"]))
    lines.extend(["", "## 按次日开盘缺口", ""])
    lines.extend(_render_summary_table(summary["by_gap_bucket"]))
    lines.extend(["", "## 主要阻断原因", ""])
    lines.extend(_render_blocker_table(summary["top_blockers"]))
    lines.extend(
        [
            "",
            "## 解释",
            "",
            "- 这是规则验证，不是交易建议。",
            "- 当前策略仍处 research_only，确认通过也不代表可以实盘买入。",
            "- 如果样本少于 20 个，只记录观察，不更新核心规则。",
            "- 需要重点比较：确认样本 vs 阻断样本、不同高开区间、成交额不足警告。",
            "",
        ]
    )
    return "\n".join(lines)


def _render_summary_table(groups: dict[str, Any]) -> list[str]:
    lines = [
        "| 分组 | 样本 | 平均收益 | 胜率 | 最好 | 最差 |",
        "|---|---:|---:|---:|---:|---:|",
    ]
    if not groups:
        lines.append("| 无 | 0 | - | - | - | - |")
        return lines
    for name, data in groups.items():
        if data.get("count", 0) == 0:
            lines.append(f"| {name} | 0 | - | - | - | - |")
            continue
        lines.append(
            f"| {name} | {data['count']} | {data['average_return']:.2%} | "
            f"{data['win_rate']:.1%} | {data['best_return']:.2%} | {data['worst_return']:.2%} |"
        )
    return lines


def _render_variant_table(variants: dict[str, Any]) -> list[str]:
    labels = {
        "strict_current": "严格现行规则",
        "ignore_evidence_gap": "只放松证据缺口",
        "ignore_evidence_and_research_band": "放松证据缺口+研究分层",
        "technical_only": "只看次日价格/成交额确认",
    }
    lines = [
        "| 规则假设 | 确认样本 | 确认平均收益 | 确认胜率 | 阻断样本 | 阻断平均收益 | 阻断胜率 |",
        "|---|---:|---:|---:|---:|---:|---:|",
    ]
    for key, data in variants.items():
        confirmed = data["confirmed"]
        blocked = data["blocked"]
        lines.append(
            f"| {labels.get(key, key)} | "
            f"{confirmed.get('count', 0)} | {_fmt_metric(confirmed, 'average_return')} | {_fmt_metric(confirmed, 'win_rate')} | "
            f"{blocked.get('count', 0)} | {_fmt_metric(blocked, 'average_return')} | {_fmt_metric(blocked, 'win_rate')} |"
        )
    return lines


def _fmt_metric(data: dict[str, Any], key: str) -> str:
    if data.get("count", 0) == 0 or key not in data:
        return "-"
    return f"{data[key]:.2%}" if key == "average_return" else f"{data[key]:.1%}"


def _render_interpretation(variants: dict[str, Any]) -> list[str]:
    lines = [
        "- 严格现行规则如果确认样本为 0，说明规则无法产生可验证买入样本，不能直接推广。",
    ]
    relaxed = variants.get("ignore_evidence_gap", {}).get("confirmed", {})
    technical = variants.get("technical_only", {}).get("confirmed", {})
    relaxed_band = variants.get("ignore_evidence_and_research_band", {}).get("confirmed", {})
    if relaxed.get("count", 0):
        lines.append(
            f"- 只放松证据缺口后，确认样本 {relaxed['count']} 个，平均收益 {relaxed['average_return']:.2%}；若表现仍弱，说明证据缺口不是唯一问题。"
        )
    if relaxed_band.get("count", 0):
        lines.append(
            f"- 放松证据缺口和研究分层后，确认样本 {relaxed_band['count']} 个，平均收益 {relaxed_band['average_return']:.2%}，可作为下一轮重点验证假设。"
        )
    if technical.get("count", 0):
        lines.append(
            f"- 只看次日技术确认后，确认样本 {technical['count']} 个，平均收益 {technical['average_return']:.2%}；这用于评估买点本身，不代表基本面/公告风险可以忽略。"
        )
    lines.append("- 任何放松规则都必须再做更大样本、分年份/市场环境验证后，才能进入规则变更讨论。")
    return lines


def _render_blocker_table(blockers: dict[str, int]) -> list[str]:
    lines = ["| 阻断原因 | 次数 |", "|---|---:|"]
    if not blockers:
        lines.append("| 无 | 0 |")
        return lines
    for name, count in blockers.items():
        lines.append(f"| {name} | {count} |")
    return lines


def _select_signal_dates(bars: list[DailyBar], *, max_dates: int, min_history_days: int, horizon_days: int) -> list[date]:
    dates = sorted({bar.trade_date for bar in bars})
    if len(dates) <= min_history_days + horizon_days + 1:
        return []
    usable = dates[min_history_days : len(dates) - horizon_days - 1]
    if max_dates <= 0 or len(usable) <= max_dates:
        return usable
    step = max(1, len(usable) // max_dates)
    return usable[::step][:max_dates]


def run_buy_entry_validation(
    project: Path,
    *,
    bars_path: str | Path,
    securities_path: str | Path,
    horizon: str = "swing",
    top_n: int = 10,
    horizon_days: int = 20,
    max_signal_dates: int = 60,
) -> tuple[list[BuyEntryValidationRecord], str]:
    bars = _load_bars_source(bars_path, date.max)
    securities = load_securities_csv(securities_path)
    records: list[BuyEntryValidationRecord] = []
    for signal_date in _select_signal_dates(
        bars,
        max_dates=max_signal_dates,
        min_history_days=max(70, horizon_days + 25),
        horizon_days=horizon_days,
    ):
        candidates = select_candidates(
            securities,
            bars,
            signal_date,
            horizon=horizon,
            momentum_days={"short": 5, "swing": 20, "medium": 60}[horizon],
            top_n=top_n,
        )
        records.extend(
            validate_candidate_entries(
                candidates,
                bars,
                signal_date,
                horizon_days=horizon_days,
            )
        )
    return records, render_validation_report(records, horizon_days=horizon_days)


def main() -> None:
    parser = argparse.ArgumentParser(description="验证买入策略 v0.1 的次日确认规则")
    parser.add_argument("--root", default=".")
    parser.add_argument("--bars", default="data/processed/daily")
    parser.add_argument("--securities", default="data/processed/securities.csv")
    parser.add_argument("--horizon", choices=["short", "swing", "medium"], default="swing")
    parser.add_argument("--top", type=int, default=10)
    parser.add_argument("--horizon-days", type=int, default=20)
    parser.add_argument("--max-signal-dates", type=int, default=60)
    parser.add_argument("--output", default="reports/buy_entry_validation/report.md")
    parser.add_argument("--records-output", default="reports/buy_entry_validation/records.json")
    args = parser.parse_args()
    project = Path(args.root).resolve()
    records, report = run_buy_entry_validation(
        project,
        bars_path=project / args.bars if not Path(args.bars).is_absolute() else args.bars,
        securities_path=project / args.securities if not Path(args.securities).is_absolute() else args.securities,
        horizon=args.horizon,
        top_n=args.top,
        horizon_days=args.horizon_days,
        max_signal_dates=args.max_signal_dates,
    )
    output = project / args.output if not Path(args.output).is_absolute() else Path(args.output)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(report, encoding="utf-8")
    records_output = project / args.records_output if not Path(args.records_output).is_absolute() else Path(args.records_output)
    records_output.parent.mkdir(parents=True, exist_ok=True)
    records_output.write_text(
        json.dumps([record.to_dict() for record in records], ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    print(f"报告已写入 {output}")
    print(f"记录已写入 {records_output}")


if __name__ == "__main__":
    main()

from __future__ import annotations

import argparse
import csv
import json
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Callable

from .announcements import EventImpact, RiskLevel
from .research_backtest import (
    Signal,
    collect_selected_rows,
    evaluate_signals_from_rows,
    generate_signals,
    snapshot_dates,
    summarize,
)


@dataclass(frozen=True, slots=True)
class EvidenceFlag:
    symbol: str
    signal_date: str
    bad_valuation: bool = False
    high_risk_announcement: bool = False
    critical_announcement: bool = False
    negative_or_mixed_announcement: bool = False
    fundamental_risk: bool = False

    @property
    def any_risk(self) -> bool:
        return any(
            (
                self.bad_valuation,
                self.high_risk_announcement,
                self.critical_announcement,
                self.negative_or_mixed_announcement,
                self.fundamental_risk,
            )
        )


VariantPredicate = Callable[[Signal, EvidenceFlag], bool]


VARIANTS: dict[str, VariantPredicate] = {
    "baseline": lambda _signal, _flag: True,
    "exclude_bad_valuation": lambda _signal, flag: not flag.bad_valuation,
    "exclude_high_risk_announcement": lambda _signal, flag: not (
        flag.high_risk_announcement or flag.critical_announcement
    ),
    "exclude_announcement_risk": lambda _signal, flag: not (
        flag.high_risk_announcement
        or flag.critical_announcement
        or flag.negative_or_mixed_announcement
    ),
    "exclude_all_evidence_risks": lambda _signal, flag: not flag.any_risk,
}


def _date_key_from_iso(value: str) -> str | None:
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.strftime("%Y%m%d")


def load_valuation_flags(project: Path) -> dict[tuple[str, str], dict[str, bool]]:
    flags: dict[tuple[str, str], dict[str, bool]] = {}
    for path in sorted((project / "data" / "processed" / "valuations").glob("20??????.csv")):
        trade_date = path.stem
        with path.open(encoding="utf-8-sig", newline="") as handle:
            for row in csv.DictReader(handle):
                pe = float(row.get("pe") or 0)
                pb = float(row.get("pb") or 0)
                flags[(str(row["symbol"]), trade_date)] = {
                    "bad_valuation": pe <= 0 or pb <= 0 or pe > 60 or pb > 8
                }
    return flags


def load_announcement_flags(project: Path) -> dict[tuple[str, str], dict[str, bool]]:
    flags: dict[tuple[str, str], dict[str, bool]] = {}
    for path in sorted((project / "data" / "processed" / "announcements").glob("20??????.json")):
        document = json.loads(path.read_text(encoding="utf-8"))
        for event in document.get("events", []):
            symbol = str(event.get("symbol", ""))
            published_date = _date_key_from_iso(str(event.get("published_at", "")))
            if not symbol or not published_date:
                continue
            key = (symbol, published_date)
            current = flags.setdefault(
                key,
                {
                    "high_risk_announcement": False,
                    "critical_announcement": False,
                    "negative_or_mixed_announcement": False,
                },
            )
            risk = str(event.get("risk_level", ""))
            impact = str(event.get("impact_hint", ""))
            current["critical_announcement"] = current["critical_announcement"] or risk == RiskLevel.CRITICAL.value
            current["high_risk_announcement"] = current["high_risk_announcement"] or risk == RiskLevel.HIGH.value
            current["negative_or_mixed_announcement"] = current["negative_or_mixed_announcement"] or impact in {
                EventImpact.NEGATIVE.value,
                EventImpact.MIXED.value,
            }
    return flags


def evidence_flags_for_signals(project: Path, signals: list[Signal]) -> dict[tuple[str, str], EvidenceFlag]:
    valuation_flags = load_valuation_flags(project)
    announcement_flags = load_announcement_flags(project)
    output: dict[tuple[str, str], EvidenceFlag] = {}
    for signal in signals:
        key = (signal.symbol, signal.signal_date)
        valuation = valuation_flags.get(key, {})
        announcement = announcement_flags.get(key, {})
        output[key] = EvidenceFlag(
            symbol=signal.symbol,
            signal_date=signal.signal_date,
            bad_valuation=bool(valuation.get("bad_valuation", False)),
            high_risk_announcement=bool(announcement.get("high_risk_announcement", False)),
            critical_announcement=bool(announcement.get("critical_announcement", False)),
            negative_or_mixed_announcement=bool(
                announcement.get("negative_or_mixed_announcement", False)
            ),
        )
    return output


def compare_variants(
    dates: list[str],
    signals: list[Signal],
    rows_by_symbol: dict[str, dict[str, dict[str, object]]],
    flags: dict[tuple[str, str], EvidenceFlag],
    *,
    holding_days: int,
    variants: dict[str, VariantPredicate] = VARIANTS,
) -> dict[str, dict[str, float]]:
    records: dict[str, dict[str, float]] = {}
    flagged_counts = {
        "bad_valuation_flags": sum(flag.bad_valuation for flag in flags.values()),
        "high_risk_announcement_flags": sum(
            flag.high_risk_announcement or flag.critical_announcement for flag in flags.values()
        ),
        "announcement_risk_flags": sum(
            flag.high_risk_announcement
            or flag.critical_announcement
            or flag.negative_or_mixed_announcement
            for flag in flags.values()
        ),
        "any_evidence_risk_flags": sum(flag.any_risk for flag in flags.values()),
    }
    for name, predicate in variants.items():
        filtered = [
            signal
            for signal in signals
            if predicate(
                signal,
                flags.get((signal.symbol, signal.signal_date), EvidenceFlag(signal.symbol, signal.signal_date)),
            )
        ]
        results = evaluate_signals_from_rows(
            dates,
            filtered,
            rows_by_symbol,
            holding_days=holding_days,
        )
        metrics = summarize(results, holding_days)
        records[name] = {
            "input_signals": float(len(signals)),
            "kept_signals": float(len(filtered)),
            "dropped_signals": float(len(signals) - len(filtered)),
            **{key: float(value) for key, value in flagged_counts.items()},
            **metrics,
        }
    return records


def render_report(records: dict[str, dict[str, float]], holding_days: int) -> str:
    lines = [
        "# APlan 证据层变体验证",
        "",
        f"持有周期：{holding_days} 日",
        "",
        "| 变体 | 保留信号 | 删除信号 | 交易数 | 胜率 | 平均单笔 | 年化收益 | 最大回撤 | Sharpe |",
        "|---|---:|---:|---:|---:|---:|---:|---:|---:|",
    ]
    for name, metrics in records.items():
        lines.append(
            f"| {name} | {int(metrics.get('kept_signals', 0))} | "
            f"{int(metrics.get('dropped_signals', 0))} | {int(metrics.get('trades', 0))} | "
            f"{metrics.get('win_rate', 0):.1%} | {metrics.get('mean_trade_return', 0):.2%} | "
            f"{metrics.get('annualized_return', 0):.2%} | {metrics.get('max_drawdown', 0):.2%} | "
            f"{metrics.get('annualized_sharpe', 0):.2f} |"
        )
    lines.extend(
        [
            "",
            "## 证据覆盖",
            "",
            "| 风险类型 | 命中信号数 |",
            "|---|---:|",
        ]
    )
    first = next(iter(records.values()), {})
    lines.extend(
        [
            f"| 坏估值 | {int(first.get('bad_valuation_flags', 0))} |",
            f"| 高风险公告 | {int(first.get('high_risk_announcement_flags', 0))} |",
            f"| 负面/混合/高风险公告 | {int(first.get('announcement_risk_flags', 0))} |",
            f"| 任一证据风险 | {int(first.get('any_evidence_risk_flags', 0))} |",
            "",
            "## 解释",
            "",
            "- 这是证据层开关验证，不是策略通过证明。",
            "- 若某个变体删除信号很少，其结果不可过度解读。",
            "- 只有在多周期、多调仓起点、样本外验证也改善时，证据层才可升级为正式规则。",
            "",
        ]
    )
    return "\n".join(lines)


def main() -> None:
    parser = argparse.ArgumentParser(description="验证估值、公告、基本面等证据层过滤是否改善结果")
    parser.add_argument("--root", default=".")
    parser.add_argument("--output", default="reports/evidence_validation")
    parser.add_argument("--holding-days", type=int, default=40)
    parser.add_argument("--momentum-days", type=int, default=20)
    parser.add_argument("--top", type=int, default=10)
    parser.add_argument("--offset", type=int, default=0)
    args = parser.parse_args()

    project = Path(args.root).resolve()
    raw_root = project / "data" / "raw" / "tushare"
    dates = snapshot_dates(raw_root)
    signals = generate_signals(
        raw_root,
        dates,
        holding_days=args.holding_days,
        momentum_days=args.momentum_days,
        top_n=args.top,
        rebalance_offset=args.offset,
    )
    rows = collect_selected_rows(raw_root, dates, {signal.symbol for signal in signals})
    flags = evidence_flags_for_signals(project, signals)
    records = compare_variants(
        dates,
        signals,
        rows,
        flags,
        holding_days=args.holding_days,
    )

    output = project / args.output
    output.mkdir(parents=True, exist_ok=True)
    (output / "metrics.json").write_text(
        json.dumps(records, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    (output / "report.md").write_text(render_report(records, args.holding_days), encoding="utf-8")
    print(f"报告已写入 {output / 'report.md'}")


if __name__ == "__main__":
    main()

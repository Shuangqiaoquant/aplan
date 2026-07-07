from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any


def _date_dirs(path: Path) -> set[str]:
    return {
        item.name
        for item in path.iterdir()
        if item.is_dir() and len(item.name) == 8 and item.name.isdigit()
    } if path.exists() else set()


def _snapshot_dates(root: Path, filename: str) -> set[str]:
    return {
        path.parent.name
        for path in root.glob(f"20??????/{filename}")
        if path.parent.name.isdigit()
    }


def _processed_dates(root: Path, pattern: str = "*.csv") -> set[str]:
    return {
        path.stem
        for path in root.glob(pattern)
        if len(path.stem) == 8 and path.stem.isdigit()
    } if root.exists() else set()


def build_coverage(project: Path) -> dict[str, Any]:
    raw_tushare = project / "data" / "raw" / "tushare"
    raw_dates = _date_dirs(raw_tushare) - {"indices"}
    raw_daily = _snapshot_dates(raw_tushare, "daily.json")
    raw_daily_basic = _snapshot_dates(raw_tushare, "daily_basic.json")
    processed_daily = _processed_dates(project / "data" / "processed" / "daily")
    processed_valuations = _processed_dates(project / "data" / "processed" / "valuations")
    processed_announcements = _processed_dates(
        project / "data" / "processed" / "announcements",
        "*.json",
    )
    processed_fulltext = _processed_dates(
        project / "data" / "processed" / "announcement_analysis",
        "*.json",
    )
    trade_dates = sorted(raw_daily | processed_daily)

    def pct(values: set[str]) -> float:
        return len(values & set(trade_dates)) / len(trade_dates) if trade_dates else 0.0

    missing_daily_basic = sorted(set(trade_dates) - raw_daily_basic)
    missing_valuations = sorted(set(trade_dates) - processed_valuations)
    return {
        "trade_dates": len(trade_dates),
        "raw_date_dirs": len(raw_dates),
        "raw_daily": len(raw_daily),
        "processed_daily": len(processed_daily),
        "raw_daily_basic": len(raw_daily_basic),
        "processed_valuations": len(processed_valuations),
        "processed_announcements": len(processed_announcements),
        "processed_fulltext": len(processed_fulltext),
        "coverage": {
            "raw_daily_basic": pct(raw_daily_basic),
            "processed_valuations": pct(processed_valuations),
            "processed_announcements": pct(processed_announcements),
            "processed_fulltext": pct(processed_fulltext),
        },
        "missing_daily_basic_sample": missing_daily_basic[:20],
        "missing_valuations_sample": missing_valuations[:20],
        "first_trade_date": trade_dates[0] if trade_dates else None,
        "last_trade_date": trade_dates[-1] if trade_dates else None,
    }


def render_report(coverage: dict[str, Any]) -> str:
    values = coverage["coverage"]
    lines = [
        "# APlan 证据覆盖报告",
        "",
        f"交易日范围：{coverage.get('first_trade_date')}–{coverage.get('last_trade_date')}",
        f"交易日数量：{coverage['trade_dates']}",
        "",
        "| 证据层 | 原始/处理数量 | 覆盖率 |",
        "|---|---:|---:|",
        f"| 日线 raw daily | {coverage['raw_daily']} | 100.0% |",
        f"| 日线 processed daily | {coverage['processed_daily']} | 100.0% |",
        f"| 估值 raw daily_basic | {coverage['raw_daily_basic']} | {values['raw_daily_basic']:.1%} |",
        f"| 估值 processed valuations | {coverage['processed_valuations']} | {values['processed_valuations']:.1%} |",
        f"| 公告 processed announcements | {coverage['processed_announcements']} | {values['processed_announcements']:.1%} |",
        f"| 公告全文 analysis | {coverage['processed_fulltext']} | {values['processed_fulltext']:.1%} |",
        "",
        "## 缺口样例",
        "",
        f"- 缺 raw daily_basic 前20个日期：{', '.join(coverage['missing_daily_basic_sample']) or '无'}",
        f"- 缺 processed valuations 前20个日期：{', '.join(coverage['missing_valuations_sample']) or '无'}",
        "",
        "## 解释",
        "",
        "- 证据覆盖不足时，证据层验证不会删除足够多的历史信号，不能判断证据有效或无效。",
        "- 优先补齐 daily_basic，因为它已有同步字段且可直接支持估值验证。",
        "- 公告历史同步成本更高，适合按候选信号日期或近年区间逐步补。",
        "",
    ]
    return "\n".join(lines)


def main() -> None:
    parser = argparse.ArgumentParser(description="统计估值、公告、基本面等证据覆盖率")
    parser.add_argument("--root", default=".")
    parser.add_argument("--output", default="reports/evidence_coverage")
    args = parser.parse_args()
    project = Path(args.root).resolve()
    coverage = build_coverage(project)
    output = project / args.output
    output.mkdir(parents=True, exist_ok=True)
    (output / "metrics.json").write_text(
        json.dumps(coverage, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    (output / "report.md").write_text(render_report(coverage), encoding="utf-8")
    print(f"报告已写入 {output / 'report.md'}")


if __name__ == "__main__":
    main()

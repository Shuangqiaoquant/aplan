from __future__ import annotations

import argparse
from datetime import date
from pathlib import Path

from .io import (
    load_announcement_events_json,
    load_bars_csv,
    load_fundamentals_csv,
    load_fulltext_analyses_json,
    load_securities_csv,
    load_valuations_csv,
)
from .pipeline import select_candidates
from .reports import render_daily_report


def main() -> None:
    parser = argparse.ArgumentParser(description="APlan A 股研究工具")
    parser.add_argument("--bars", required=True, help="日线 CSV")
    parser.add_argument("--securities", required=True, help="股票信息 CSV")
    parser.add_argument("--date", required=True, help="截止日期 YYYY-MM-DD")
    parser.add_argument("--horizon", choices=["short", "swing", "medium"], default="swing")
    parser.add_argument("--top", type=int, default=10)
    parser.add_argument("--valuations", help="估值 CSV；可选，来自 data/processed/valuations/YYYYMMDD.csv")
    parser.add_argument("--fundamentals", help="基本面 CSV；可选，必须包含 period_end 和 publish_time")
    parser.add_argument("--announcements", help="公告事件 JSON；可选，来自 data/processed/announcements/YYYYMMDD.json")
    parser.add_argument(
        "--announcement-analysis",
        help="公告全文分析 JSON；可选，来自 data/processed/announcement_analysis/YYYYMMDD.json",
    )
    parser.add_argument("--output", help="报告输出路径；不传则打印")
    args = parser.parse_args()

    lookbacks = {"short": 5, "swing": 20, "medium": 60}
    as_of = date.fromisoformat(args.date)
    candidates = select_candidates(
        load_securities_csv(args.securities),
        load_bars_csv(args.bars),
        as_of,
        valuations=load_valuations_csv(args.valuations) if args.valuations else None,
        fundamentals=load_fundamentals_csv(args.fundamentals) if args.fundamentals else None,
        announcement_events=load_announcement_events_json(args.announcements)
        if args.announcements
        else None,
        fulltext_analyses=load_fulltext_analyses_json(args.announcement_analysis)
        if args.announcement_analysis
        else None,
        horizon=args.horizon,
        momentum_days=lookbacks[args.horizon],
        top_n=args.top,
    )
    report = render_daily_report(as_of, candidates)
    if args.output:
        Path(args.output).write_text(report, encoding="utf-8")
    else:
        print(report)


if __name__ == "__main__":
    main()

from __future__ import annotations

import argparse
import json
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Any, Callable

from .akshare_sync import sync_financial_indicators
from .io import (
    load_announcement_events_json,
    load_bars_csv,
    load_fundamentals_csv,
    load_fulltext_analyses_json,
    load_securities_csv,
    load_valuations_csv,
)
from .models import Candidate, DailyBar, FundamentalSnapshot
from .pipeline import select_candidates
from .reports import render_daily_report
from .research_funnel import UserConstraints, run_funnel, write_funnel_run


@dataclass(frozen=True, slots=True)
class ResearchRunResult:
    candidates: list[Candidate]
    report: str
    evidence: dict[str, Any]


FundamentalSyncer = Callable[..., dict[str, Any]]


def _lookback(horizon: str) -> int:
    return {"short": 5, "swing": 20, "medium": 60}[horizon]


def _date_key(as_of: date) -> str:
    return as_of.strftime("%Y%m%d")


def _split_cli_values(value: str | None) -> tuple[str, ...]:
    return tuple(item.strip() for item in (value or "").split(",") if item.strip())


def _load_bars_source(path: str | Path, as_of: date) -> list[DailyBar]:
    source = Path(path)
    if source.is_dir():
        cutoff = _date_key(as_of)
        bars: list[DailyBar] = []
        for item in sorted(source.glob("20??????.csv")):
            if item.stem <= cutoff:
                bars.extend(load_bars_csv(item))
        return sorted(bars, key=lambda bar: (bar.symbol, bar.trade_date))
    return load_bars_csv(source)


def _load_optional_inputs(
    *,
    valuations_path: str | Path | None = None,
    fundamentals_path: str | Path | None = None,
    announcements_path: str | Path | None = None,
    announcement_analysis_path: str | Path | None = None,
) -> dict[str, Any]:
    return {
        "valuations": load_valuations_csv(valuations_path) if valuations_path else None,
        "fundamentals": load_fundamentals_csv(fundamentals_path) if fundamentals_path else None,
        "announcement_events": load_announcement_events_json(announcements_path)
        if announcements_path
        else None,
        "fulltext_analyses": load_fulltext_analyses_json(announcement_analysis_path)
        if announcement_analysis_path
        else None,
    }


def run_research_report(
    project: Path,
    *,
    bars_path: str | Path,
    securities_path: str | Path,
    as_of: date,
    horizon: str = "swing",
    top_n: int = 10,
    valuations_path: str | Path | None = None,
    fundamentals_path: str | Path | None = None,
    announcements_path: str | Path | None = None,
    announcement_analysis_path: str | Path | None = None,
    auto_akshare_fundamentals: bool = False,
    akshare_start_year: str = "2024",
    akshare_candidate_pool: int | None = None,
    akshare_retries: int = 1,
    akshare_retry_delay: float = 2.0,
    fundamental_syncer: FundamentalSyncer = sync_financial_indicators,
) -> ResearchRunResult:
    securities = load_securities_csv(securities_path)
    bars = _load_bars_source(bars_path, as_of)
    optional = _load_optional_inputs(
        valuations_path=valuations_path,
        fundamentals_path=fundamentals_path,
        announcements_path=announcements_path,
        announcement_analysis_path=announcement_analysis_path,
    )
    fundamentals: list[FundamentalSnapshot] = list(optional["fundamentals"] or [])
    evidence: dict[str, Any] = {
        "auto_akshare_fundamentals": auto_akshare_fundamentals,
        "input_fundamentals_path": str(fundamentals_path) if fundamentals_path else None,
        "akshare_fundamentals": None,
    }

    if auto_akshare_fundamentals:
        pool_size = akshare_candidate_pool or max(top_n * 2, top_n)
        initial_candidates = select_candidates(
            securities,
            bars,
            as_of,
            valuations=optional["valuations"],
            fundamentals=fundamentals or None,
            announcement_events=optional["announcement_events"],
            fulltext_analyses=optional["fulltext_analyses"],
            horizon=horizon,
            momentum_days=_lookback(horizon),
            top_n=pool_size,
        )
        symbols = [candidate.symbol for candidate in initial_candidates]
        if symbols:
            sync_result = fundamental_syncer(
                project,
                symbols,
                as_of=as_of.isoformat(),
                start_year=akshare_start_year,
                retries=akshare_retries,
                retry_delay=akshare_retry_delay,
            )
        else:
            sync_result = {
                "symbols": 0,
                "fundamental_rows": 0,
                "failures": {},
                "processed_path": None,
                "reason": "候选池为空，跳过 AkShare 基本面补证",
            }
        evidence["akshare_fundamentals"] = sync_result
        processed_path = sync_result.get("processed_path")
        if processed_path:
            fundamentals.extend(load_fundamentals_csv(processed_path))

    candidates = select_candidates(
        securities,
        bars,
        as_of,
        valuations=optional["valuations"],
        fundamentals=fundamentals if fundamentals or fundamentals_path or auto_akshare_fundamentals else None,
        announcement_events=optional["announcement_events"],
        fulltext_analyses=optional["fulltext_analyses"],
        horizon=horizon,
        momentum_days=_lookback(horizon),
        top_n=top_n,
    )
    report = render_daily_report(as_of, candidates)
    report = _append_evidence_section(report, evidence)
    return ResearchRunResult(candidates=candidates, report=report, evidence=evidence)


def _append_evidence_section(report: str, evidence: dict[str, Any]) -> str:
    lines = [report.rstrip(), "", "## 数据补证", ""]
    if evidence.get("input_fundamentals_path"):
        lines.append(f"- 外部基本面文件：{evidence['input_fundamentals_path']}")
    if not evidence.get("auto_akshare_fundamentals"):
        lines.append("- AkShare 基本面自动补证：未启用")
    else:
        result = evidence.get("akshare_fundamentals") or {}
        failures = result.get("failures") or {}
        lines.append("- AkShare 基本面自动补证：已启用")
        lines.append(f"- 覆盖股票数：{result.get('symbols', 0)}")
        lines.append(f"- 基本面快照行数：{result.get('fundamental_rows', 0)}")
        lines.append(f"- 输出文件：{result.get('processed_path', '未生成')}")
        if failures:
            lines.append(f"- 失败股票数：{len(failures)}")
        else:
            lines.append("- 失败股票数：0")
        lines.append("- 使用边界：AkShare 财务指标以下载观察时间作为 publish_time，当前只做展示和风险提示")
    return "\n".join(lines) + "\n"


def main() -> None:
    parser = argparse.ArgumentParser(description="APlan 候选池研究报告，可按候选股自动补 AkShare 基本面")
    parser.add_argument("--root", default=".")
    parser.add_argument("--bars", required=True, help="日线 CSV")
    parser.add_argument("--securities", required=True, help="股票信息 CSV")
    parser.add_argument("--date", required=True, help="截止日期 YYYY-MM-DD")
    parser.add_argument("--horizon", choices=["short", "swing", "medium"], default="swing")
    parser.add_argument("--top", type=int, default=10)
    parser.add_argument("--valuations", help="估值 CSV；可选")
    parser.add_argument("--fundamentals", help="已有基本面 CSV；可选")
    parser.add_argument("--announcements", help="公告事件 JSON；可选")
    parser.add_argument("--announcement-analysis", help="公告全文分析 JSON；可选")
    parser.add_argument("--auto-akshare-fundamentals", action="store_true", help="只对候选池自动补 AkShare 基本面")
    parser.add_argument("--akshare-start-year", default="2024")
    parser.add_argument("--akshare-candidate-pool", type=int, help="补基本面的候选池大小；默认 top*2")
    parser.add_argument("--akshare-retries", type=int, default=1)
    parser.add_argument("--akshare-retry-delay", type=float, default=2.0)
    parser.add_argument("--output", help="报告输出路径；不传则打印")
    parser.add_argument("--evidence-output", help="数据补证 JSON 输出路径；可选")
    parser.add_argument("--funnel", action="store_true", help="运行策略驱动的可审计研究漏斗")
    parser.add_argument(
        "--strategy-profile",
        choices=["momentum", "event", "fundamental", "hybrid"],
        default="hybrid",
        help="研究漏斗的数据需求类型",
    )
    parser.add_argument("--industries", help="用户偏好的行业，逗号分隔；只约束范围，不修改评分")
    parser.add_argument("--prefixes", help="允许的股票代码前缀，逗号分隔")
    parser.add_argument("--include-symbols", help="用户指定的研究股票，逗号分隔")
    parser.add_argument("--exclude-symbols", help="用户排除的股票，逗号分隔")
    parser.add_argument("--exclude-star", action="store_true", help="研究漏斗排除科创板")
    parser.add_argument("--exclude-chinext", action="store_true", help="研究漏斗排除创业板")
    parser.add_argument(
        "--risk-preference",
        choices=["conservative", "balanced", "aggressive"],
        default="balanced",
        help="记录用户风险偏好；当前不直接修改评分",
    )
    parser.add_argument("--broad-pool", type=int, default=300, help="低成本粗筛池大小")
    parser.add_argument("--refined-pool", type=int, default=50, help="策略数据精筛池大小")
    parser.add_argument("--confirm-refinement", action="store_true", help="确认粗筛结果并继续精筛")
    parser.add_argument("--funnel-output", help="研究漏斗 JSON 输出路径；默认写入 runs/research_funnel")
    args = parser.parse_args()

    if args.funnel:
        securities = load_securities_csv(args.securities)
        bars = _load_bars_source(args.bars, date.fromisoformat(args.date))
        optional = _load_optional_inputs(
            valuations_path=args.valuations,
            fundamentals_path=args.fundamentals,
            announcements_path=args.announcements,
            announcement_analysis_path=args.announcement_analysis,
        )
        available_datasets = {
            name
            for name, path in (
                ("valuations", args.valuations),
                ("fundamentals", args.fundamentals),
                ("announcements", args.announcements),
                ("announcement_fulltext", args.announcement_analysis),
            )
            if path and Path(path).exists()
        }
        funnel_run = run_funnel(
            securities,
            bars,
            date.fromisoformat(args.date),
            strategy_profile=args.strategy_profile,
            constraints=UserConstraints(
                industries=_split_cli_values(args.industries),
                prefixes=_split_cli_values(args.prefixes),
                include_symbols=_split_cli_values(args.include_symbols),
                exclude_symbols=_split_cli_values(args.exclude_symbols),
                allow_star=not args.exclude_star,
                allow_chinext=not args.exclude_chinext,
                risk_preference=args.risk_preference,
            ),
            broad_pool_size=args.broad_pool,
            refined_pool_size=args.refined_pool,
            final_top_n=args.top,
            confirmed=args.confirm_refinement,
            valuations=optional["valuations"],
            fundamentals=optional["fundamentals"],
            announcement_events=optional["announcement_events"],
            fulltext_analyses=optional["fulltext_analyses"],
            horizon=args.horizon,
            momentum_days=_lookback(args.horizon),
            available_datasets=available_datasets,
        )
        funnel_path = write_funnel_run(
            Path(args.root).resolve(),
            funnel_run,
            Path(args.funnel_output) if args.funnel_output else None,
        )
        payload = funnel_run.to_dict()
        print(
            json.dumps(
                {
                    "run_id": funnel_run.run_id,
                    "status": funnel_run.status,
                    "output_path": str(funnel_path),
                    "stage_counts": {
                        stage["stage_id"]: stage["output_count"]
                        for stage in payload["stages"]
                    },
                    "missing_or_planned_data": [
                        requirement
                        for requirement in payload["data_requirements"]
                        if requirement["availability"] != "available"
                    ],
                    "final_candidates": len(funnel_run.final_candidates),
                    "execution_allowed": False,
                },
                ensure_ascii=False,
                indent=2,
            )
        )
        return

    result = run_research_report(
        Path(args.root).resolve(),
        bars_path=args.bars,
        securities_path=args.securities,
        as_of=date.fromisoformat(args.date),
        horizon=args.horizon,
        top_n=args.top,
        valuations_path=args.valuations,
        fundamentals_path=args.fundamentals,
        announcements_path=args.announcements,
        announcement_analysis_path=args.announcement_analysis,
        auto_akshare_fundamentals=args.auto_akshare_fundamentals,
        akshare_start_year=args.akshare_start_year,
        akshare_candidate_pool=args.akshare_candidate_pool,
        akshare_retries=args.akshare_retries,
        akshare_retry_delay=args.akshare_retry_delay,
    )
    if args.output:
        output_path = Path(args.output)
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_text(result.report, encoding="utf-8")
    else:
        print(result.report)
    if args.evidence_output:
        evidence_path = Path(args.evidence_output)
        evidence_path.parent.mkdir(parents=True, exist_ok=True)
        evidence_path.write_text(json.dumps(result.evidence, ensure_ascii=False, indent=2), encoding="utf-8")


if __name__ == "__main__":
    main()

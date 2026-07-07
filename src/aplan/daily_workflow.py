from __future__ import annotations

import argparse
import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from .audit import write_chained_audit
from .daily_report import write_daily_report
from .quality import validate_daily_snapshot
from .portfolio import PortfolioStore
from .paper_watch import update_watchlist_from_insights
from .report_insights import build_daily_insights
from .risk import RiskPolicy, plan_orders
from .strategy import StrategyContext, signal_set_sha256
from .strategy_cli import build_registry
from .sync import sync_market_day
from .tushare import TushareClient, TushareError


def previous_active_row_count(raw_root: Path, trade_date: str) -> int | None:
    candidates = sorted(
        path
        for path in raw_root.glob("20??????/daily.json")
        if path.parent.name < trade_date
    )
    for path in reversed(candidates):
        document = json.loads(path.read_text(encoding="utf-8"))
        count = int(document.get("row_count", 0))
        if count > 0:
            return count
    return None


def run_daily(
    project: Path,
    trade_date: str,
    *,
    download: bool = True,
) -> tuple[dict[str, Any], Path]:
    started_at = datetime.now(UTC).isoformat()
    audit: dict[str, Any] = {
        "schema_version": 1,
        "trade_date": trade_date,
        "started_at": started_at,
        "finished_at": None,
        "status": "running",
        "mode": "research_only",
        "strategy_approved": False,
        "stages": {},
    }
    raw_root = project / "data" / "raw" / "tushare"
    try:
        if download:
            counts = sync_market_day(
                TushareClient.from_env(project / ".env"),
                project,
                trade_date,
                ("daily",),
            )
            audit["stages"]["download"] = {"status": "passed", "counts": counts}
            if counts.get("daily", -1) < 0:
                raise RuntimeError("daily 下载失败")
            if counts.get("daily") == 0:
                audit["stages"]["quality"] = {
                    "status": "skipped",
                    "reason": "当日无行情，按休市日处理",
                }
                audit["stages"]["strategy"] = {
                    "status": "skipped",
                    "reason": "休市日不运行策略",
                }
                audit["stages"]["portfolio"] = {
                    "status": "skipped",
                    "orders": 0,
                    "execution_allowed": False,
                }
                audit["stages"]["paper_simulation"] = {
                    "status": "skipped",
                    "state_mutated": False,
                    "real_broker_connected": False,
                }
                audit["status"] = "skipped_market_closed"
                audit["finished_at"] = datetime.now(UTC).isoformat()
                path = write_chained_audit(project, trade_date, audit)
                return audit, path
        else:
            audit["stages"]["download"] = {"status": "skipped"}

        daily_path = raw_root / trade_date / "daily.json"
        if not daily_path.exists():
            raise RuntimeError(f"缺少 daily 快照：{daily_path}")
        report = validate_daily_snapshot(
            daily_path,
            trade_date,
            previous_row_count=previous_active_row_count(raw_root, trade_date),
        )
        audit["stages"]["quality"] = report.to_dict()
        if not report.passed:
            raise RuntimeError("数据质量检查未通过")

        registry = build_registry()
        strategy_runs = registry.run(
            StrategyContext(
                trade_date=trade_date,
                project_root=project,
                data_sha256=report.sha256,
                mode="research_only",
            )
        )
        all_signals = [signal for run in strategy_runs for signal in run.signals]
        audit["stages"]["strategy"] = {
            "status": "no_registered_strategy" if not strategy_runs else "completed",
            "registered_count": len(strategy_runs),
            "signal_count": len(all_signals),
            "signal_set_sha256": signal_set_sha256(all_signals),
            "execution_allowed": any(run.execution_allowed for run in strategy_runs),
            "runs": [
                {
                    "strategy_id": run.metadata.strategy_id,
                    "version": run.metadata.version,
                    "status": run.metadata.status.value,
                    "signal_count": len(run.signals),
                    "execution_allowed": run.execution_allowed,
                    "blocked_reason": run.blocked_reason,
                }
                for run in strategy_runs
            ],
        }
        portfolio = PortfolioStore(project).load("paper-main")
        if portfolio is None:
            audit["stages"]["portfolio"] = {
                "status": "not_initialized",
                "orders": 0,
                "execution_allowed": False,
            }
        else:
            prices = {
                str(row["ts_code"]).split(".", 1)[0]: float(row["close"])
                for row in json.loads(daily_path.read_text(encoding="utf-8")).get("rows", [])
            }
            decision = plan_orders(
                portfolio,
                all_signals,
                prices,
                policy=RiskPolicy(),
            )
            audit["stages"]["portfolio"] = {
                "status": "planned",
                **decision.to_dict(),
                "execution_allowed": False,
                "reason": "当前工作流为 research_only，不提交订单",
            }
        audit["stages"]["paper_simulation"] = {
            "status": "blocked",
            "reason": (
                "当前为 research_only，且没有通过隔离验证并获模拟盘审批的策略"
            ),
            "state_mutated": False,
            "real_broker_connected": False,
        }
        announcement_path = (
            project
            / "data"
            / "processed"
            / "announcements"
            / f"{trade_date}.json"
        )
        if announcement_path.exists():
            announcement_data = json.loads(announcement_path.read_text(encoding="utf-8"))
            events = announcement_data.get("events", [])
            scoped_events = [
                event
                for event in events
                if str(event.get("symbol", "")).startswith(
                    ("000", "001", "002", "003", "300", "301", "600", "601", "603", "605")
                )
            ]
            audit["stages"]["announcements"] = {
                "status": "available",
                "source": announcement_data.get("source"),
                "announcement_count": announcement_data.get("scope_announcement_count", 0),
                "event_count": len(scoped_events),
                "high_risk_count": sum(
                    event.get("risk_level") in {"high", "critical"}
                    for event in scoped_events
                ),
                "actionable_signals_created": 0,
                "reason": "标题分类仅供研究，全文Agent和事件回测尚未批准",
            }
        else:
            audit["stages"]["announcements"] = {
                "status": "not_synced",
                "announcement_count": 0,
                "event_count": 0,
                "actionable_signals_created": 0,
            }
        analysis_path = (
            project
            / "data"
            / "processed"
            / "announcement_analysis"
            / f"{trade_date}.json"
        )
        if analysis_path.exists():
            analysis_data = json.loads(analysis_path.read_text(encoding="utf-8"))
            audit["stages"]["announcement_fulltext"] = {
                "status": "available",
                "requested": analysis_data.get("requested", 0),
                "completed": analysis_data.get("completed", 0),
                "failed": analysis_data.get("failed", 0),
                "needs_ocr": analysis_data.get("needs_ocr", 0),
                "actionable_signals_created": 0,
            }
        else:
            audit["stages"]["announcement_fulltext"] = {
                "status": "not_processed",
                "completed": 0,
                "actionable_signals_created": 0,
            }
        audit["status"] = "passed_research_only"
    except (TushareError, RuntimeError, ValueError, json.JSONDecodeError) as exc:
        audit["status"] = "failed"
        audit["error"] = str(exc)
    try:
        audit["insights"] = build_daily_insights(project, trade_date)
        audit["stages"]["paper_watchlist"] = update_watchlist_from_insights(
            project,
            audit["insights"],
            portfolio_id="paper-main",
        )
    except (RuntimeError, ValueError, json.JSONDecodeError, OSError) as exc:
        audit["insights"] = {
            "status": "failed",
            "reason": str(exc),
        }
        audit["stages"]["paper_watchlist"] = {
            "status": "failed",
            "reason": str(exc),
        }
    audit["finished_at"] = datetime.now(UTC).isoformat()
    path = write_chained_audit(project, trade_date, audit)
    return audit, path


def main() -> None:
    parser = argparse.ArgumentParser(description="运行 APlan 每日研究工作流")
    parser.add_argument("--date", required=True, help="交易日 YYYYMMDD")
    parser.add_argument("--root", default=".")
    parser.add_argument("--no-download", action="store_true", help="仅验证已有本地快照")
    args = parser.parse_args()
    audit, path = run_daily(
        Path(args.root).resolve(),
        args.date,
        download=not args.no_download,
    )
    print(f"每日流程状态：{audit['status']}")
    print(f"审计记录：{path}")
    report_path = write_daily_report(Path(args.root).resolve(), audit, path)
    print(f"每日报告：{report_path}")
    if audit["status"] == "failed":
        raise SystemExit(1)


if __name__ == "__main__":
    main()

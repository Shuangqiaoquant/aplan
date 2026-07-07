from __future__ import annotations

import csv
import json
from datetime import date, datetime
from pathlib import Path

from .announcement_fulltext import FulltextAnalysis
from .announcements import AnnouncementEvent, EventImpact, RiskLevel
from .models import DailyBar, FundamentalSnapshot, Security, ValuationSnapshot


def _flag(value: str | None) -> bool:
    return str(value or "").strip().lower() in {"1", "true", "yes", "y"}


def load_bars_csv(path: str | Path) -> list[DailyBar]:
    """读取已复权日线。列名见 data/README.md。"""
    with Path(path).open(encoding="utf-8-sig", newline="") as handle:
        rows = csv.DictReader(handle)
        bars = [
            DailyBar(
                symbol=row["symbol"].strip(),
                trade_date=date.fromisoformat(row["trade_date"]),
                open=float(row["open"]),
                high=float(row["high"]),
                low=float(row["low"]),
                close=float(row["close"]),
                volume=float(row["volume"]),
                turnover=float(row["turnover"]),
                is_suspended=_flag(row.get("is_suspended")),
                is_limit_up=_flag(row.get("is_limit_up")),
                is_limit_down=_flag(row.get("is_limit_down")),
            )
            for row in rows
        ]
    return sorted(bars, key=lambda bar: (bar.symbol, bar.trade_date))


def load_securities_csv(path: str | Path) -> list[Security]:
    with Path(path).open(encoding="utf-8-sig", newline="") as handle:
        rows = csv.DictReader(handle)
        return [
            Security(
                symbol=row["symbol"].strip(),
                name=row["name"].strip(),
                list_date=date.fromisoformat(row["list_date"]),
                industry=(row.get("industry") or "未知").strip(),
                is_st=_flag(row.get("is_st")),
                is_delisting_risk=_flag(row.get("is_delisting_risk")),
            )
            for row in rows
        ]


def load_valuations_csv(path: str | Path) -> list[ValuationSnapshot]:
    with Path(path).open(encoding="utf-8-sig", newline="") as handle:
        rows = csv.DictReader(handle)
        return [
            ValuationSnapshot(
                symbol=row["symbol"].strip(),
                trade_date=date.fromisoformat(row["trade_date"]),
                pe=float(row.get("pe") or 0),
                pb=float(row.get("pb") or 0),
                total_mv=float(row.get("total_mv") or 0),
                circ_mv=float(row.get("circ_mv") or 0),
                turnover_rate=float(row.get("turnover_rate") or 0),
                volume_ratio=float(row.get("volume_ratio") or 0),
            )
            for row in rows
        ]


def _optional_float(row: dict[str, str], key: str) -> float | None:
    value = row.get(key)
    return None if value in (None, "") else float(value)


def load_fundamentals_csv(path: str | Path) -> list[FundamentalSnapshot]:
    with Path(path).open(encoding="utf-8-sig", newline="") as handle:
        rows = csv.DictReader(handle)
        return [
            FundamentalSnapshot(
                symbol=row["symbol"].strip(),
                period_end=date.fromisoformat(row["period_end"]),
                publish_time=datetime.fromisoformat(row["publish_time"]),
                source=(row.get("source") or "").strip(),
                source_hash=(row.get("source_hash") or "").strip(),
                revenue_growth=_optional_float(row, "revenue_growth"),
                net_profit_growth=_optional_float(row, "net_profit_growth"),
                roe=_optional_float(row, "roe"),
                operating_cashflow_to_profit=_optional_float(
                    row,
                    "operating_cashflow_to_profit",
                ),
                debt_to_assets=_optional_float(row, "debt_to_assets"),
            )
            for row in rows
        ]


def load_announcement_events_json(path: str | Path) -> list[AnnouncementEvent]:
    document = json.loads(Path(path).read_text(encoding="utf-8"))
    events = []
    for row in document.get("events", []):
        events.append(
            AnnouncementEvent(
                announcement_id=str(row["announcement_id"]),
                symbol=str(row["symbol"]),
                event_type=str(row["event_type"]),
                impact_hint=EventImpact(str(row["impact_hint"])),
                risk_level=RiskLevel(str(row["risk_level"])),
                confidence=float(row["confidence"]),
                summary=str(row["summary"]),
                evidence=tuple(str(item) for item in row.get("evidence", [])),
                source_url=str(row["source_url"]),
                published_at=str(row["published_at"]),
                requires_fulltext=bool(row["requires_fulltext"]),
                analyzer=str(row.get("analyzer", "unknown")),
            )
        )
    return events


def load_fulltext_analyses_json(path: str | Path) -> list[FulltextAnalysis]:
    document = json.loads(Path(path).read_text(encoding="utf-8"))
    analyses = []
    for row in document.get("analyses", []):
        analyses.append(
            FulltextAnalysis(
                announcement_id=str(row["announcement_id"]),
                symbol=str(row["symbol"]),
                event_type=str(row["event_type"]),
                conclusion=str(row["conclusion"]),
                confidence=float(row["confidence"]),
                facts=tuple(str(item) for item in row.get("facts", [])),
                positive_evidence=tuple(str(item) for item in row.get("positive_evidence", [])),
                negative_evidence=tuple(str(item) for item in row.get("negative_evidence", [])),
                uncertainties=tuple(str(item) for item in row.get("uncertainties", [])),
                source_url=str(row["source_url"]),
                pdf_sha256=str(row["pdf_sha256"]),
                analyzer=str(row.get("analyzer", "fulltext_rules_v1")),
                actionable_signal_created=bool(row.get("actionable_signal_created", False)),
            )
        )
    return analyses

from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any


@dataclass(frozen=True, slots=True)
class PaperWatchRecord:
    watch_id: str
    portfolio_id: str
    signal_date: str
    symbol: str
    score: float
    decision_band: str
    entry_style: str
    status: str
    action: str
    target_weight_min: float
    target_weight_max: float
    reasons: tuple[str, ...]
    risks: tuple[str, ...]
    created_at: str
    updated_at: str

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> "PaperWatchRecord":
        return cls(
            **{
                **value,
                "reasons": tuple(value.get("reasons", [])),
                "risks": tuple(value.get("risks", [])),
            }
        )


def watchlist_path(project: Path, portfolio_id: str = "paper-main") -> Path:
    return project / "state" / "paper" / portfolio_id / "watchlist.json"


def _watch_id(portfolio_id: str, signal_date: str, symbol: str, entry_style: str) -> str:
    raw = f"{portfolio_id}|{signal_date}|{symbol}|{entry_style}"
    return hashlib.sha256(raw.encode()).hexdigest()[:24]


def _default_status(action: str) -> str:
    if "等待次日确认" in action:
        return "pending_next_day_confirmation"
    if "阻断" in action:
        return "paper_watch_blocked"
    return "paper_watch"


def load_watchlist(project: Path, portfolio_id: str = "paper-main") -> list[PaperWatchRecord]:
    path = watchlist_path(project, portfolio_id)
    if not path.exists():
        return []
    document = json.loads(path.read_text(encoding="utf-8"))
    return [PaperWatchRecord.from_dict(item) for item in document.get("records", [])]


def save_watchlist(
    project: Path,
    records: list[PaperWatchRecord],
    portfolio_id: str = "paper-main",
) -> Path:
    path = watchlist_path(project, portfolio_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "schema_version": 1,
        "portfolio_id": portfolio_id,
        "saved_at": datetime.now(UTC).isoformat(),
        "records": [
            record.to_dict()
            for record in sorted(records, key=lambda item: (item.signal_date, item.symbol, item.watch_id))
        ],
    }
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    temporary.replace(path)
    return path


def update_watchlist_from_insights(
    project: Path,
    insights: dict[str, Any],
    *,
    portfolio_id: str = "paper-main",
    target_weight_min: float = 0.01,
    target_weight_max: float = 0.03,
) -> dict[str, Any]:
    candidate_top = insights.get("candidate_top") or {}
    if candidate_top.get("status") != "available":
        return {
            "status": "skipped",
            "reason": candidate_top.get("reason", "候选洞察不可用"),
            "added": 0,
            "updated": 0,
            "path": str(watchlist_path(project, portfolio_id)),
        }
    signal_date = str(candidate_top.get("as_of") or "")
    now = datetime.now(UTC).isoformat()
    existing = {record.watch_id: record for record in load_watchlist(project, portfolio_id)}
    added = 0
    updated = 0
    for item in candidate_top.get("items", []):
        symbol = str(item.get("symbol", ""))
        entry_style = str(item.get("entry_style", "unknown"))
        if not signal_date or len(symbol) != 6:
            continue
        watch_id = _watch_id(portfolio_id, signal_date, symbol, entry_style)
        old = existing.get(watch_id)
        record = PaperWatchRecord(
            watch_id=watch_id,
            portfolio_id=portfolio_id,
            signal_date=signal_date,
            symbol=symbol,
            score=float(item.get("score", 0)),
            decision_band=str(item.get("decision_band", "unknown")),
            entry_style=entry_style,
            status=_default_status(str(item.get("action", ""))),
            action=str(item.get("action", "观察")),
            target_weight_min=target_weight_min,
            target_weight_max=target_weight_max,
            reasons=tuple(str(value) for value in item.get("reasons", [])),
            risks=tuple(str(value) for value in item.get("risks", [])),
            created_at=old.created_at if old else now,
            updated_at=now,
        )
        existing[watch_id] = record
        if old:
            updated += 1
        else:
            added += 1
    path = save_watchlist(project, list(existing.values()), portfolio_id)
    return {
        "status": "updated",
        "added": added,
        "updated": updated,
        "total": len(existing),
        "path": str(path),
    }

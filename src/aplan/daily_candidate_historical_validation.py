from __future__ import annotations

import argparse
import bisect
import csv
import hashlib
import json
import math
import sqlite3
import tomllib
from collections import defaultdict, deque
from dataclasses import dataclass, replace
from datetime import UTC, date, datetime
from pathlib import Path
from statistics import mean, median, pstdev
from typing import Any, Iterable

from .announcement_event_validation import (
    MARKET_CODE,
    _benchmark_return,
    _build_bar_database,
    _canonical,
    _canonicalizer,
    _date,
    _industry_at,
    _load_benchmarks,
    _load_calendar,
    _load_security_state,
    _market_regimes,
    _max_drawdown,
    _number,
    _purged_development_cutoff,
    _return,
    _sha256,
    _truthy,
)
from .factors import percentile_ranks
from .fundamental_quality_validation import (
    _load_snapshot_timelines,
    _snapshot_at,
)
from .pipeline import (
    _fundamental_risk_flags,
    _valuation_score,
)
from .trend_monitor_validation import (
    MINIMUM_AVERAGE_TURNOVER20,
    MINIMUM_LISTING_DAYS,
    TechnicalCandidate,
    _days_listed,
    _finalize_oos,
    _gate,
    _listing_dates,
    _matched_controls,
    _nominate_horizon,
)


AS_OF_TRADE_DATE = "20251231"
DEVELOPMENT_END = "20241231"
PROTOCOL_SHA256 = (
    "77e60006c4aaebc58bd8bf8b6b03cce83b966932ac7228df27aaf9869fba0f45"
)
HORIZONS = (1, 5, 10, 20, 40, 60)
OFFSETS = (0, 1, 2, 3, 4)
STEP_DAYS = 5
CANDIDATE_COUNT = 5
VALUATION_MINIMUM_COVERAGE = 0.90
VARIANTS = (
    "price_score_only",
    "full_current_model",
    "no_valuation",
    "no_market_regime_cap",
    "no_industry_cap",
    "no_fundamental_cap",
    "no_announcement_cap",
)
VALUATION_DEPENDENT = frozenset(
    {
        "full_current_model",
        "no_market_regime_cap",
        "no_industry_cap",
        "no_fundamental_cap",
        "no_announcement_cap",
    }
)
CAP_FLAGS = {
    "price_score_only": frozenset(),
    "full_current_model": frozenset(
        {"market", "industry", "fundamental", "announcement"}
    ),
    "no_valuation": frozenset(
        {"market", "industry", "fundamental", "announcement"}
    ),
    "no_market_regime_cap": frozenset(
        {"industry", "fundamental", "announcement"}
    ),
    "no_industry_cap": frozenset(
        {"market", "fundamental", "announcement"}
    ),
    "no_fundamental_cap": frozenset(
        {"market", "industry", "announcement"}
    ),
    "no_announcement_cap": frozenset(
        {"market", "industry", "fundamental"}
    ),
}


@dataclass(frozen=True, slots=True)
class Candidate:
    symbol: str
    signal_date: str
    offset: int
    score: float
    pre20_return: float
    average_turnover20: float
    industry_code: str
    valuation_joined: bool
    fundamental_joined: bool
    announcement_joined: bool


@dataclass(frozen=True, slots=True)
class Observation:
    variant: str
    period: str
    horizon: int
    signal_date: str
    entry_date: str
    symbol: str
    offset: int
    industry_code: str
    gross_return: float
    net_return: float
    market_excess: float | None
    industry_excess: float | None
    matched_excess: float | None
    market_regime: str
    price_score_excess: float | None = None


def _protocol(project: Path) -> dict[str, Any]:
    path = project / "config" / "daily_candidate_historical_validation.toml"
    if not path.exists():
        return {
            "path": None,
            "sha256": PROTOCOL_SHA256,
            "document": None,
            "note": (
                "Cloud checkout lacked the frozen TOML; embedded frozen "
                "parameters matching the registered hash were used."
            ),
        }
    digest = _sha256(path)
    if digest != PROTOCOL_SHA256:
        raise ValueError(
            "daily_candidate 冻结规范哈希不匹配："
            f"expected={PROTOCOL_SHA256} actual={digest}"
        )
    document = tomllib.loads(path.read_text(encoding="utf-8"))
    if document.get("final_holdout_opened") is not False:
        raise ValueError("冻结规范必须保持 final_holdout_opened=false")
    time_design = document.get("time_design", {})
    if _date(time_design.get("research_as_of_trade_date")) != AS_OF_TRADE_DATE:
        raise ValueError("冻结规范 as_of_trade_date 已变化")
    if int(time_design.get("purge_trading_days", 0)) != 60:
        raise ValueError("冻结规范必须保留 60 个交易日 purge")
    if set(document.get("variants", {})) != set(VARIANTS):
        raise ValueError("冻结规范的验证变体已变化")
    return {"path": str(path), "sha256": digest, "document": document}


def _factor_snapshot(
    history: Iterable[tuple[float, float]],
    close: float,
    turnover: float,
) -> tuple[float, float, float, float, bool] | None:
    previous = list(history)
    if len(previous) < 20:
        return None
    closes = [item[0] for item in previous[-20:]] + [close]
    turnovers = [item[1] for item in previous[-19:]] + [turnover]
    if min(closes) <= 0 or len(turnovers) != 20:
        return None
    momentum20 = close / previous[-20][0] - 1
    returns = [
        closes[index] / closes[index - 1] - 1
        for index in range(1, len(closes))
    ]
    volatility20 = pstdev(returns) * math.sqrt(252)
    baseline = mean(turnovers[:10])
    turnover_trend20 = (
        mean(turnovers[10:]) / baseline - 1 if baseline > 0 else 0.0
    )
    return (
        momentum20,
        volatility20,
        turnover_trend20,
        mean(turnovers),
        close >= mean(closes[-20:]),
    )


def _score_parts(
    snapshots: dict[str, tuple[float, float, float, float, bool]],
) -> dict[str, tuple[float, float, float, float]]:
    common = set(snapshots)
    momentum = percentile_ranks(
        {symbol: snapshots[symbol][0] for symbol in common}
    )
    volatility = percentile_ranks(
        {symbol: snapshots[symbol][1] for symbol in common},
        higher_is_better=False,
    )
    turnover = percentile_ranks(
        {symbol: snapshots[symbol][2] for symbol in common}
    )
    return {
        symbol: (
            30.0 * momentum[symbol],
            20.0 * volatility[symbol],
            15.0 * turnover[symbol],
            snapshots[symbol][3],
        )
        for symbol in common
    }


def _model_score(
    price_parts: tuple[float, float, float, float],
    *,
    variant: str,
    valuation_score: float | None,
    market_cap: float | None,
    industry_cap: float | None,
    fundamental_cap: float | None,
    announcement_cap: float | None,
) -> float | None:
    score = sum(price_parts[:3]) + 5.0 + 3.0 + 7.0
    if variant == "price_score_only":
        return score
    if variant in VALUATION_DEPENDENT:
        if valuation_score is None:
            return None
        score += valuation_score - 3.0
    caps = CAP_FLAGS[variant]
    for name, value in (
        ("market", market_cap),
        ("industry", industry_cap),
        ("fundamental", fundamental_cap),
        ("announcement", announcement_cap),
    ):
        if name in caps and value is not None:
            score = min(score, value)
    return score


def _timeline_at(
    timeline: tuple[list[str], list[Any]] | None,
    day: str,
) -> Any | None:
    if timeline is None:
        return None
    dates, values = timeline
    index = bisect.bisect_right(dates, day) - 1
    return values[index] if index >= 0 else None


def _exact_timeline_at(
    timeline: tuple[list[str], list[Any]] | None,
    day: str,
) -> Any | None:
    if timeline is None:
        return None
    dates, values = timeline
    index = bisect.bisect_left(dates, day)
    return values[index] if index < len(dates) and dates[index] == day else None


def _load_valuations(
    project: Path,
    aliases: dict[str, str],
) -> tuple[dict[str, tuple[list[str], list[tuple[float, float]]]], dict[str, Any]]:
    root = (
        project / "data" / "processed" / "yinhe_derived_valuations"
    )
    manifest_path = root / "manifest.json"
    if not manifest_path.exists():
        return {}, {
            "status": "data_unavailable",
            "reason": "missing_yinhe_derived_valuation_manifest",
            "path": str(manifest_path),
            "minimum_required_join_coverage": VALUATION_MINIMUM_COVERAGE,
        }
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    if (
        manifest.get("status") != "validated"
        or manifest.get("protocol_sha256")
        != "8c615cde9046440deaee5c2e7e54b87933a59d9355f2208f6e2d3ba1f9486b0b"
        or manifest.get("coverage_end", "") > AS_OF_TRADE_DATE
        or manifest.get("2026_rows") != 0
        or manifest.get("final_holdout_opened") is not False
    ):
        return {}, {
            "status": "data_unavailable",
            "reason": "yinhe_derived_valuation_manifest_not_accepted",
            "path": str(manifest_path),
            "manifest_sha256": _sha256(manifest_path),
            "minimum_required_join_coverage": VALUATION_MINIMUM_COVERAGE,
        }
    incoming: dict[str, list[tuple[str, float, float]]] = defaultdict(list)
    source_files: list[Path] = []
    rows_visible = 0
    rows_after_cutoff = 0
    invalid_rows = 0
    for path in sorted(root.glob("20??????.csv")):
        if path.stem > AS_OF_TRADE_DATE:
            rows_after_cutoff += 1
            continue
        source_files.append(path)
        with path.open(encoding="utf-8-sig", newline="") as handle:
            for row in csv.DictReader(handle):
                day = _date(row.get("trade_date") or path.stem)
                if day > AS_OF_TRADE_DATE:
                    raise ValueError(
                        f"估值文件 {path.name} 含研究截止日之后的数据"
                    )
                symbol = _canonical(str(row.get("symbol") or ""), aliases)
                pe, pb = _number(row.get("pe_ttm")), _number(row.get("pb"))
                if len(symbol) != 6 or not day or pe is None or pb is None:
                    invalid_rows += 1
                    continue
                incoming[symbol].append((day, float(pe), float(pb)))
                rows_visible += 1
    timelines: dict[str, tuple[list[str], list[tuple[float, float]]]] = {}
    first_date = ""
    last_date = ""
    for symbol, items in incoming.items():
        ordered = sorted(items)
        timelines[symbol] = (
            [item[0] for item in ordered],
            [(item[1], item[2]) for item in ordered],
        )
        first_date = min(first_date or ordered[0][0], ordered[0][0])
        last_date = max(last_date, ordered[-1][0])
    return timelines, {
        "status": "accepted_source_loaded",
        "source": "yinhe_pit_derived_valuation_v1",
        "manifest_path": str(manifest_path),
        "manifest_sha256": _sha256(manifest_path),
        "source_protocol_sha256": manifest.get("protocol_sha256"),
        "daily_files_sha256": manifest.get("daily_files_sha256"),
        "final_holdout_opened": False,
        "source_files": len(source_files),
        "source_files_sha256": hashlib.sha256(
            "".join(_sha256(path) for path in source_files).encode()
        ).hexdigest(),
        "rows_visible_through_20251231": rows_visible,
        "source_files_after_cutoff_not_opened": rows_after_cutoff,
        "invalid_rows": invalid_rows,
        "symbols": len(timelines),
        "first_date": first_date or None,
        "last_date": last_date or None,
        "minimum_required_join_coverage": VALUATION_MINIMUM_COVERAGE,
    }


def _load_fundamentals(
    project: Path,
    aliases: dict[str, str],
) -> tuple[dict[str, tuple[list[str], list[Any]]], dict[str, Any]]:
    root = project / "data" / "processed" / "yinhe_fundamentals"
    path = root / "fundamental_snapshots.csv"
    if not path.exists():
        return {}, {"status": "data_unavailable", "path": str(path)}
    timelines, audit = _load_snapshot_timelines(path, aliases)
    return timelines, {
        "status": "available",
        "path": str(path),
        "sha256": _sha256(path),
        **audit,
    }


def _announcement_cap(impact: str, risk: str) -> float | None:
    if risk == "critical":
        return 49.0
    if risk == "high":
        return 64.0
    if impact in {"negative", "mixed"}:
        return 74.0
    return None


def _load_announcements(
    project: Path,
    aliases: dict[str, str],
) -> tuple[dict[str, tuple[list[str], list[float]]], dict[str, Any]]:
    root = project / "data" / "processed" / "announcements"
    path = root / "event_index.csv"
    if not path.exists():
        return {}, {"status": "data_unavailable", "path": str(path)}
    incoming: dict[str, list[tuple[str, float]]] = defaultdict(list)
    rows = 0
    excluded_right_edge = 0
    with path.open(encoding="utf-8-sig", newline="") as handle:
        for row in csv.DictReader(handle):
            usable = _date(row.get("usable_from_trade_date"))
            if not usable:
                excluded_right_edge += 1
                continue
            if usable > AS_OF_TRADE_DATE:
                continue
            symbol = _canonical(str(row.get("symbol") or ""), aliases)
            cap = _announcement_cap(
                str(row.get("impact_hint") or "").lower(),
                str(row.get("risk_level") or "").lower(),
            )
            if len(symbol) == 6 and cap is not None:
                incoming[symbol].append((usable, cap))
                rows += 1
    timelines: dict[str, tuple[list[str], list[float]]] = {}
    for symbol, items in incoming.items():
        running = 100.0
        dates: list[str] = []
        values: list[float] = []
        for day, cap in sorted(items):
            running = min(running, cap)
            dates.append(day)
            values.append(running)
        timelines[symbol] = (dates, values)
    return timelines, {
        "status": "available",
        "path": str(path),
        "sha256": _sha256(path),
        "risk_rows_visible": rows,
        "right_edge_rows_excluded": excluded_right_edge,
        "symbols": len(timelines),
    }


def _market_and_industry_caps(
    snapshots: dict[str, tuple[float, float, float, float, bool]],
    industry_by_symbol: dict[str, str],
) -> tuple[float | None, dict[str, float | None]]:
    if len(snapshots) < 50:
        market_cap = None
    else:
        momentums = [item[0] for item in snapshots.values()]
        breadth = sum(item[4] for item in snapshots.values()) / len(snapshots)
        median_momentum = median(momentums)
        market_cap = (
            64.0
            if breadth < 0.25 and median_momentum < -0.08
            else 74.0
            if breadth < 0.35 and median_momentum < -0.03
            else None
        )
    grouped: dict[str, list[float]] = defaultdict(list)
    for symbol, item in snapshots.items():
        industry = industry_by_symbol.get(symbol, "")
        if industry:
            grouped[industry].append(item[0])
    if len(grouped) < 3:
        return market_cap, {}
    ranks = percentile_ranks(
        {industry: median(values) for industry, values in grouped.items()}
    )
    return market_cap, {
        industry: 74.0 if rank <= 0.30 else None
        for industry, rank in ranks.items()
    }


def _scan_candidates(
    project: Path,
    dates: list[str],
    aliases: dict[str, str],
    security_database: Path,
    listing_dates: dict[str, str],
    pit_industries: dict[str, list[tuple[str, str, str]]],
    valuations: dict[str, tuple[list[str], list[tuple[float, float]]]],
    fundamentals: dict[str, tuple[list[str], list[Any]]],
    announcements: dict[str, tuple[list[str], list[float]]],
) -> tuple[dict[str, dict[str, list[Candidate]]], dict[str, Any]]:
    paths = {
        path.stem: path
        for path in (
            project / "data" / "processed" / "yinhe_daily_qfq"
        ).glob("20??????.csv")
        if path.stem <= AS_OF_TRADE_DATE
    }
    histories: dict[str, deque[tuple[float, float]]] = defaultdict(
        lambda: deque(maxlen=20)
    )
    selections: dict[str, dict[str, list[Candidate]]] = {}
    audit: dict[str, int] = defaultdict(int)
    statuses = sqlite3.connect(security_database)
    statuses.row_factory = sqlite3.Row
    try:
        for date_index, signal_date in enumerate(dates):
            path = paths.get(signal_date)
            if path is None:
                continue
            current: dict[str, tuple[float, float, float, float, bool]] = {}
            tradability: dict[str, tuple[int, int, int]] = {}
            with path.open(encoding="utf-8-sig", newline="") as handle:
                for row in csv.DictReader(handle):
                    symbol = _canonical(str(row.get("symbol") or ""), aliases)
                    close, turnover = _number(row.get("close")), _number(
                        row.get("turnover")
                    )
                    if (
                        len(symbol) != 6
                        or close is None
                        or turnover is None
                        or close <= 0
                    ):
                        continue
                    snapshot = _factor_snapshot(
                        histories[symbol], float(close), float(turnover)
                    )
                    if snapshot is not None:
                        current[symbol] = snapshot
                        tradability[symbol] = (
                            _truthy(row.get("is_suspended")),
                            _truthy(row.get("is_limit_up")),
                            _truthy(row.get("is_limit_down")),
                        )
                    histories[symbol].append((float(close), float(turnover)))
            price_parts = _score_parts(current)
            eligible_parts: dict[str, tuple[float, float, float, float]] = {}
            industries: dict[str, str] = {}
            for symbol, parts in price_parts.items():
                audit["factor_complete"] += 1
                suspended, _, _ = tradability[symbol]
                if suspended:
                    audit["excluded_signal_suspended"] += 1
                    continue
                state = statuses.execute(
                    "SELECT is_st,is_suspended FROM daily_status "
                    "WHERE trade_date=? AND symbol=?",
                    (signal_date, symbol),
                ).fetchone()
                if not state:
                    audit["missing_signal_security_state"] += 1
                    continue
                audit["signal_security_state_joined"] += 1
                if state["is_st"] or state["is_suspended"]:
                    audit["excluded_st_or_suspended"] += 1
                    continue
                listed = listing_dates.get(symbol)
                if (
                    not listed
                    or _days_listed(listed, signal_date)
                    < MINIMUM_LISTING_DAYS
                ):
                    audit["excluded_listing_age"] += 1
                    continue
                if parts[3] < MINIMUM_AVERAGE_TURNOVER20:
                    audit["below_turnover_threshold"] += 1
                    continue
                industry = _industry_at(symbol, signal_date, pit_industries)
                if not industry:
                    audit["missing_pit_industry"] += 1
                else:
                    audit["industry_joined"] += 1
                industries[symbol] = industry
                eligible_parts[symbol] = parts
            audit["eligible_signal_grains"] += len(eligible_parts)
            market_cap, industry_caps = _market_and_industry_caps(
                {
                    symbol: current[symbol]
                    for symbol in eligible_parts
                },
                industries,
            )
            auxiliary: dict[
                str,
                tuple[
                    float | None,
                    float | None,
                    float | None,
                    bool,
                    bool,
                ],
            ] = {}
            for symbol in eligible_parts:
                valuation = _exact_timeline_at(
                    valuations.get(symbol), signal_date
                )
                valuation_score = None
                if valuation is not None:
                    audit["valuation_joined"] += 1
                    pe, pb = valuation
                    valuation_score = _valuation_score(
                        type("Valuation", (), {"pe": pe, "pb": pb})()
                    )
                else:
                    audit["valuation_missing"] += 1
                fundamental = _snapshot_at(
                    fundamentals.get(symbol), signal_date
                )
                if fundamental is not None:
                    audit["fundamental_joined"] += 1
                else:
                    audit["fundamental_missing"] += 1
                fundamental_cap = None
                if fundamental is not None:
                    risks = len(_fundamental_risk_flags(fundamental))
                    fundamental_cap = (
                        64.0 if risks >= 2 else 74.0 if risks else None
                    )
                announcement_cap = _timeline_at(
                    announcements.get(symbol), signal_date
                )
                if announcement_cap is not None:
                    audit["announcement_joined"] += 1
                else:
                    audit["announcement_missing"] += 1
                auxiliary[symbol] = (
                    valuation_score,
                    fundamental_cap,
                    announcement_cap,
                    fundamental is not None,
                    announcement_cap is not None,
                )
            by_variant: dict[str, list[Candidate]] = {}
            for variant in VARIANTS:
                ranked: list[Candidate] = []
                for symbol, parts in eligible_parts.items():
                    (
                        valuation_score,
                        fundamental_cap,
                        announcement_cap,
                        fundamental_joined,
                        announcement_joined,
                    ) = auxiliary[symbol]
                    score = _model_score(
                        parts,
                        variant=variant,
                        valuation_score=valuation_score,
                        market_cap=market_cap,
                        industry_cap=industry_caps.get(industries[symbol]),
                        fundamental_cap=fundamental_cap,
                        announcement_cap=announcement_cap,
                    )
                    if score is None:
                        continue
                    ranked.append(
                        Candidate(
                            symbol=symbol,
                            signal_date=signal_date,
                            offset=date_index % STEP_DAYS,
                            score=score,
                            pre20_return=current[symbol][0],
                            average_turnover20=parts[3],
                            industry_code=industries[symbol],
                            valuation_joined=valuation_score is not None,
                            fundamental_joined=fundamental_joined,
                            announcement_joined=announcement_joined,
                        )
                    )
                by_variant[variant] = sorted(
                    ranked, key=lambda item: (-item.score, item.symbol)
                )[:CANDIDATE_COUNT]
            selections[signal_date] = by_variant
            if (date_index + 1) % 100 == 0 or date_index + 1 == len(dates):
                print(
                    f"daily_candidate 历史重放：{date_index + 1}/{len(dates)}",
                    flush=True,
                )
    finally:
        statuses.close()
    eligible = sum(
        len(values.get("price_score_only", []))
        for values in selections.values()
    )
    valuation_total = audit["valuation_joined"] + audit["valuation_missing"]
    audit["valuation_join_coverage"] = (
        audit["valuation_joined"] / valuation_total if valuation_total else 0.0
    )
    audit["valuation_audit_sufficient"] = (
        audit["valuation_join_coverage"] >= VALUATION_MINIMUM_COVERAGE
    )
    for label, joined_key, missing_key in (
        (
            "signal_security_state",
            "signal_security_state_joined",
            "missing_signal_security_state",
        ),
        ("industry", "industry_joined", "missing_pit_industry"),
        ("fundamental", "fundamental_joined", "fundamental_missing"),
    ):
        joined = audit[joined_key]
        missing = audit[missing_key]
        audit[f"{label}_join_coverage"] = (
            joined / (joined + missing) if joined + missing else None
        )
    announcement_total = (
        audit["announcement_joined"] + audit["announcement_missing"]
    )
    audit["announcement_risk_event_presence_rate"] = (
        audit["announcement_joined"] / announcement_total
        if announcement_total
        else None
    )
    audit["price_score_signals"] = eligible
    return selections, dict(audit)


def _evaluate(
    bar_database: Path,
    security_database: Path,
    dates: list[str],
    selections: dict[str, dict[str, list[Candidate]]],
    market: dict[tuple[str, str], tuple[float, float]],
    industry_daily: dict[tuple[str, str], tuple[float, float]],
    industry_by_symbol: dict[str, list[tuple[str, str, str]]],
    industry_members: dict[str, list[str]],
    listing_dates: dict[str, str],
) -> tuple[list[Observation], dict[str, Any]]:
    bars = sqlite3.connect(bar_database)
    bars.row_factory = sqlite3.Row
    statuses = sqlite3.connect(security_database)
    statuses.row_factory = sqlite3.Row
    date_index = {day: index for index, day in enumerate(dates)}
    regimes = _market_regimes(market, dates)
    observations: list[Observation] = []
    audit: dict[str, int] = defaultdict(int)
    try:
        for signal_date, variants in selections.items():
            signal_index = date_index[signal_date]
            if signal_index + 1 >= len(dates):
                continue
            entry_date = dates[signal_index + 1]
            exits = {
                horizon: dates[signal_index + horizon]
                for horizon in HORIZONS
                if signal_index + horizon < len(dates)
            }
            all_candidates = {
                item.symbol for items in variants.values() for item in items
            }
            for variant, candidates in variants.items():
                audit[f"{variant}_signals"] += len(candidates)
                for candidate in candidates:
                    state = statuses.execute(
                        "SELECT is_st,is_suspended FROM daily_status "
                        "WHERE trade_date=? AND symbol=?",
                        (entry_date, candidate.symbol),
                    ).fetchone()
                    entry = bars.execute(
                        "SELECT * FROM bars WHERE trade_date=? AND symbol=?",
                        (entry_date, candidate.symbol),
                    ).fetchone()
                    if not state:
                        audit["missing_entry_security_state"] += 1
                        continue
                    audit["entry_security_state_joined"] += 1
                    if (
                        state["is_st"]
                        or state["is_suspended"]
                        or not entry
                        or entry["is_suspended"]
                    ):
                        audit["untradeable_suspended_or_st"] += 1
                        continue
                    if entry["is_limit_up"] and entry["high"] == entry["low"]:
                        audit["untradeable_one_price_limit_up"] += 1
                        continue
                    proxy = TechnicalCandidate(
                        symbol=candidate.symbol,
                        signal_date=signal_date,
                        offset=candidate.offset,
                        signals=(),
                        score=candidate.score,
                        pre20_return=candidate.pre20_return,
                        average_turnover20=candidate.average_turnover20,
                        industry_code=candidate.industry_code,
                    )
                    controls = _matched_controls(
                        bars,
                        statuses,
                        proxy,
                        entry_date,
                        exits,
                        industry_members,
                        industry_by_symbol,
                        all_candidates,
                        listing_dates,
                    )
                    for horizon, exit_date in exits.items():
                        exit_row = bars.execute(
                            "SELECT close FROM bars WHERE trade_date=? AND symbol=?",
                            (exit_date, candidate.symbol),
                        ).fetchone()
                        if not exit_row:
                            continue
                        gross, net = _return(
                            float(entry["open"]), float(exit_row["close"])
                        )
                        market_return = _benchmark_return(
                            market, MARKET_CODE, entry_date, exit_date
                        )
                        industry_return = (
                            _benchmark_return(
                                industry_daily,
                                candidate.industry_code,
                                entry_date,
                                exit_date,
                            )
                            if candidate.industry_code
                            else None
                        )
                        matched = controls.get(horizon)
                        observations.append(
                            Observation(
                                variant=variant,
                                period=(
                                    "development"
                                    if signal_date <= DEVELOPMENT_END
                                    else "rolling_oos"
                                ),
                                horizon=horizon,
                                signal_date=signal_date,
                                entry_date=entry_date,
                                symbol=candidate.symbol,
                                offset=candidate.offset,
                                industry_code=candidate.industry_code,
                                gross_return=gross,
                                net_return=net,
                                market_excess=(
                                    (1 + net) / (1 + market_return) - 1
                                    if market_return is not None
                                    and market_return > -1
                                    else None
                                ),
                                industry_excess=(
                                    (1 + net) / (1 + industry_return) - 1
                                    if industry_return is not None
                                    and industry_return > -1
                                    else None
                                ),
                                matched_excess=(
                                    (1 + net) / (1 + matched) - 1
                                    if matched is not None and matched > -1
                                    else None
                                ),
                                market_regime=regimes.get(
                                    signal_date, "insufficient"
                                ),
                            )
                        )
    finally:
        bars.close()
        statuses.close()
    baselines: dict[tuple[str, int, int], float] = {}
    grouped: dict[tuple[str, int, int], list[float]] = defaultdict(list)
    for row in observations:
        if row.variant == "price_score_only":
            grouped[(row.signal_date, row.offset, row.horizon)].append(
                row.net_return
            )
    for key, values in grouped.items():
        baselines[key] = mean(values)
    observations = [
        replace(
            row,
            price_score_excess=(
                (1 + row.net_return)
                / (
                    1
                    + baselines[(row.signal_date, row.offset, row.horizon)]
                )
                - 1
                if (
                    row.variant != "price_score_only"
                    and (row.signal_date, row.offset, row.horizon) in baselines
                    and baselines[(row.signal_date, row.offset, row.horizon)] > -1
                )
                else 0.0
                if row.variant == "price_score_only"
                else None
            ),
        )
        for row in observations
    ]
    total = (
        audit["entry_security_state_joined"]
        + audit["missing_entry_security_state"]
    )
    audit["entry_security_state_join_coverage"] = (
        audit["entry_security_state_joined"] / total if total else None
    )
    signals = sum(audit[f"{variant}_signals"] for variant in VARIANTS)
    audit["untradeable_rate"] = (
        (
            audit["untradeable_suspended_or_st"]
            + audit["untradeable_one_price_limit_up"]
        )
        / signals
        if signals
        else None
    )
    return observations, dict(audit)


def _comparison(
    rows: list[Observation],
    variant: str,
    baseline_variant: str,
    horizon: int,
    period: str,
    *,
    development_cutoff: str,
    unavailable_reason: str | None = None,
) -> dict[str, Any]:
    if unavailable_reason:
        return {
            "status": "data_unavailable",
            "reason": unavailable_reason,
        }
    selected = [
        row
        for row in rows
        if row.variant == variant
        and row.horizon == horizon
        and row.period == period
        and (
            period != "development"
            or row.signal_date <= development_cutoff
        )
    ]
    baseline_rows = [
        row
        for row in rows
        if row.variant == baseline_variant
        and row.horizon == horizon
        and row.period == period
        and (
            period != "development"
            or row.signal_date <= development_cutoff
        )
    ]
    baseline_by_cohort: dict[tuple[str, int], list[float]] = defaultdict(
        list
    )
    for row in baseline_rows:
        baseline_by_cohort[(row.signal_date, row.offset)].append(
            row.net_return
        )
    baseline_mean = {
        key: mean(values) for key, values in baseline_by_cohort.items()
    }
    increments: list[tuple[str, float]] = []
    for row in selected:
        baseline = baseline_mean.get((row.signal_date, row.offset))
        if baseline is None or baseline <= -1:
            continue
        increments.append(
            (
                row.signal_date,
                (1 + row.net_return) / (1 + baseline) - 1,
            )
        )
    variant_cohorts = {
        (row.signal_date, row.offset, row.symbol) for row in selected
    }
    baseline_cohorts = {
        (row.signal_date, row.offset, row.symbol)
        for row in baseline_rows
    }
    union = variant_cohorts | baseline_cohorts
    values = [value for _, value in increments]
    return {
        "status": "available",
        "variant": variant,
        "baseline_variant": baseline_variant,
        "observations": len(values),
        "median_net_increment": median(values) if values else None,
        "mean_net_increment": mean(values) if values else None,
        "opportunity_cost_changed_selection_rate": (
            1 - len(variant_cohorts & baseline_cohorts) / len(union)
            if union
            else None
        ),
        "tail_loss_rate": (
            sum(value <= -0.10 for value in values) / len(values)
            if values
            else None
        ),
        "max_drawdown": (
            _max_drawdown(increments)
            if values
            else None
        ),
        "comparison_coverage": (
            len(values) / len(selected) if selected else None
        ),
    }


def _report_markdown(summary: dict[str, Any]) -> str:
    def percent(value: Any) -> str:
        return "NA" if value is None else f"{float(value):.2%}"

    lines = [
        "# daily_candidate_v0_1 历史重放冻结验证",
        "",
        f"- 状态：`{summary['status']}`",
        f"- 研究截止：`{summary['as_of_trade_date']}`",
        f"- 有效训练截止：`{summary['effective_development_signal_end']}`",
        f"- 冻结规范：`{summary['protocol']['sha256']}`",
        f"- 参数哈希：`{summary['model']['parameters_sha256']}`",
        f"- 实现哈希：`{summary['model']['implementation_sha256']}`",
        f"- pipeline/factors：`{summary['model']['pipeline_sha256']}` / `{summary['model']['factors_sha256']}`",
        "- 2026 最终留出集：`sealed`",
        "",
        "## 估值审计",
        "",
        f"- 状态：`{summary['data']['valuation_audit']['status']}`",
        f"- PIT join coverage：{summary['data']['valuation_audit']['join_coverage']:.2%}",
        f"- 要求：{summary['data']['valuation_audit']['minimum_required_join_coverage']:.0%}",
        "",
        "## 门控",
        "",
        "| 变体 | 训练提名 | 2025结论 | 数据状态 |",
        "|---|---:|---|---|",
    ]
    for variant, result in summary["variants"].items():
        lines.append(
            f"| {variant} | "
            f"{result['training_nomination']['horizon'] or '-'} | "
            f"{result['final_gate_decision']} | "
            f"{result['data_status']} |"
        )
    lines.extend(
        [
            "",
            "## 2025 滚动样本外",
            "",
            "| 变体 | 周期 | 门控 | 样本 | 中位净收益 | 市场/行业/匹配超额 | 相对纯价格增量 | 相对完整模型 |",
            "|---|---:|---|---:|---:|---|---:|---|",
        ]
    )
    for variant, result in summary["variants"].items():
        for horizon, values in result["horizons"].items():
            oos = values["rolling_oos"]
            metrics = oos["metrics"]
            price = oos["increment_vs_price_score_only"]
            full = oos["increment_vs_full_current_model"]
            lines.append(
                f"| {variant} | {horizon} | "
                f"{oos['gate_decision']} | "
                f"{metrics.get('observations', 0)} | "
                f"{percent(metrics.get('median_net_return'))} | "
                f"{percent(metrics.get('median_market_excess'))} / "
                f"{percent(metrics.get('median_industry_excess'))} / "
                f"{percent(metrics.get('median_matched_excess'))} | "
                f"{percent(price.get('median_net_increment'))} | "
                f"{full['status']} |"
            )
    lines.extend(["", "## 边界", ""])
    lines.extend(f"- {item}" for item in summary["caveats"])
    return "\n".join(lines) + "\n"


def run_validation(project: Path) -> dict[str, Any]:
    project = project.resolve()
    protocol = _protocol(project)
    aliases, alias_hash = _canonicalizer(
        project
        / "data"
        / "processed"
        / "security_history"
        / "security_aliases.csv"
    )
    bar_database, dates, bar_cache = _build_bar_database(
        project, aliases, alias_hash
    )
    dates = [day for day in dates if "20230101" <= day <= AS_OF_TRADE_DATE]
    if not dates or any(day > AS_OF_TRADE_DATE for day in dates):
        raise AssertionError("2026 行情进入 daily_candidate 冻结验证")
    calendar_path = project / "data" / "processed" / "trade_calendar.csv"
    official_calendar = [
        day
        for day in _load_calendar(calendar_path)
        if "20230101" <= day <= AS_OF_TRADE_DATE
    ]
    if dates != official_calendar:
        raise ValueError("行情与官方交易日历不一致")
    development_cutoff = _purged_development_cutoff(
        dates, DEVELOPMENT_END, 60
    )
    security_database, security_hashes = _load_security_state(project)
    (
        market,
        industry_daily,
        industry_by_symbol,
        industry_members,
        benchmark_hashes,
    ) = _load_benchmarks(project, aliases)
    listing_dates = _listing_dates(
        project
        / "data"
        / "processed"
        / "security_history"
        / "security_master.csv",
        aliases,
    )
    valuations, valuation_audit = _load_valuations(project, aliases)
    fundamentals, fundamental_audit = _load_fundamentals(project, aliases)
    announcements, announcement_audit = _load_announcements(project, aliases)
    selections, scan_audit = _scan_candidates(
        project,
        dates,
        aliases,
        security_database,
        listing_dates,
        industry_by_symbol,
        valuations,
        fundamentals,
        announcements,
    )
    valuation_sufficient = bool(scan_audit["valuation_audit_sufficient"])
    valuation_audit.update(
        {
            "join_coverage": scan_audit["valuation_join_coverage"],
            "status": (
                "available"
                if valuation_sufficient
                else "data_unavailable"
            ),
            "default_three_not_treated_as_validated_valuation": True,
        }
    )
    observations, evaluation_audit = _evaluate(
        bar_database,
        security_database,
        dates,
        selections,
        market,
        industry_daily,
        industry_by_symbol,
        industry_members,
        listing_dates,
    )
    date_index = {day: index for index, day in enumerate(dates)}
    variants: dict[str, Any] = {}
    valuation_reason = (
        "PIT valuation exact-date join coverage is below the frozen "
        f"{VALUATION_MINIMUM_COVERAGE:.0%} threshold"
    )
    for variant in VARIANTS:
        unavailable = (
            variant in VALUATION_DEPENDENT and not valuation_sufficient
        )
        development: dict[int, dict[str, Any]] = {}
        oos: dict[int, dict[str, Any]] = {}
        for horizon in HORIZONS:
            train_rows = [
                row
                for row in observations
                if row.variant == variant
                and row.horizon == horizon
                and row.period == "development"
                and row.signal_date <= development_cutoff
            ]
            oos_rows = [
                row
                for row in observations
                if row.variant == variant
                and row.horizon == horizon
                and row.period == "rolling_oos"
            ]
            development[horizon] = _gate(train_rows, date_index)
            oos[horizon] = _gate(oos_rows, date_index)
            development[horizon][
                "increment_vs_price_score_only"
            ] = _comparison(
                observations,
                variant,
                "price_score_only",
                horizon,
                "development",
                development_cutoff=development_cutoff,
                unavailable_reason=valuation_reason if unavailable else None,
            )
            oos[horizon]["increment_vs_price_score_only"] = _comparison(
                observations,
                variant,
                "price_score_only",
                horizon,
                "rolling_oos",
                development_cutoff=development_cutoff,
                unavailable_reason=valuation_reason if unavailable else None,
            )
            development[horizon][
                "increment_vs_full_current_model"
            ] = _comparison(
                observations,
                variant,
                "full_current_model",
                horizon,
                "development",
                development_cutoff=development_cutoff,
                unavailable_reason=(
                    valuation_reason if not valuation_sufficient else None
                ),
            )
            oos[horizon][
                "increment_vs_full_current_model"
            ] = _comparison(
                observations,
                variant,
                "full_current_model",
                horizon,
                "rolling_oos",
                development_cutoff=development_cutoff,
                unavailable_reason=(
                    valuation_reason if not valuation_sufficient else None
                ),
            )
            if unavailable:
                development[horizon]["gate_decision"] = "data_unavailable"
                oos[horizon]["gate_decision"] = "data_unavailable"
        nomination = None if unavailable else _nominate_horizon(development)
        horizons = {}
        for horizon in HORIZONS:
            final_oos = (
                {
                    **oos[horizon],
                    "training_nominated": False,
                    "oos_cannot_self_nominate": True,
                }
                if unavailable
                else _finalize_oos(
                    development[horizon],
                    oos[horizon],
                    nominated=horizon == nomination,
                )
            )
            horizons[str(horizon)] = {
                "development": development[horizon],
                "rolling_oos": final_oos,
            }
        variants[variant] = {
            "data_status": (
                "data_unavailable" if unavailable else "available"
            ),
            "training_nomination": {
                "horizon": nomination,
                "selection_source": "development_2023_2024_only",
            },
            "horizons": horizons,
            "final_gate_decision": (
                horizons[str(nomination)]["rolling_oos"]["gate_decision"]
                if nomination is not None
                else "data_unavailable"
                if unavailable
                else "reject"
            ),
        }
    parameters = {
        "score": {
            "momentum20": 30,
            "low_volatility20": 20,
            "turnover_trend20": 15,
            "quality_constant": 5,
            "valuation_default": 3,
            "execution_constant": 7,
            "candidate_count": 5,
        },
        "variants": VARIANTS,
        "horizons": HORIZONS,
        "offsets": OFFSETS,
        "step_trading_days": STEP_DAYS,
        "purge_trading_days": 60,
        "as_of_trade_date": AS_OF_TRADE_DATE,
        "valuation_minimum_join_coverage": VALUATION_MINIMUM_COVERAGE,
        "entry": "next_trading_day_open",
        "positive_fundamental_weight": 0,
        "positive_announcement_weight": 0,
        "fulltext_news_opinion_confirmation_stoploss": False,
    }
    parameters_hash = hashlib.sha256(
        json.dumps(parameters, sort_keys=True).encode()
    ).hexdigest()
    summary = {
        "schema_version": 1,
        "status": "completed_first_pass",
        "generated_at": datetime.now(UTC).isoformat(),
        "as_of_trade_date": AS_OF_TRADE_DATE,
        "effective_development_signal_end": development_cutoff,
        "2026_holdout_opened": False,
        "research_only": True,
        "protocol": protocol,
        "model": {
            "id": "daily_candidate_v0_1_historical_replay",
            "parameters": parameters,
            "parameters_sha256": parameters_hash,
            "implementation_sha256": _sha256(Path(__file__)),
            "pipeline_sha256": _sha256(Path(__file__).with_name("pipeline.py")),
            "factors_sha256": _sha256(Path(__file__).with_name("factors.py")),
        },
        "data": {
            "security_aliases_sha256": alias_hash,
            "trade_calendar_sha256": _sha256(calendar_path),
            "security_state_hashes": security_hashes,
            "benchmark_hashes": benchmark_hashes,
            "bar_cache": bar_cache,
            "first_date": dates[0],
            "last_date": dates[-1],
            "files_evaluated": len(dates),
            "2026_rows_evaluated": 0,
            "valuation_audit": valuation_audit,
            "fundamental_audit": fundamental_audit,
            "announcement_audit": announcement_audit,
            "scan_audit": scan_audit,
            "evaluation_audit": evaluation_audit,
        },
        "variants": variants,
        "caveats": [
            "2026 final holdout was not opened, scanned, or evaluated.",
            "The final 60 official trading days of 2024 are purged from training.",
            "The five offsets remain separate five-trading-day rebalance paths.",
            "The 2025 period can only accept or reject a training nomination.",
            "PIT valuation coverage is audited before any full-model conclusion.",
            "The default valuation score of 3 is diagnostic only and never proves valuation efficacy.",
            "Caps only reduce scores; fundamentals and announcements never add positive weight.",
            "Missing official market, PIT industry, matched, or price-score controls are never zero-filled.",
            "No full text, news, community opinion, buy confirmation, stop loss, or parameter search is used.",
        ],
    }
    output = project / "reports" / "daily_candidate_historical_validation"
    output.mkdir(parents=True, exist_ok=True)
    json_path = output / "first_pass_summary.json"
    markdown_path = output / "report.md"
    json_path.write_text(
        json.dumps(summary, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    markdown_path.write_text(_report_markdown(summary), encoding="utf-8")
    return {
        "status": summary["status"],
        "as_of_trade_date": AS_OF_TRADE_DATE,
        "effective_development_signal_end": development_cutoff,
        "2026_holdout_opened": False,
        "protocol_sha256": protocol["sha256"],
        "parameters_sha256": parameters_hash,
        "implementation_sha256": summary["model"]["implementation_sha256"],
        "valuation_status": valuation_audit["status"],
        "gate_decisions": {
            variant: result["final_gate_decision"]
            for variant, result in variants.items()
        },
        "report_json": str(json_path),
        "report_markdown": str(markdown_path),
    }


def main() -> None:
    parser = argparse.ArgumentParser(
        description="daily_candidate_v0_1 历史重放冻结验证"
    )
    parser.add_argument("--root", default=".")
    parser.add_argument(
        "--as-of-trade-date", default=AS_OF_TRADE_DATE
    )
    args = parser.parse_args()
    if _date(args.as_of_trade_date) != AS_OF_TRADE_DATE:
        raise SystemExit("冻结验证仅允许 --as-of-trade-date 20251231")
    print(
        json.dumps(
            run_validation(Path(args.root)),
            ensure_ascii=False,
            indent=2,
        )
    )


if __name__ == "__main__":
    main()

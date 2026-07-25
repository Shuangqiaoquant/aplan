from __future__ import annotations

import argparse
import bisect
import csv
import hashlib
import json
import math
import sqlite3
import tomllib
from collections import defaultdict
from dataclasses import dataclass
from datetime import UTC, date, datetime
from pathlib import Path
from statistics import mean, median
from typing import Any, Iterable

from .announcement_event_validation import (
    _benchmark_return,
    _build_bar_database,
    _canonical,
    _canonicalizer,
    _date,
    _industry_at,
    _load_benchmarks,
    _load_calendar,
    _load_security_state,
    _manifest,
    _market_regimes,
    _max_drawdown,
    _number,
    _purged_development_cutoff,
    _quantile,
    _return,
    _sha256,
)


AS_OF_TRADE_DATE = "20251231"
DEVELOPMENT_END = "20241231"
PROTOCOL_SHA256 = (
    "b9780f23becf494770251a24f48980b8db5f2b42511b9ff49491a934696aae20"
)
MARKET_CODE = "000300.SH"
HORIZONS = (1, 5, 10, 20, 40, 60)
MINIMUM_LISTING_DAYS = 120
MINIMUM_MEDIAN_TURNOVER20 = 50_000_000.0
MINIMUM_INDUSTRY_MEMBERS = 5
TOP_FRACTION = 0.20
MINIMUM_OOS_OBSERVATIONS = 100
MINIMUM_OOS_COHORTS = 12
MINIMUM_STABILITY_RATIO = 0.60
MAXIMUM_DRAWDOWN = 0.25
TAIL_LOSS_THRESHOLD = -0.10

VARIANT_METRICS = {
    "quality_growth_core": (
        "revenue_growth",
        "net_profit_growth",
        "roe",
        "debt_to_assets",
    ),
    "quality_only": ("roe", "debt_to_assets"),
    "growth_only": ("revenue_growth", "net_profit_growth"),
}
MINIMUM_AVAILABLE_METRICS = {
    "quality_growth_core": 3,
    "quality_only": 2,
    "growth_only": 2,
}
LOW_IS_BETTER = {"debt_to_assets"}


@dataclass(frozen=True, slots=True)
class FundamentalSnapshot:
    symbol: str
    period_end: str
    publish_date: str
    revenue_growth: float | None
    net_profit_growth: float | None
    roe: float | None
    debt_to_assets: float | None
    operating_cashflow_to_profit: float | None
    source_hash: str


@dataclass(frozen=True, slots=True)
class Candidate:
    symbol: str
    signal_date: str
    entry_date: str
    industry_code: str
    median_turnover20: float
    liquidity_quintile: int
    period_end: str
    score: float
    available_metrics: int
    cashflow_diagnostic_available: bool


@dataclass(frozen=True, slots=True)
class Observation:
    variant: str
    cohort_type: str
    period: str
    horizon: int
    signal_date: str
    entry_date: str
    symbol: str
    industry_code: str
    gross_return: float
    net_return: float
    market_excess: float | None
    industry_excess: float | None
    matched_excess: float | None
    market_regime: str


def _protocol(project: Path) -> dict[str, Any]:
    path = project / "config" / "fundamental_quality_validation.toml"
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
            "基本面冻结规范哈希不匹配："
            f"expected={PROTOCOL_SHA256} actual={digest}"
        )
    document = tomllib.loads(path.read_text(encoding="utf-8"))
    if document.get("final_holdout_opened") is not False:
        raise ValueError("基本面冻结规范必须保持 final_holdout_opened=false")
    as_of = _date(document.get("time_design", {}).get("research_as_of_trade_date"))
    if as_of != AS_OF_TRADE_DATE:
        raise ValueError("基本面冻结规范 as_of_trade_date 已变化")
    if document.get("cashflow_quality", {}).get("first_pass_use") != "diagnostic_only":
        raise ValueError("现金流利润比必须保持 diagnostic_only")
    if document.get("change_control", {}).get("cashflow_ratio_allowed_in_score"):
        raise ValueError("现金流利润比不得进入首轮评分")
    return {"path": str(path), "sha256": digest, "document": document}


def _monthly_signal_dates(calendar: Iterable[str]) -> list[str]:
    first_by_month: dict[str, str] = {}
    for day in sorted(set(calendar)):
        if "20230101" <= day <= AS_OF_TRADE_DATE:
            first_by_month.setdefault(day[:6], day)
    return [first_by_month[key] for key in sorted(first_by_month)]


def _snapshot_from_row(
    row: dict[str, str],
    aliases: dict[str, str],
) -> FundamentalSnapshot | None:
    symbol = _canonical(str(row.get("symbol") or "").strip(), aliases)
    period_end = _date(row.get("period_end"))
    publish_date = _date(row.get("publish_time"))
    if len(symbol) != 6 or not period_end or not publish_date:
        return None
    return FundamentalSnapshot(
        symbol=symbol,
        period_end=period_end,
        publish_date=publish_date,
        revenue_growth=_number(row.get("revenue_growth")),
        net_profit_growth=_number(row.get("net_profit_growth")),
        roe=_number(row.get("roe")),
        debt_to_assets=_number(row.get("debt_to_assets")),
        operating_cashflow_to_profit=_number(
            row.get("operating_cashflow_to_profit")
        ),
        source_hash=str(row.get("source_hash") or ""),
    )


def _load_snapshot_timelines(
    path: Path,
    aliases: dict[str, str],
) -> tuple[
    dict[str, tuple[list[str], list[FundamentalSnapshot]]],
    dict[str, Any],
]:
    incoming: dict[str, list[FundamentalSnapshot]] = defaultdict(list)
    rows_before_cutoff = 0
    invalid_rows = 0
    with path.open(encoding="utf-8-sig", newline="") as handle:
        for raw in csv.DictReader(handle):
            item = _snapshot_from_row(raw, aliases)
            if item is None:
                invalid_rows += 1
                continue
            if item.publish_date > AS_OF_TRADE_DATE:
                continue
            incoming[item.symbol].append(item)
            rows_before_cutoff += 1

    timelines: dict[str, tuple[list[str], list[FundamentalSnapshot]]] = {}
    correction_versions = 0
    for symbol, items in incoming.items():
        by_period: dict[str, FundamentalSnapshot] = {}
        dates: list[str] = []
        visible: list[FundamentalSnapshot] = []
        for item in sorted(
            items,
            key=lambda value: (
                value.publish_date,
                value.period_end,
                value.source_hash,
            ),
        ):
            correction_versions += int(item.period_end in by_period)
            by_period[item.period_end] = item
            latest_period = max(by_period)
            dates.append(item.publish_date)
            visible.append(by_period[latest_period])
        timelines[symbol] = (dates, visible)
    return timelines, {
        "rows_visible_through_20251231": rows_before_cutoff,
        "symbols": len(timelines),
        "invalid_rows": invalid_rows,
        "correction_versions": correction_versions,
        "2026_rows_evaluated": 0,
        "2026_holdout_opened": False,
    }


def _snapshot_at(
    timeline: tuple[list[str], list[FundamentalSnapshot]] | None,
    signal_date: str,
) -> FundamentalSnapshot | None:
    if timeline is None:
        return None
    dates, values = timeline
    position = bisect.bisect_right(dates, signal_date) - 1
    return values[position] if position >= 0 else None


def _percentile_ranks(
    values: dict[str, float],
    *,
    low_is_better: bool = False,
) -> dict[str, float]:
    if not values:
        return {}
    ordered = sorted(
        values.items(),
        key=lambda item: (item[1], item[0]),
        reverse=low_is_better,
    )
    output: dict[str, float] = {}
    denominator = max(len(ordered) - 1, 1)
    index = 0
    while index < len(ordered):
        stop = index + 1
        while stop < len(ordered) and ordered[stop][1] == ordered[index][1]:
            stop += 1
        average_position = (index + stop - 1) / 2
        rank = average_position / denominator if len(ordered) > 1 else 1.0
        for offset in range(index, stop):
            output[ordered[offset][0]] = rank
        index = stop
    return output


def _score_industry(
    snapshots: dict[str, FundamentalSnapshot],
    variant: str,
) -> dict[str, tuple[float, int]]:
    metrics = VARIANT_METRICS[variant]
    ranks: dict[str, dict[str, float]] = {}
    for metric in metrics:
        values = {
            symbol: value
            for symbol, snapshot in snapshots.items()
            if (value := getattr(snapshot, metric)) is not None
        }
        ranks[metric] = _percentile_ranks(
            values,
            low_is_better=metric in LOW_IS_BETTER,
        )
    scores: dict[str, tuple[float, int]] = {}
    for symbol in snapshots:
        available = [
            ranks[metric][symbol]
            for metric in metrics
            if symbol in ranks[metric]
        ]
        if len(available) >= MINIMUM_AVAILABLE_METRICS[variant]:
            scores[symbol] = (mean(available), len(available))
    return scores


def _boundary_symbols(
    scores: dict[str, tuple[float, int]],
) -> tuple[set[str], set[str]]:
    if len(scores) < MINIMUM_INDUSTRY_MEMBERS:
        return set(), set()
    count = max(1, math.ceil(len(scores) * TOP_FRACTION))
    descending = sorted(scores, key=lambda symbol: (-scores[symbol][0], symbol))
    ascending = sorted(scores, key=lambda symbol: (scores[symbol][0], symbol))
    return set(descending[:count]), set(ascending[:count])


def _liquidity_quintiles(turnover: dict[str, float]) -> dict[str, int]:
    ordered = sorted(turnover, key=lambda symbol: (turnover[symbol], symbol))
    size = len(ordered)
    if not size:
        return {}
    return {
        symbol: min(5, int(index * 5 / size) + 1)
        for index, symbol in enumerate(ordered)
    }


def _listing_dates(path: Path, aliases: dict[str, str]) -> dict[str, str]:
    result: dict[str, str] = {}
    with path.open(encoding="utf-8-sig", newline="") as handle:
        for row in csv.DictReader(handle):
            symbol = _canonical(str(row.get("symbol") or ""), aliases)
            listing = _date(row.get("list_date"))
            if len(symbol) == 6 and listing:
                prior = result.get(symbol)
                result[symbol] = min(prior, listing) if prior else listing
    return result


def _days_listed(list_date: str, signal_date: str) -> int:
    first = date.fromisoformat(
        f"{list_date[:4]}-{list_date[4:6]}-{list_date[6:8]}"
    )
    current = date.fromisoformat(
        f"{signal_date[:4]}-{signal_date[4:6]}-{signal_date[6:8]}"
    )
    return (current - first).days


def _rows_for_date(
    connection: sqlite3.Connection,
    table: str,
    day: str,
    aliases: dict[str, str] | None = None,
) -> dict[str, sqlite3.Row]:
    return {
        _canonical(str(row["symbol"]), aliases or {}): row
        for row in connection.execute(
            f"SELECT * FROM {table} WHERE trade_date=?",
            (day,),
        )
    }


def _rank_month(
    *,
    signal_date: str,
    entry_date: str,
    snapshots: dict[str, FundamentalSnapshot],
    signal_bars: dict[str, sqlite3.Row],
    entry_status: dict[str, sqlite3.Row],
    listing_dates: dict[str, str],
    industry_by_symbol: dict[str, list[tuple[str, str, str]]],
) -> tuple[
    dict[str, dict[str, list[Candidate]]],
    dict[str, Any],
]:
    eligible_by_industry: dict[str, dict[str, FundamentalSnapshot]] = defaultdict(dict)
    turnover_by_industry: dict[str, dict[str, float]] = defaultdict(dict)
    audit: dict[str, int] = defaultdict(int)
    for symbol, snapshot in snapshots.items():
        audit["snapshot_candidates"] += 1
        listing = listing_dates.get(symbol)
        if not listing:
            audit["missing_list_date"] += 1
            continue
        if _days_listed(listing, signal_date) < MINIMUM_LISTING_DAYS:
            audit["listing_age_lt_120d"] += 1
            continue
        status = entry_status.get(symbol)
        if status is None:
            audit["missing_entry_status"] += 1
            continue
        if status["is_st"]:
            audit["excluded_st"] += 1
            continue
        if status["is_suspended"]:
            audit["excluded_suspended"] += 1
            continue
        bar = signal_bars.get(symbol)
        if bar is None or bar["median_turnover20"] is None:
            audit["missing_liquidity_history"] += 1
            continue
        turnover = float(bar["median_turnover20"])
        if turnover < MINIMUM_MEDIAN_TURNOVER20:
            audit["below_turnover_threshold"] += 1
            continue
        industry = _industry_at(symbol, signal_date, industry_by_symbol)
        if not industry:
            audit["missing_pit_industry"] += 1
            continue
        audit["eligible"] += 1
        eligible_by_industry[industry][symbol] = snapshot
        turnover_by_industry[industry][symbol] = turnover

    selections: dict[str, dict[str, list[Candidate]]] = {
        variant: {"ranked": [], "top": [], "bottom": []}
        for variant in VARIANT_METRICS
    }
    metric_coverage: dict[str, list[int]] = defaultdict(list)
    for industry, industry_snapshots in eligible_by_industry.items():
        if len(industry_snapshots) < MINIMUM_INDUSTRY_MEMBERS:
            audit["industry_members_lt_5"] += len(industry_snapshots)
            continue
        quintiles = _liquidity_quintiles(turnover_by_industry[industry])
        for variant in VARIANT_METRICS:
            scores = _score_industry(industry_snapshots, variant)
            top, bottom = _boundary_symbols(scores)
            metric_coverage[variant].extend(
                count for _, count in scores.values()
            )
            candidates = {}
            for symbol in sorted(scores):
                snapshot = industry_snapshots[symbol]
                score, available = scores[symbol]
                candidates[symbol] = Candidate(
                    symbol=symbol,
                    signal_date=signal_date,
                    entry_date=entry_date,
                    industry_code=industry,
                    median_turnover20=turnover_by_industry[industry][symbol],
                    liquidity_quintile=quintiles[symbol],
                    period_end=snapshot.period_end,
                    score=score,
                    available_metrics=available,
                    cashflow_diagnostic_available=(
                        snapshot.operating_cashflow_to_profit is not None
                    )
                )
            selections[variant]["ranked"].extend(candidates.values())
            selections[variant]["top"].extend(
                candidates[symbol] for symbol in sorted(top)
            )
            selections[variant]["bottom"].extend(
                candidates[symbol] for symbol in sorted(bottom)
            )
    audit["metric_coverage_distribution"] = {
        variant: {
            str(count): values.count(count)
            for count in sorted(set(values))
        }
        for variant, values in metric_coverage.items()
    }
    return selections, dict(audit)


def _matched_symbols(
    candidate: Candidate,
    pool: list[Candidate],
    excluded: set[str],
) -> list[str]:
    eligible = [
        item
        for item in pool
        if item.symbol != candidate.symbol
        and item.symbol not in excluded
        and item.industry_code == candidate.industry_code
        and item.liquidity_quintile == candidate.liquidity_quintile
    ]
    return [
        item.symbol
        for item in sorted(
            eligible,
            key=lambda item: (
                abs(item.score - candidate.score),
                item.symbol,
            ),
        )
    ]


def _evaluate_candidate(
    *,
    candidate: Candidate,
    variant: str,
    cohort_type: str,
    dates: list[str],
    date_index: dict[str, int],
    bars_by_date: dict[str, dict[str, sqlite3.Row]],
    market: dict[tuple[str, str], tuple[float, float]],
    industry_daily: dict[tuple[str, str], tuple[float, float]],
    market_regimes: dict[str, str],
    matched_symbols: list[str],
) -> list[Observation]:
    signal_index = date_index[candidate.signal_date]
    entry = bars_by_date.get(candidate.entry_date, {}).get(candidate.symbol)
    if entry is None or entry["is_suspended"]:
        return []
    if entry["is_limit_up"] and entry["high"] == entry["low"]:
        return []
    output = []
    for horizon in HORIZONS:
        exit_index = signal_index + horizon
        if exit_index >= len(dates):
            continue
        exit_date = dates[exit_index]
        exit_bar = bars_by_date.get(exit_date, {}).get(candidate.symbol)
        if exit_bar is None:
            continue
        gross, net = _return(float(entry["open"]), float(exit_bar["close"]))
        market_return = _benchmark_return(
            market,
            MARKET_CODE,
            candidate.entry_date,
            exit_date,
        )
        industry_return = _benchmark_return(
            industry_daily,
            candidate.industry_code,
            candidate.entry_date,
            exit_date,
        )
        control_returns = []
        for symbol in matched_symbols:
            control_entry = bars_by_date.get(candidate.entry_date, {}).get(symbol)
            control_exit = bars_by_date.get(exit_date, {}).get(symbol)
            if control_entry is None or control_exit is None:
                continue
            if control_entry["is_suspended"]:
                continue
            if (
                control_entry["is_limit_up"]
                and control_entry["high"] == control_entry["low"]
            ):
                continue
            _, control_net = _return(
                float(control_entry["open"]),
                float(control_exit["close"]),
            )
            control_returns.append(control_net)
        matched_return = mean(control_returns) if control_returns else None
        output.append(
            Observation(
                variant=variant,
                cohort_type=cohort_type,
                period=(
                    "development"
                    if candidate.signal_date <= DEVELOPMENT_END
                    else "rolling_oos"
                ),
                horizon=horizon,
                signal_date=candidate.signal_date,
                entry_date=candidate.entry_date,
                symbol=candidate.symbol,
                industry_code=candidate.industry_code,
                gross_return=gross,
                net_return=net,
                market_excess=(
                    (1 + net) / (1 + market_return) - 1
                    if market_return is not None and market_return > -1
                    else None
                ),
                industry_excess=(
                    (1 + net) / (1 + industry_return) - 1
                    if industry_return is not None and industry_return > -1
                    else None
                ),
                matched_excess=(
                    (1 + net) / (1 + matched_return) - 1
                    if matched_return is not None and matched_return > -1
                    else None
                ),
                market_regime=market_regimes.get(
                    candidate.signal_date,
                    "insufficient",
                ),
            )
        )
    return output


def _distribution(values: list[float]) -> dict[str, Any]:
    if not values:
        return {"observations": 0}
    return {
        "observations": len(values),
        "mean": mean(values),
        "median": median(values),
        "win_rate": sum(value > 0 for value in values) / len(values),
        "p05": _quantile(values, 0.05),
        "p25": _quantile(values, 0.25),
        "p75": _quantile(values, 0.75),
        "p95": _quantile(values, 0.95),
        "tail_loss_rate": (
            sum(value <= TAIL_LOSS_THRESHOLD for value in values) / len(values)
        ),
    }


def _portfolio_turnover(rows: list[Observation]) -> float | None:
    by_date: dict[str, set[str]] = defaultdict(set)
    for row in rows:
        by_date[row.signal_date].add(row.symbol)
    dates = sorted(by_date)
    if len(dates) < 2:
        return None
    values = []
    for previous, current in zip(dates, dates[1:]):
        first, second = by_date[previous], by_date[current]
        weights = set(first) | set(second)
        difference = sum(
            abs(
                (1 / len(second) if symbol in second else 0)
                - (1 / len(first) if symbol in first else 0)
            )
            for symbol in weights
        )
        values.append(difference / 2)
    return mean(values)


def _stats(rows: list[Observation]) -> dict[str, Any]:
    if not rows:
        return {
            "status": "insufficient_data",
            "observations": 0,
            "signals": 0,
            "canonical_securities": 0,
            "cohorts": 0,
        }
    gross = [row.gross_return for row in rows]
    net = [row.net_return for row in rows]
    market = [row.market_excess for row in rows if row.market_excess is not None]
    industry = [
        row.industry_excess for row in rows if row.industry_excess is not None
    ]
    matched = [row.matched_excess for row in rows if row.matched_excess is not None]
    signals = {(row.signal_date, row.symbol) for row in rows}
    return {
        "status": "ok",
        "observations": len(rows),
        "signals": len(signals),
        "canonical_securities": len({row.symbol for row in rows}),
        "cohorts": len({row.signal_date for row in rows}),
        "mean_gross_return": mean(gross),
        "mean_net_return": mean(net),
        "median_net_return": median(net),
        "win_rate": sum(value > 0 for value in net) / len(net),
        "p05": _quantile(net, 0.05),
        "p25": _quantile(net, 0.25),
        "p75": _quantile(net, 0.75),
        "p95": _quantile(net, 0.95),
        "tail_loss_rate": (
            sum(value <= TAIL_LOSS_THRESHOLD for value in net) / len(net)
        ),
        "max_drawdown": _max_drawdown(
            [(row.signal_date, row.net_return) for row in rows]
        ),
        "turnover": _portfolio_turnover(rows),
        "mean_transaction_cost": mean(
            row.gross_return - row.net_return for row in rows
        ),
        "market_join_coverage": len(market) / len(rows),
        "industry_join_coverage": len(industry) / len(rows),
        "matched_control_coverage": len(matched) / len(rows),
        "median_market_excess": median(market) if market else None,
        "median_industry_excess": median(industry) if industry else None,
        "median_matched_excess": median(matched) if matched else None,
        "distributions": {
            "gross_return": _distribution(gross),
            "net_return": _distribution(net),
            "market_excess": _distribution(market),
            "industry_excess": _distribution(industry),
            "matched_control_excess": _distribution(matched),
        },
    }


def _rolling_ratio(
    rows: list[Observation],
    date_index: dict[str, int],
    *,
    positive: bool,
) -> float | None:
    relevant = [row for row in rows if row.matched_excess is not None]
    if not relevant:
        return None
    decisions = []
    for start in range(0, len(date_index), 21):
        stop = start + 63
        window = [
            row.matched_excess
            for row in relevant
            if start <= date_index.get(row.signal_date, -1) < stop
        ]
        if len(window) < 20:
            continue
        value = median(window)
        decisions.append(value > 0 if positive else value < 0)
    return sum(decisions) / len(decisions) if decisions else None


def _regime_ratio(rows: list[Observation], *, positive: bool) -> float | None:
    grouped: dict[str, list[float]] = defaultdict(list)
    for row in rows:
        if row.matched_excess is not None and row.market_regime != "insufficient":
            grouped[row.market_regime].append(row.matched_excess)
    eligible = [values for values in grouped.values() if len(values) >= 30]
    if not eligible:
        return None
    decisions = [
        median(values) > 0 if positive else median(values) < 0
        for values in eligible
    ]
    return sum(decisions) / len(decisions)


def _regime_slices(rows: list[Observation]) -> dict[str, Any]:
    grouped: dict[str, list[Observation]] = defaultdict(list)
    for row in rows:
        grouped[row.market_regime].append(row)
    return {
        regime: {
            "observations": len(values),
            "median_net_return": median(item.net_return for item in values),
            "median_matched_excess": (
                median(matched)
                if (
                    matched := [
                        item.matched_excess
                        for item in values
                        if item.matched_excess is not None
                    ]
                )
                else None
            ),
        }
        for regime, values in sorted(grouped.items())
    }


def _gate(
    rows: list[Observation],
    date_index: dict[str, int],
    *,
    negative: bool,
) -> dict[str, Any]:
    metrics = _stats(rows)
    rolling = _rolling_ratio(rows, date_index, positive=not negative)
    regime = _regime_ratio(rows, positive=not negative)
    metrics["rolling_window_ratio"] = rolling
    metrics["market_regime_ratio"] = regime
    metrics["market_regimes"] = _regime_slices(rows)
    ready = (
        metrics.get("observations", 0) >= MINIMUM_OOS_OBSERVATIONS
        and metrics.get("cohorts", 0) >= MINIMUM_OOS_COHORTS
        and metrics.get("matched_control_coverage", 0) > 0
    )
    if negative:
        supported = bool(
            ready
            and metrics.get("median_matched_excess") is not None
            and metrics["median_matched_excess"] < 0
            and rolling is not None
            and rolling >= MINIMUM_STABILITY_RATIO
            and regime is not None
            and regime >= MINIMUM_STABILITY_RATIO
        )
        return {
            "gate_decision": "hazard_supported" if supported else "reject",
            "hazard_supported": supported,
            "risk_gate_eligible": False,
            "metrics": metrics,
        }
    survived = bool(
        ready
        and metrics.get("median_market_excess") is not None
        and metrics["median_market_excess"] > 0
        and metrics.get("median_industry_excess") is not None
        and metrics["median_industry_excess"] > 0
        and metrics.get("median_matched_excess") is not None
        and metrics["median_matched_excess"] > 0
        and rolling is not None
        and rolling >= MINIMUM_STABILITY_RATIO
        and regime is not None
        and regime >= MINIMUM_STABILITY_RATIO
        and metrics.get("max_drawdown") is not None
        and metrics["max_drawdown"] <= MAXIMUM_DRAWDOWN
    )
    return {
        "gate_decision": "survive_first_pass" if survived else "reject",
        "metrics": metrics,
    }


def _finalize_oos(
    development: dict[str, Any],
    rolling_oos: dict[str, Any],
    *,
    nominated: bool,
    negative: bool,
) -> dict[str, Any]:
    output = dict(rolling_oos)
    output["training_nominated"] = nominated
    output["oos_cannot_self_nominate"] = True
    supported = rolling_oos["gate_decision"] in {
        "survive_first_pass",
        "hazard_supported",
    }
    output["oos_supported"] = supported
    if not nominated or not supported:
        output["gate_decision"] = "reject"
    elif negative:
        output["gate_decision"] = "hazard_supported"
        output["risk_gate_eligible"] = False
    else:
        output["gate_decision"] = "survive_first_pass"
    output["development_gate_decision"] = development["gate_decision"]
    return output


def _nominate_horizon(
    development_by_horizon: dict[int, dict[str, Any]],
) -> int | None:
    survivors = [
        (horizon, result)
        for horizon, result in development_by_horizon.items()
        if result["gate_decision"] in {"survive_first_pass", "hazard_supported"}
    ]
    if not survivors:
        return None
    return max(
        survivors,
        key=lambda item: (
            abs(item[1]["metrics"].get("median_matched_excess") or 0),
            -item[0],
        ),
    )[0]


def _snapshot_for_month(
    timelines: dict[str, tuple[list[str], list[FundamentalSnapshot]]],
    signal_date: str,
) -> dict[str, FundamentalSnapshot]:
    output = {}
    for symbol, timeline in timelines.items():
        item = _snapshot_at(timeline, signal_date)
        if item is not None:
            output[symbol] = item
    return output


def _report_markdown(summary: dict[str, Any]) -> str:
    lines = [
        "# 基本面质量首轮冻结验证",
        "",
        f"- 状态：`{summary['status']}`",
        f"- 研究截止：`{summary['as_of_trade_date']}`",
        f"- 冻结规范：`{summary['protocol']['sha256']}`",
        f"- 模型参数：`{summary['model']['parameters_sha256']}`",
        "- 2026 最终留出集：`sealed`",
        "",
        "> 现金流利润比仅报告诊断覆盖率，不参与任何分数、排名或门槛。",
        "",
        "| 变体 | 训练提名周期 | OOS 最终结论 |",
        "|---|---:|---|",
    ]
    for variant, result in summary["variants"].items():
        nominated = result["training_nomination"]["horizon"]
        decision = (
            result["horizons"].get(str(nominated), {})
            .get("rolling_oos", {})
            .get("gate_decision", "reject")
            if nominated is not None
            else "reject"
        )
        lines.append(f"| {variant} | {nominated or '-'} | {decision} |")
    lines.extend(
        [
            "",
            "## 全周期样本外结果",
            "",
            "| 变体 | 周期 | 训练提名 | 决策 | 样本 | 中位市场超额 | 中位行业超额 | 中位匹配超额 |",
            "|---|---:|---|---|---:|---:|---:|---:|",
        ]
    )
    for variant, result in summary["variants"].items():
        for horizon, values in result["horizons"].items():
            oos = values["rolling_oos"]
            metrics = oos["metrics"]
            fmt = lambda value: "NA" if value is None else f"{value:.2%}"
            lines.append(
                f"| {variant} | {horizon} | "
                f"{str(oos['training_nominated']).lower()} | "
                f"{oos['gate_decision']} | {metrics.get('observations', 0)} | "
                f"{fmt(metrics.get('median_market_excess'))} | "
                f"{fmt(metrics.get('median_industry_excess'))} | "
                f"{fmt(metrics.get('median_matched_excess'))} |"
            )
    risk = summary["fundamental_risk_cohort"]
    lines.extend(
        [
            "",
            "## 风险 cohort",
            "",
            f"- 训练提名周期：`{risk['training_nomination']['horizon']}`",
            "- 用途：仅作 hazard 诊断；不构造做空，不直接晋级 risk gate。",
            "",
            "## 数据覆盖",
            "",
            f"- 月度信号日：{summary['data']['monthly_signal_dates']}",
            f"- 快照可见行：{summary['data']['snapshot_audit']['rows_visible_through_20251231']}",
            f"- 2026 计算行：{summary['data']['snapshot_audit']['2026_rows_evaluated']}",
            "",
            "## 限制",
            "",
        ]
    )
    lines.extend(f"- {item}" for item in summary["caveats"])
    return "\n".join(lines) + "\n"


def run_validation(project: Path) -> dict[str, Any]:
    project = project.resolve()
    protocol = _protocol(project)
    fundamental_root = (
        project / "data" / "processed" / "yinhe_fundamentals"
    )
    snapshot_path = fundamental_root / "fundamental_snapshots.csv"
    snapshot_manifest_path = fundamental_root / "snapshot_manifest.json"
    snapshot_manifest = _manifest(snapshot_manifest_path)
    expected_snapshot_hash = (
        snapshot_manifest.get("hashes", {}).get(snapshot_path.name) or ""
    )
    actual_snapshot_hash = _sha256(snapshot_path)
    if not expected_snapshot_hash or expected_snapshot_hash != actual_snapshot_hash:
        raise ValueError("基本面快照哈希与 snapshot_manifest 不一致")
    if not snapshot_manifest.get("strict_availability_lag"):
        raise ValueError("基本面快照未通过严格可用时点验收")

    aliases, alias_hash = _canonicalizer(
        project
        / "data"
        / "processed"
        / "security_history"
        / "security_aliases.csv"
    )
    timelines, snapshot_audit = _load_snapshot_timelines(snapshot_path, aliases)
    bar_database, dates, bar_cache = _build_bar_database(
        project,
        aliases,
        alias_hash,
    )
    dates = [day for day in dates if day <= AS_OF_TRADE_DATE]
    development_cutoff = _purged_development_cutoff(dates)
    calendar_path = project / "data" / "processed" / "trade_calendar.csv"
    official_calendar = _load_calendar(calendar_path)
    signal_dates = _monthly_signal_dates(official_calendar)
    date_index = {day: index for index, day in enumerate(dates)}
    if any(day not in date_index for day in signal_dates):
        raise ValueError("月度首个官方交易日在银河行情中缺失")

    security_database, security_hashes = _load_security_state(project)
    market, industry_daily, industry_by_symbol, _, benchmark_hashes = (
        _load_benchmarks(project, aliases)
    )
    listing_dates = _listing_dates(
        project
        / "data"
        / "processed"
        / "security_history"
        / "security_master.csv",
        aliases,
    )
    regimes = _market_regimes(market, dates)

    bars = sqlite3.connect(bar_database)
    bars.row_factory = sqlite3.Row
    statuses = sqlite3.connect(security_database)
    statuses.row_factory = sqlite3.Row
    observations: list[Observation] = []
    monthly_audits: dict[str, Any] = {}
    cashflow_available = 0
    selected_total = 0
    try:
        for month_index, signal_date in enumerate(signal_dates, 1):
            position = date_index[signal_date]
            if position + 1 >= len(dates):
                continue
            entry_date = dates[position + 1]
            required_dates = {
                entry_date,
                *(
                    dates[position + horizon]
                    for horizon in HORIZONS
                    if position + horizon < len(dates)
                ),
            }
            bars_by_date = {
                day: _rows_for_date(bars, "bars", day)
                for day in required_dates
            }
            signal_bars = _rows_for_date(bars, "bars", signal_date)
            entry_status = _rows_for_date(
                statuses,
                "daily_status",
                entry_date,
                aliases,
            )
            visible = _snapshot_for_month(timelines, signal_date)
            selections, audit = _rank_month(
                signal_date=signal_date,
                entry_date=entry_date,
                snapshots=visible,
                signal_bars=signal_bars,
                entry_status=entry_status,
                listing_dates=listing_dates,
                industry_by_symbol=industry_by_symbol,
            )
            monthly_audits[signal_date] = audit
            for variant in VARIANT_METRICS:
                top = selections[variant]["top"]
                all_ranked = selections[variant]["ranked"]
                excluded = {item.symbol for item in top}
                for candidate in top:
                    controls = _matched_symbols(candidate, all_ranked, excluded)
                    observations.extend(
                        _evaluate_candidate(
                            candidate=candidate,
                            variant=variant,
                            cohort_type="top_20_percent",
                            dates=dates,
                            date_index=date_index,
                            bars_by_date=bars_by_date,
                            market=market,
                            industry_daily=industry_daily,
                            market_regimes=regimes,
                            matched_symbols=controls,
                        )
                    )
                    selected_total += 1
                    cashflow_available += int(
                        candidate.cashflow_diagnostic_available
                    )
            core_bottom = selections["quality_growth_core"]["bottom"]
            core_ranked = selections["quality_growth_core"]["ranked"]
            bottom_symbols = {item.symbol for item in core_bottom}
            for candidate in core_bottom:
                controls = _matched_symbols(
                    candidate,
                    core_ranked,
                    bottom_symbols,
                )
                observations.extend(
                    _evaluate_candidate(
                        candidate=candidate,
                        variant="fundamental_risk_cohort",
                        cohort_type="bottom_20_percent_core",
                        dates=dates,
                        date_index=date_index,
                        bars_by_date=bars_by_date,
                        market=market,
                        industry_daily=industry_daily,
                        market_regimes=regimes,
                        matched_symbols=controls,
                    )
                )
            if month_index % 6 == 0 or month_index == len(signal_dates):
                print(
                    f"基本面冻结验证：{month_index}/{len(signal_dates)}，"
                    f"observations={len(observations)}",
                    flush=True,
                )
    finally:
        bars.close()
        statuses.close()

    variants: dict[str, Any] = {}
    for variant in VARIANT_METRICS:
        development = {}
        oos_raw = {}
        for horizon in HORIZONS:
            development[horizon] = _gate(
                [
                    row
                    for row in observations
                    if row.variant == variant
                    and row.horizon == horizon
                    and row.period == "development"
                    and row.signal_date <= development_cutoff
                ],
                date_index,
                negative=False,
            )
            oos_raw[horizon] = _gate(
                [
                    row
                    for row in observations
                    if row.variant == variant
                    and row.horizon == horizon
                    and row.period == "rolling_oos"
                ],
                date_index,
                negative=False,
            )
        nomination = _nominate_horizon(development)
        variants[variant] = {
            "training_nomination": {
                "horizon": nomination,
                "selection_source": "development_2023_2024_only",
            },
            "horizons": {
                str(horizon): {
                    "development": development[horizon],
                    "rolling_oos": _finalize_oos(
                        development[horizon],
                        oos_raw[horizon],
                        nominated=horizon == nomination,
                        negative=False,
                    ),
                }
                for horizon in HORIZONS
            },
        }

    risk_development = {}
    risk_oos = {}
    for horizon in HORIZONS:
        risk_development[horizon] = _gate(
            [
                row
                for row in observations
                if row.variant == "fundamental_risk_cohort"
                and row.horizon == horizon
                and row.period == "development"
                and row.signal_date <= development_cutoff
            ],
            date_index,
            negative=True,
        )
        risk_oos[horizon] = _gate(
            [
                row
                for row in observations
                if row.variant == "fundamental_risk_cohort"
                and row.horizon == horizon
                and row.period == "rolling_oos"
            ],
            date_index,
            negative=True,
        )
    risk_nomination = _nominate_horizon(risk_development)
    risk_result = {
        "training_nomination": {
            "horizon": risk_nomination,
            "selection_source": "development_2023_2024_only",
        },
        "decision_use": "hazard_diagnostic_only",
        "short_portfolio": False,
        "risk_gate_eligible": False,
        "horizons": {
            str(horizon): {
                "development": risk_development[horizon],
                "rolling_oos": _finalize_oos(
                    risk_development[horizon],
                    risk_oos[horizon],
                    nominated=horizon == risk_nomination,
                    negative=True,
                ),
            }
            for horizon in HORIZONS
        },
    }

    parameters = {
        "as_of_trade_date": AS_OF_TRADE_DATE,
        "development": ["20230101", DEVELOPMENT_END],
        "rolling_oos": ["20250101", AS_OF_TRADE_DATE],
        "purge_trading_days": 60,
        "effective_development_signal_end": development_cutoff,
        "horizons": HORIZONS,
        "variants": VARIANT_METRICS,
        "minimum_available_metrics": MINIMUM_AVAILABLE_METRICS,
        "cashflow_ratio_in_score": False,
        "monthly_signal": "first_official_trading_day",
        "entry": "next_trading_day_open",
        "minimum_listing_days": MINIMUM_LISTING_DAYS,
        "listing_age_unit": "calendar_days",
        "minimum_median_turnover20": MINIMUM_MEDIAN_TURNOVER20,
        "top_fraction": TOP_FRACTION,
        "matched_control": {
            "same_signal_date": True,
            "same_pit_industry": True,
            "same_liquidity_quintile": True,
            "excluded_selected_cohort": True,
            "control_pool": "all eligible non-selected names in the quintile",
            "missing_fallback": None,
        },
    }
    model_hash = hashlib.sha256(
        json.dumps(parameters, sort_keys=True).encode()
    ).hexdigest()
    summary = {
        "schema_version": 1,
        "status": "completed_first_pass",
        "generated_at": datetime.now(UTC).isoformat(),
        "as_of_trade_date": AS_OF_TRADE_DATE,
        "2026_holdout_opened": False,
        "research_only": True,
        "protocol": protocol,
        "model": {
            "id": "fundamental_quality_first_pass_v0_1",
            "parameters": parameters,
            "parameters_sha256": model_hash,
            "implementation_sha256": _sha256(Path(__file__)),
        },
        "data": {
            "snapshot_manifest_status": snapshot_manifest["status"],
            "snapshot_manifest_sha256": _sha256(snapshot_manifest_path),
            "fundamental_snapshots_sha256": actual_snapshot_hash,
            "security_aliases_sha256": alias_hash,
            "trade_calendar_sha256": _sha256(calendar_path),
            "security_state_hashes": security_hashes,
            "benchmark_hashes": benchmark_hashes,
            "bar_cache": bar_cache,
            "effective_development_signal_end": development_cutoff,
            "monthly_signal_dates": len(signal_dates),
            "snapshot_audit": snapshot_audit,
            "snapshot_join_coverage": (
                snapshot_audit["symbols"] / len(listing_dates)
                if listing_dates else None
            ),
            "monthly_industry_join_coverage": {
                day: (
                    audit.get("eligible", 0)
                    / (
                        audit.get("eligible", 0)
                        + audit.get("missing_pit_industry", 0)
                    )
                    if (
                        audit.get("eligible", 0)
                        + audit.get("missing_pit_industry", 0)
                    )
                    else None
                )
                for day, audit in monthly_audits.items()
            },
            "monthly_eligibility_audit": monthly_audits,
        },
        "cashflow_quality_diagnostic": {
            "field": "operating_cashflow_to_profit",
            "included_in_score": False,
            "selected_candidates_with_value": cashflow_available,
            "selected_candidates": selected_total,
            "coverage": (
                cashflow_available / selected_total if selected_total else None
            ),
            "reason": (
                "The ratio can look positive when operating cashflow and net "
                "profit are both negative; raw signs are unavailable."
            ),
        },
        "variants": variants,
        "fundamental_risk_cohort": risk_result,
        "caveats": [
            "2026 final holdout was not opened or evaluated.",
            "The final 60 official trading days of 2024 are purged from training.",
            "The frozen 120-day listing-age threshold is interpreted as calendar days.",
            "Operating cashflow to profit is diagnostic only and never affects a score.",
            "Missing metrics, benchmarks, and matched controls are never zero-filled.",
            "Industry ranks and membership are point-in-time at each signal date.",
            "The 2025 OOS period can only accept or reject a horizon nominated in 2023-2024.",
            "The bottom-core cohort is hazard evidence only, not a short portfolio or risk gate.",
            "Historical security-state timestamps are session metadata, not exact publication timestamps.",
            "Valuation, parameter search, and automatic score-weight changes are disabled.",
        ],
    }
    output = project / "reports" / "fundamental_quality_validation"
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
        "2026_holdout_opened": False,
        "protocol_sha256": protocol["sha256"],
        "model_sha256": model_hash,
        "observations": len(observations),
        "report_json": str(json_path),
        "report_markdown": str(markdown_path),
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="基本面质量首轮冻结验证")
    parser.add_argument("--root", default=".")
    parser.add_argument("--as-of-trade-date", default=AS_OF_TRADE_DATE)
    args = parser.parse_args()
    if _date(args.as_of_trade_date) != AS_OF_TRADE_DATE:
        raise SystemExit(
            "基本面首轮验证冻结为 as_of_trade_date=20251231；禁止打开 2026"
        )
    result = run_validation(Path(args.root))
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()

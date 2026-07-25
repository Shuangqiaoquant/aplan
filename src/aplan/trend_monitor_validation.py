from __future__ import annotations

import argparse
import csv
import hashlib
import json
import math
import sqlite3
import tomllib
from collections import defaultdict, deque
from dataclasses import dataclass
from datetime import UTC, date, datetime
from pathlib import Path
from statistics import mean, median
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
    _quantile,
    _return,
    _sha256,
    _truthy,
)


AS_OF_TRADE_DATE = "20251231"
DEVELOPMENT_END = "20241231"
PROTOCOL_SHA256 = (
    "3c4fc72624e3802a3e02864c003cf502de05e8a355b4f0b4f9f112693c80a814"
)
HORIZONS = (1, 5, 10, 20, 40, 60)
VARIANTS = (
    "B1_volume_breakout",
    "B2_ma20_pullback",
    "B3_ma5_cross_ma20",
    "combined_top5",
)
OFFSETS = (0, 1, 2, 3, 4)
STEP_DAYS = 5
MINIMUM_LISTING_DAYS = 120
MINIMUM_AVERAGE_TURNOVER20 = 50_000_000.0
MINIMUM_OOS_OBSERVATIONS = 100
MINIMUM_OOS_COHORTS = 60
MINIMUM_STABILITY_RATIO = 0.60
MAXIMUM_DRAWDOWN = 0.25
TAIL_LOSS_THRESHOLD = -0.10
MATCH_COUNT = 5


@dataclass(frozen=True, slots=True)
class TechnicalCandidate:
    symbol: str
    signal_date: str
    offset: int
    signals: tuple[str, ...]
    score: float
    pre20_return: float
    average_turnover20: float
    industry_code: str = ""


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


def _protocol(project: Path) -> dict[str, Any]:
    path = project / "config" / "trend_monitor_validation.toml"
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
            "趋势监控冻结规范哈希不匹配："
            f"expected={PROTOCOL_SHA256} actual={digest}"
        )
    document = tomllib.loads(path.read_text(encoding="utf-8"))
    if document.get("final_holdout_opened") is not False:
        raise ValueError("趋势监控冻结规范必须保持 final_holdout_opened=false")
    if (
        _date(document.get("time_design", {}).get("research_as_of_trade_date"))
        != AS_OF_TRADE_DATE
    ):
        raise ValueError("趋势监控冻结规范 as_of_trade_date 已变化")
    if tuple(document.get("rebalance", {}).get("multiple_start_offsets", ())) != OFFSETS:
        raise ValueError("趋势监控冻结规范的五个调仓 offset 已变化")
    if set(document.get("variants", {})) != set(VARIANTS):
        raise ValueError("趋势监控冻结规范的规则轴已变化")
    return {"path": str(path), "sha256": digest, "document": document}


def _listing_dates(path: Path, aliases: dict[str, str]) -> dict[str, str]:
    output: dict[str, str] = {}
    with path.open(encoding="utf-8-sig", newline="") as handle:
        for row in csv.DictReader(handle):
            symbol = _canonical(str(row.get("symbol") or "").strip(), aliases)
            listed = _date(
                row.get("list_date")
                or row.get("listing_date")
                or row.get("listed_date")
            )
            if len(symbol) == 6 and listed:
                previous = output.get(symbol)
                output[symbol] = min(previous, listed) if previous else listed
    return output


def _days_listed(list_date: str, signal_date: str) -> int:
    return (
        date(
            int(signal_date[:4]),
            int(signal_date[4:6]),
            int(signal_date[6:8]),
        )
        - date(
            int(list_date[:4]),
            int(list_date[4:6]),
            int(list_date[6:8]),
        )
    ).days


def _technical_snapshot(
    symbol: str,
    signal_date: str,
    offset: int,
    history: Iterable[tuple[float, float, float, float]],
    *,
    open_price: float,
    close: float,
    low: float,
    volume: float,
) -> TechnicalCandidate | None:
    previous = list(history)
    if len(previous) < 60:
        return None
    prior_closes = [item[0] for item in previous]
    prior_highs = [item[1] for item in previous]
    prior_volumes = [item[2] for item in previous]
    prior_turnover = [item[3] for item in previous]
    ma5 = mean([*prior_closes[-4:], close])
    ma20 = mean([*prior_closes[-19:], close])
    ma60 = mean([*prior_closes[-59:], close])
    previous_ma5 = mean(prior_closes[-5:])
    previous_ma20 = mean(prior_closes[-20:])
    volume_baseline = mean(prior_volumes[-5:])
    volume_ratio = volume / volume_baseline if volume_baseline > 0 else 0.0
    breakout_reference = max(prior_highs[-20:])
    average_turnover20 = mean(prior_turnover[-20:])
    pre20_return = (
        prior_closes[-1] / prior_closes[-21] - 1
        if prior_closes[-21] > 0
        else 0.0
    )
    signals: list[str] = []
    if (
        close > breakout_reference
        and close >= ma20
        and ma20 >= previous_ma20
        and volume_ratio >= 1.5
    ):
        signals.append("B1_volume_breakout")
    if (
        close > ma20 > ma60
        and ma20 > previous_ma20
        and abs(low / ma20 - 1) <= 0.02
        and close >= open_price
    ):
        signals.append("B2_ma20_pullback")
    if previous_ma5 <= previous_ma20 and ma5 > ma20 and close > ma60:
        signals.append("B3_ma5_cross_ma20")
    if not signals:
        return None
    aligned = close > ma20 > ma60
    score = (
        45.0
        + (22.0 if "B1_volume_breakout" in signals else 0.0)
        + (18.0 if "B2_ma20_pullback" in signals else 0.0)
        + (15.0 if "B3_ma5_cross_ma20" in signals else 0.0)
        + (8.0 if aligned else 0.0)
        + min(7.0, max(0.0, (volume_ratio - 1.0) * 5.0))
    )
    return TechnicalCandidate(
        symbol=symbol,
        signal_date=signal_date,
        offset=offset,
        signals=tuple(signals),
        score=min(score, 95.0),
        pre20_return=pre20_return,
        average_turnover20=average_turnover20,
    )


def _scan_candidates(
    project: Path,
    dates: list[str],
    aliases: dict[str, str],
    security_database: Path,
    listing_dates: dict[str, str],
    industry_by_symbol: dict[str, list[tuple[str, str, str]]],
) -> tuple[dict[str, list[TechnicalCandidate]], dict[str, Any]]:
    paths = {
        path.stem: path
        for path in (
            project / "data" / "processed" / "yinhe_daily_qfq"
        ).glob("20??????.csv")
        if path.stem <= AS_OF_TRADE_DATE
    }
    histories: dict[
        str, deque[tuple[float, float, float, float]]
    ] = defaultdict(lambda: deque(maxlen=60))
    candidates: dict[str, list[TechnicalCandidate]] = defaultdict(list)
    counters: dict[str, int] = defaultdict(int)
    statuses = sqlite3.connect(security_database)
    statuses.row_factory = sqlite3.Row
    try:
        for date_index, signal_date in enumerate(dates):
            path = paths.get(signal_date)
            if path is None:
                continue
            day_rows: list[
                tuple[
                    str,
                    float,
                    float,
                    float,
                    float,
                    float,
                    float,
                    int,
                    int,
                    int,
                ]
            ] = []
            with path.open(encoding="utf-8-sig", newline="") as handle:
                for row in csv.DictReader(handle):
                    symbol = _canonical(
                        str(row.get("symbol") or "").strip(),
                        aliases,
                    )
                    close = _number(row.get("close"))
                    high = _number(row.get("high"))
                    low = _number(row.get("low"))
                    open_price = _number(row.get("open"))
                    volume = _number(row.get("volume"))
                    turnover = _number(row.get("turnover"))
                    if (
                        len(symbol) != 6
                        or None
                        in (close, high, low, open_price, volume, turnover)
                        or min(close, high, low, open_price) <= 0
                    ):
                        continue
                    day_rows.append(
                        (
                            symbol,
                            float(open_price),
                            float(close),
                            float(high),
                            float(low),
                            float(volume),
                            float(turnover),
                            _truthy(row.get("is_suspended")),
                            _truthy(row.get("is_limit_up")),
                            _truthy(row.get("is_limit_down")),
                        )
                    )
            technical: list[TechnicalCandidate] = []
            for (
                symbol,
                open_price,
                close,
                high,
                low,
                volume,
                turnover,
                suspended,
                limit_up,
                limit_down,
            ) in day_rows:
                counters["rows_scanned"] += 1
                if not (suspended or limit_up or limit_down):
                    candidate = _technical_snapshot(
                        symbol,
                        signal_date,
                        date_index % STEP_DAYS,
                        histories[symbol],
                        open_price=open_price,
                        close=close,
                        low=low,
                        volume=volume,
                    )
                    if candidate is not None:
                        counters["technical_triggers"] += 1
                        if (
                            candidate.average_turnover20
                            >= MINIMUM_AVERAGE_TURNOVER20
                        ):
                            technical.append(candidate)
                        else:
                            counters["below_liquidity_threshold"] += 1
                histories[symbol].append((close, high, volume, turnover))
            for candidate in technical:
                state = statuses.execute(
                    "SELECT is_st, is_suspended FROM daily_status "
                    "WHERE trade_date=? AND symbol=?",
                    (signal_date, candidate.symbol),
                ).fetchone()
                if not state:
                    counters["missing_signal_security_state"] += 1
                    continue
                counters["signal_security_state_joined"] += 1
                if state["is_st"]:
                    counters["excluded_st"] += 1
                    continue
                if state["is_suspended"]:
                    counters["excluded_signal_suspended"] += 1
                    continue
                listed = listing_dates.get(candidate.symbol)
                if (
                    not listed
                    or _days_listed(listed, signal_date)
                    < MINIMUM_LISTING_DAYS
                ):
                    counters["excluded_listing_age"] += 1
                    continue
                industry = _industry_at(
                    candidate.symbol,
                    signal_date,
                    industry_by_symbol,
                )
                if not industry:
                    counters["missing_signal_industry"] += 1
                candidates[signal_date].append(
                    TechnicalCandidate(
                        symbol=candidate.symbol,
                        signal_date=candidate.signal_date,
                        offset=candidate.offset,
                        signals=candidate.signals,
                        score=candidate.score,
                        pre20_return=candidate.pre20_return,
                        average_turnover20=candidate.average_turnover20,
                        industry_code=industry,
                    )
                )
            if (date_index + 1) % 100 == 0 or date_index + 1 == len(dates):
                print(
                    f"趋势信号扫描：{date_index + 1}/{len(dates)}，"
                    f"eligible={sum(map(len, candidates.values()))}",
                    flush=True,
                )
    finally:
        statuses.close()
    counters["eligible_signal_grains"] = sum(map(len, candidates.values()))
    counters["signal_security_state_join_coverage"] = (
        counters["signal_security_state_joined"]
        / (
            counters["signal_security_state_joined"]
            + counters["missing_signal_security_state"]
        )
        if (
            counters["signal_security_state_joined"]
            + counters["missing_signal_security_state"]
        )
        else None
    )
    return candidates, dict(counters)


def _variant_candidates(
    day_candidates: list[TechnicalCandidate],
) -> dict[str, list[TechnicalCandidate]]:
    output = {
        variant: [
            item for item in day_candidates if variant in item.signals
        ]
        for variant in VARIANTS[:-1]
    }
    output["combined_top5"] = sorted(
        day_candidates,
        key=lambda item: (-item.score, item.symbol),
    )[:5]
    return output


def _matched_controls(
    bars: sqlite3.Connection,
    statuses: sqlite3.Connection,
    candidate: TechnicalCandidate,
    entry_date: str,
    exit_dates: dict[int, str],
    industry_members: dict[str, list[str]],
    industry_by_symbol: dict[str, list[tuple[str, str, str]]],
    excluded: set[str],
    listing_dates: dict[str, str],
) -> dict[int, float]:
    if not candidate.industry_code:
        return {}
    members = [
        symbol
        for symbol in industry_members.get(candidate.industry_code, [])
        if symbol not in excluded
        and _industry_at(
            symbol,
            candidate.signal_date,
            industry_by_symbol,
        )
        == candidate.industry_code
    ]
    if not members:
        return {}
    rows = []
    for start in range(0, len(members), 800):
        group = members[start : start + 800]
        placeholders = ",".join("?" for _ in group)
        rows.extend(
            bars.execute(
                f"SELECT symbol,pre20_return,median_turnover20,"
                f"is_suspended,is_limit_up,high,low FROM bars "
                f"WHERE trade_date=? AND symbol IN ({placeholders}) "
                "AND pre20_return IS NOT NULL "
                "AND median_turnover20>=?",
                (
                    candidate.signal_date,
                    *group,
                    MINIMUM_AVERAGE_TURNOVER20,
                ),
            ).fetchall()
        )
    scored: list[tuple[float, str]] = []
    source_turnover = candidate.average_turnover20
    for row in rows:
        symbol = str(row["symbol"])
        signal_state = statuses.execute(
            "SELECT is_st,is_suspended FROM daily_status "
            "WHERE trade_date=? AND symbol=?",
            (candidate.signal_date, symbol),
        ).fetchone()
        if (
            not signal_state
            or signal_state["is_st"]
            or signal_state["is_suspended"]
            or row["is_suspended"]
            or (
                row["is_limit_up"]
                and float(row["high"]) == float(row["low"])
            )
        ):
            continue
        listed = listing_dates.get(symbol)
        if (
            not listed
            or _days_listed(listed, candidate.signal_date)
            < MINIMUM_LISTING_DAYS
        ):
            continue
        distance = (
            abs(float(row["pre20_return"]) - candidate.pre20_return) / 0.05
            + abs(
                math.log(
                    float(row["median_turnover20"]) / source_turnover
                )
            )
        )
        scored.append((distance, symbol))
    selected: list[str] = []
    for _, symbol in sorted(scored):
        state = statuses.execute(
            "SELECT is_st,is_suspended FROM daily_status "
            "WHERE trade_date=? AND symbol=?",
            (entry_date, symbol),
        ).fetchone()
        entry = bars.execute(
            "SELECT * FROM bars WHERE trade_date=? AND symbol=?",
            (entry_date, symbol),
        ).fetchone()
        if (
            not state
            or state["is_st"]
            or state["is_suspended"]
            or not entry
            or entry["is_suspended"]
            or (entry["is_limit_up"] and entry["high"] == entry["low"])
        ):
            continue
        selected.append(symbol)
        if len(selected) == MATCH_COUNT:
            break
    result: dict[int, list[float]] = defaultdict(list)
    for symbol in selected:
        entry = bars.execute(
            "SELECT open FROM bars WHERE trade_date=? AND symbol=?",
            (entry_date, symbol),
        ).fetchone()
        for horizon, exit_date in exit_dates.items():
            exit_row = bars.execute(
                "SELECT close FROM bars WHERE trade_date=? AND symbol=?",
                (exit_date, symbol),
            ).fetchone()
            if entry and exit_row:
                _, net = _return(float(entry["open"]), float(exit_row["close"]))
                result[horizon].append(net)
    return {
        horizon: mean(values)
        for horizon, values in result.items()
        if values
    }


def _evaluate(
    bar_database: Path,
    security_database: Path,
    dates: list[str],
    candidates: dict[str, list[TechnicalCandidate]],
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
    counters: dict[str, int] = defaultdict(int)
    try:
        for day_number, signal_date in enumerate(dates, 1):
            signal_index = date_index[signal_date]
            if signal_index + 1 >= len(dates):
                continue
            entry_date = dates[signal_index + 1]
            by_variant = _variant_candidates(candidates.get(signal_date, []))
            non_signal_exclusions = {
                item.symbol for item in candidates.get(signal_date, [])
            }
            exit_dates = {
                horizon: dates[signal_index + horizon]
                for horizon in HORIZONS
                if signal_index + horizon < len(dates)
            }
            for variant, selected in by_variant.items():
                counters[f"{variant}_signals"] += len(selected)
                for candidate in selected:
                    state = statuses.execute(
                        "SELECT is_st,is_suspended FROM daily_status "
                        "WHERE trade_date=? AND symbol=?",
                        (entry_date, candidate.symbol),
                    ).fetchone()
                    if not state:
                        counters["missing_entry_security_state"] += 1
                        continue
                    counters["entry_security_state_joined"] += 1
                    entry = bars.execute(
                        "SELECT * FROM bars WHERE trade_date=? AND symbol=?",
                        (entry_date, candidate.symbol),
                    ).fetchone()
                    if (
                        state["is_st"]
                        or state["is_suspended"]
                        or not entry
                        or entry["is_suspended"]
                    ):
                        counters["untradeable_suspended_or_st"] += 1
                        continue
                    if entry["is_limit_up"] and entry["high"] == entry["low"]:
                        counters["untradeable_one_price_limit_up"] += 1
                        continue
                    controls = _matched_controls(
                        bars,
                        statuses,
                        candidate,
                        entry_date,
                        exit_dates,
                        industry_members,
                        industry_by_symbol,
                        non_signal_exclusions,
                        listing_dates,
                    )
                    for horizon, exit_date in exit_dates.items():
                        exit_row = bars.execute(
                            "SELECT close FROM bars "
                            "WHERE trade_date=? AND symbol=?",
                            (exit_date, candidate.symbol),
                        ).fetchone()
                        if not exit_row:
                            counters["missing_exit_bar"] += 1
                            continue
                        gross, net = _return(
                            float(entry["open"]),
                            float(exit_row["close"]),
                        )
                        market_return = _benchmark_return(
                            market,
                            MARKET_CODE,
                            entry_date,
                            exit_date,
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
                        matched_return = controls.get(horizon)
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
                                    (1 + net) / (1 + matched_return) - 1
                                    if matched_return is not None
                                    and matched_return > -1
                                    else None
                                ),
                                market_regime=regimes.get(
                                    signal_date,
                                    "insufficient",
                                ),
                            )
                        )
            if day_number % 100 == 0 or day_number == len(dates):
                print(
                    f"趋势冻结验证：{day_number}/{len(dates)}，"
                    f"observations={len(observations)}",
                    flush=True,
                )
    finally:
        bars.close()
        statuses.close()
    total_entry = (
        counters["entry_security_state_joined"]
        + counters["missing_entry_security_state"]
    )
    total_signals = sum(counters[f"{item}_signals"] for item in VARIANTS)
    counters["entry_security_state_join_coverage"] = (
        counters["entry_security_state_joined"] / total_entry
        if total_entry
        else None
    )
    counters["untradeable_rate"] = (
        (
            counters["untradeable_suspended_or_st"]
            + counters["untradeable_one_price_limit_up"]
        )
        / total_signals
        if total_signals
        else None
    )
    return observations, dict(counters)


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
    by_path: dict[int, dict[str, set[str]]] = defaultdict(
        lambda: defaultdict(set)
    )
    for row in rows:
        by_path[row.offset][row.signal_date].add(row.symbol)
    changes: list[float] = []
    for cohorts in by_path.values():
        previous: set[str] | None = None
        for day in sorted(cohorts):
            current = cohorts[day]
            if previous is not None and (previous or current):
                overlap = len(previous & current)
                changes.append(
                    1 - overlap / max(len(previous), len(current), 1)
                )
            previous = current
    return mean(changes) if changes else None


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
    offsets = {
        str(offset): {
            "observations": len(items),
            "cohorts": len({item.signal_date for item in items}),
            "median_matched_excess": (
                median(
                    item.matched_excess
                    for item in items
                    if item.matched_excess is not None
                )
                if any(item.matched_excess is not None for item in items)
                else None
            ),
        }
        for offset in OFFSETS
        if (items := [row for row in rows if row.offset == offset])
    }
    regimes: dict[str, Any] = {}
    for regime in sorted({row.market_regime for row in rows}):
        values = [
            row.matched_excess
            for row in rows
            if row.market_regime == regime and row.matched_excess is not None
        ]
        regimes[regime] = _distribution(values)
    return {
        "status": "ok",
        "observations": len(rows),
        "signals": len({(row.signal_date, row.symbol) for row in rows}),
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
        "offsets": offsets,
        "market_regimes": regimes,
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
) -> float | None:
    relevant = [row for row in rows if row.matched_excess is not None]
    decisions: list[bool] = []
    for start in range(0, len(date_index), 21):
        values = [
            row.matched_excess
            for row in relevant
            if start <= date_index.get(row.signal_date, -1) < start + 63
        ]
        if len(values) >= 20:
            decisions.append(median(values) > 0)
    return sum(decisions) / len(decisions) if decisions else None


def _regime_ratio(rows: list[Observation]) -> float | None:
    grouped: dict[str, list[float]] = defaultdict(list)
    for row in rows:
        if (
            row.matched_excess is not None
            and row.market_regime != "insufficient"
        ):
            grouped[row.market_regime].append(row.matched_excess)
    eligible = [values for values in grouped.values() if len(values) >= 30]
    if not eligible:
        return None
    return sum(median(values) > 0 for values in eligible) / len(eligible)


def _gate(
    rows: list[Observation],
    date_index: dict[str, int],
) -> dict[str, Any]:
    metrics = _stats(rows)
    rolling = _rolling_ratio(rows, date_index)
    regime = _regime_ratio(rows)
    metrics["positive_rolling_window_ratio"] = rolling
    metrics["positive_market_regime_ratio"] = regime
    survived = bool(
        metrics.get("observations", 0) >= MINIMUM_OOS_OBSERVATIONS
        and metrics.get("cohorts", 0) >= MINIMUM_OOS_COHORTS
        and metrics.get("median_net_return") is not None
        and metrics["median_net_return"] > 0
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


def _nominate_horizon(
    development: dict[int, dict[str, Any]],
) -> int | None:
    survivors = [
        (horizon, result)
        for horizon, result in development.items()
        if result["gate_decision"] == "survive_first_pass"
    ]
    if not survivors:
        return None
    return max(
        survivors,
        key=lambda item: (
            item[1]["metrics"].get("median_matched_excess") or 0.0,
            -item[0],
        ),
    )[0]


def _finalize_oos(
    development: dict[str, Any],
    rolling_oos: dict[str, Any],
    *,
    nominated: bool,
) -> dict[str, Any]:
    output = dict(rolling_oos)
    supported = rolling_oos["gate_decision"] == "survive_first_pass"
    output["training_nominated"] = nominated
    output["oos_supported"] = supported
    output["oos_cannot_self_nominate"] = True
    if not nominated or not supported:
        output["gate_decision"] = "reject"
    output["development_gate_decision"] = development["gate_decision"]
    return output


def _report_markdown(summary: dict[str, Any]) -> str:
    lines = [
        "# 趋势监控首轮冻结验证",
        "",
        f"- 状态：`{summary['status']}`",
        f"- 研究截止：`{summary['as_of_trade_date']}`",
        f"- 有效训练截止：`{summary['effective_development_signal_end']}`",
        f"- 冻结规范：`{summary['protocol']['sha256']}`",
        f"- 模型参数：`{summary['model']['parameters_sha256']}`",
        f"- 实现哈希：`{summary['model']['implementation_sha256']}`",
        f"- 信号实现：`{summary['model']['signal_implementation_sha256']}`",
        "- 2026 最终留出集：`sealed`",
        "",
        "| 规则轴 | 训练提名周期 | 2025 最终结论 |",
        "|---|---:|---|",
    ]
    for variant, result in summary["variants"].items():
        nominated = result["training_nomination"]["horizon"]
        decision = (
            result["horizons"][str(nominated)]["rolling_oos"][
                "gate_decision"
            ]
            if nominated is not None
            else "reject"
        )
        lines.append(f"| {variant} | {nominated or '-'} | {decision} |")
    lines.extend(
        [
            "",
            "## 全周期结果",
            "",
            "| 规则轴 | 周期 | 训练门控 | 训练提名 | 2025门控 | 样本 | 中位净收益 | 中位市场/行业/匹配超额 |",
            "|---|---:|---|---|---|---:|---:|---|",
        ]
    )
    fmt = lambda value: "NA" if value is None else f"{value:.2%}"
    for variant, result in summary["variants"].items():
        for horizon, values in result["horizons"].items():
            development = values["development"]
            oos = values["rolling_oos"]
            metrics = oos["metrics"]
            lines.append(
                f"| {variant} | {horizon} | "
                f"{development['gate_decision']} | "
                f"{str(oos['training_nominated']).lower()} | "
                f"{oos['gate_decision']} | "
                f"{metrics.get('observations', 0)} | "
                f"{fmt(metrics.get('median_net_return'))} | "
                f"{fmt(metrics.get('median_market_excess'))} / "
                f"{fmt(metrics.get('median_industry_excess'))} / "
                f"{fmt(metrics.get('median_matched_excess'))} |"
            )
    lines.extend(
        [
            "",
            "## 覆盖与限制",
            "",
            f"- 合格信号粒度：{summary['data']['scan_audit'].get('eligible_signal_grains', 0)}",
            f"- 信号日证券状态覆盖：{fmt(summary['data']['scan_audit'].get('signal_security_state_join_coverage'))}",
            f"- 入场证券状态覆盖：{fmt(summary['data']['evaluation_audit'].get('entry_security_state_join_coverage'))}",
            f"- 不可交易率：{fmt(summary['data']['evaluation_audit'].get('untradeable_rate'))}",
            "",
        ]
    )
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
        project,
        aliases,
        alias_hash,
    )
    dates = [day for day in dates if "20230101" <= day <= AS_OF_TRADE_DATE]
    if any(day > AS_OF_TRADE_DATE for day in dates):
        raise AssertionError("2026 行情进入趋势冻结验证")
    calendar_path = project / "data" / "processed" / "trade_calendar.csv"
    official_calendar = [
        day
        for day in _load_calendar(calendar_path)
        if "20230101" <= day <= AS_OF_TRADE_DATE
    ]
    if dates != official_calendar:
        missing = sorted(set(official_calendar) - set(dates))
        raise ValueError(
            "银河行情与官方交易日历不一致；"
            f"missing={missing[:10]} count={len(missing)}"
        )
    development_cutoff = _purged_development_cutoff(
        dates,
        DEVELOPMENT_END,
        60,
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
    candidates, scan_audit = _scan_candidates(
        project,
        dates,
        aliases,
        security_database,
        listing_dates,
        industry_by_symbol,
    )
    observations, evaluation_audit = _evaluate(
        bar_database,
        security_database,
        dates,
        candidates,
        market,
        industry_daily,
        industry_by_symbol,
        industry_members,
        listing_dates,
    )
    date_index = {day: index for index, day in enumerate(dates)}
    variants: dict[str, Any] = {}
    for variant in VARIANTS:
        development: dict[int, dict[str, Any]] = {}
        oos_raw: dict[int, dict[str, Any]] = {}
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
        "variants": VARIANTS,
        "horizons": HORIZONS,
        "rebalance": {
            "step_trading_days": STEP_DAYS,
            "offsets": OFFSETS,
        },
        "signals": {
            "breakout_lookback": 20,
            "volume_baseline_days": 5,
            "minimum_volume_ratio": 1.5,
            "pullback_tolerance": 0.02,
        },
        "combined_top5": {
            "candidate_count": 5,
            "score_base": 45,
            "score_B1": 22,
            "score_B2": 18,
            "score_B3": 15,
            "score_trend_alignment": 8,
            "score_volume_confirmation_max": 7,
            "ranking": "score_desc_then_symbol",
        },
        "eligibility": {
            "minimum_listing_calendar_days": MINIMUM_LISTING_DAYS,
            "minimum_average_turnover_previous_20d": (
                MINIMUM_AVERAGE_TURNOVER20
            ),
            "exclude_st": True,
            "exclude_suspended_signal": True,
            "exclude_limit_up_or_down_signal": True,
        },
        "entry": "next_trading_day_open",
        "costs": {
            "commission_each_side": 0.0003,
            "slippage_each_side": 0.001,
            "sell_stamp_tax": 0.0005,
        },
        "matched_control": {
            "same_signal_date": True,
            "same_pit_industry": True,
            "similar_pre20_return_and_turnover": True,
            "exclude_any_frozen_trend_signal": True,
            "maximum_controls": MATCH_COUNT,
            "missing_fallback": None,
        },
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
            "id": "trend_monitor_v0_1",
            "parameters": parameters,
            "parameters_sha256": parameters_hash,
            "implementation_sha256": _sha256(Path(__file__)),
            "signal_implementation_sha256": _sha256(
                Path(__file__).with_name("trend_monitor.py")
            ),
        },
        "data": {
            "security_aliases_sha256": alias_hash,
            "trade_calendar_sha256": _sha256(calendar_path),
            "security_state_hashes": security_hashes,
            "benchmark_hashes": benchmark_hashes,
            "bar_cache": bar_cache,
            "files_evaluated": len(dates),
            "first_date": dates[0],
            "last_date": dates[-1],
            "2026_rows_evaluated": 0,
            "scan_audit": scan_audit,
            "evaluation_audit": evaluation_audit,
        },
        "variants": variants,
        "caveats": [
            "2026 final holdout was not opened, scanned, or evaluated.",
            "The final 60 official trading days of 2024 are purged from training.",
            "Each B1/B2/B3 axis is gated independently; combined_top5 cannot mask an axis.",
            "The five offsets are retained as five separate five-trading-day paths.",
            "The 2025 OOS period can only accept or reject a horizon nominated in training.",
            "Missing official benchmarks, PIT industries, or matched controls are never zero-filled.",
            "Signal-day and entry-day tradability are checked separately.",
            "The 120-day listing threshold uses calendar days to match the frozen current model.",
            "No fundamentals, announcements, news, stop loss, or parameter search are used.",
        ],
    }
    output = project / "reports" / "trend_monitor_validation"
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
        "observations": len(observations),
        "gate_decisions": {
            variant: {
                "training_nomination": result["training_nomination"]["horizon"],
                "oos": (
                    result["horizons"][
                        str(result["training_nomination"]["horizon"])
                    ]["rolling_oos"]["gate_decision"]
                    if result["training_nomination"]["horizon"] is not None
                    else "reject"
                ),
            }
            for variant, result in variants.items()
        },
        "report_json": str(json_path),
        "report_markdown": str(markdown_path),
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="趋势监控首轮冻结验证")
    parser.add_argument("--root", default=".")
    parser.add_argument(
        "--as-of-trade-date",
        default=AS_OF_TRADE_DATE,
    )
    args = parser.parse_args()
    if _date(args.as_of_trade_date) != AS_OF_TRADE_DATE:
        raise SystemExit(
            "趋势监控首轮验证冻结为 as_of_trade_date=20251231；禁止打开 2026"
        )
    print(
        json.dumps(
            run_validation(Path(args.root)),
            ensure_ascii=False,
            indent=2,
        )
    )


if __name__ == "__main__":
    main()

from __future__ import annotations

import csv
import json
import math
from collections import defaultdict, deque
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from statistics import mean, median, pstdev
from typing import Any

from .factors import percentile_ranks
from .validation_protocol import load_protocol


@dataclass(slots=True)
class PriceHistory:
    closes: deque[float] = field(default_factory=lambda: deque(maxlen=121))
    returns: deque[float] = field(default_factory=lambda: deque(maxlen=20))
    turnovers: deque[float] = field(default_factory=lambda: deque(maxlen=20))

    def update(self, close: float, turnover: float) -> None:
        if self.closes:
            self.returns.append(close / self.closes[-1] - 1)
        self.closes.append(close)
        self.turnovers.append(turnover)


@dataclass(frozen=True, slots=True)
class BaselineSignal:
    configuration: str
    horizon: int
    offset: int
    signal_index: int
    signal_date: str
    symbol: str
    rank: int
    score: float


@dataclass(slots=True)
class OpenPosition:
    signal: BaselineSignal
    entry_index: int
    entry_date: str
    entry_price: float
    target_exit_index: int


@dataclass(frozen=True, slots=True)
class BaselineResult:
    configuration: str
    horizon: int
    offset: int
    signal_date: str
    symbol: str
    entry_date: str
    exit_date: str
    gross_return: float
    net_return: float
    benchmark_return: float
    net_excess_return: float
    transaction_cost: float


def _date_key(value: str) -> str:
    return "".join(character for character in value if character.isdigit())[:8]


def _daily_paths(daily_dir: Path, start: str, end: str) -> list[Path]:
    start_key = _date_key(start)
    end_key = _date_key(end)
    return [
        path
        for path in sorted(daily_dir.glob("20??????.csv"))
        if start_key <= path.stem <= end_key
    ]


def _read_day(path: Path) -> list[dict[str, str]]:
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        return list(csv.DictReader(handle))


def _truthy(value: Any) -> bool:
    return str(value or "").strip().lower() in {"1", "true", "yes"}


def _offsets(horizon: int) -> tuple[int, ...]:
    return tuple(sorted({0, horizon // 4, horizon // 2, 3 * horizon // 4}))


def _score(
    histories: dict[str, PriceHistory],
    *,
    momentum_days: int,
    min_history: int,
    min_avg_turnover: float,
    top_n: int,
) -> list[tuple[str, float]]:
    raw: dict[str, tuple[float, float, float]] = {}
    for symbol, history in histories.items():
        if len(history.closes) < min_history + 1 or len(history.turnovers) < 20:
            continue
        if mean(history.turnovers) < min_avg_turnover:
            continue
        momentum = history.closes[-1] / history.closes[-momentum_days - 1] - 1
        volatility = pstdev(history.returns) * math.sqrt(252) if len(history.returns) > 1 else 0
        turnovers = list(history.turnovers)
        old_turnover = mean(turnovers[:10])
        new_turnover = mean(turnovers[10:])
        turnover_trend = new_turnover / old_turnover - 1 if old_turnover else 0
        raw[symbol] = (momentum, volatility, turnover_trend)
    if not raw:
        return []
    momentum_rank = percentile_ranks({symbol: values[0] for symbol, values in raw.items()})
    low_vol_rank = percentile_ranks(
        {symbol: values[1] for symbol, values in raw.items()},
        higher_is_better=False,
    )
    turnover_rank = percentile_ranks({symbol: values[2] for symbol, values in raw.items()})
    scored = {
        symbol: 100
        * (
            0.55 * momentum_rank[symbol]
            + 0.25 * low_vol_rank[symbol]
            + 0.20 * turnover_rank[symbol]
        )
        for symbol in raw
    }
    return sorted(scored.items(), key=lambda item: (-item[1], item[0]))[:top_n]


def generate_signals(
    paths: list[Path],
    *,
    horizons: tuple[int, ...],
    top_n: int,
    min_history: int,
    momentum_days: int,
    min_avg_turnover: float,
) -> tuple[list[BaselineSignal], list[float]]:
    configurations = [
        (horizon, offset)
        for horizon in horizons
        for offset in _offsets(horizon)
    ]
    next_signal = {
        configuration: min_history - 1 + configuration[1]
        for configuration in configurations
    }
    histories: dict[str, PriceHistory] = defaultdict(PriceHistory)
    previous_open: dict[str, float] = {}
    market_proxy_returns: list[float] = []
    signals: list[BaselineSignal] = []

    for day_index, path in enumerate(paths):
        proxy_returns: list[float] = []
        for row in _read_day(path):
            symbol = str(row.get("symbol") or "")
            close = float(row.get("close") or 0)
            open_price = float(row.get("open") or 0)
            turnover = float(row.get("turnover") or 0)
            if len(symbol) != 6 or close <= 0 or open_price <= 0:
                continue
            prior_open = previous_open.get(symbol)
            if prior_open and not _truthy(row.get("is_suspended")):
                value = open_price / prior_open - 1
                if abs(value) <= 0.35:
                    proxy_returns.append(value)
            previous_open[symbol] = open_price
            histories[symbol].update(close, turnover)
        market_proxy_returns.append(mean(proxy_returns) if proxy_returns else 0.0)

        due = [
            configuration
            for configuration, index in next_signal.items()
            if day_index == index
        ]
        if not due:
            continue
        scored = _score(
            histories,
            momentum_days=momentum_days,
            min_history=min_history,
            min_avg_turnover=min_avg_turnover,
            top_n=top_n,
        )
        for horizon, offset in due:
            configuration = f"{horizon}d_offset_{offset}"
            for rank, (symbol, score) in enumerate(scored, 1):
                signals.append(
                    BaselineSignal(
                        configuration=configuration,
                        horizon=horizon,
                        offset=offset,
                        signal_index=day_index,
                        signal_date=path.stem,
                        symbol=symbol,
                        rank=rank,
                        score=score,
                    )
                )
            next_signal[(horizon, offset)] += horizon
    return signals, market_proxy_returns


def _benchmark_return(market_returns: list[float], entry_index: int, exit_index: int) -> float:
    growth = 1.0
    for value in market_returns[entry_index + 1 : exit_index + 1]:
        growth *= 1 + value
    return growth - 1


def evaluate_signals(
    paths: list[Path],
    signals: list[BaselineSignal],
    market_returns: list[float],
    *,
    commission_rate: float,
    stamp_tax_rate: float,
    slippage_rate: float,
) -> list[BaselineResult]:
    entries: dict[int, list[BaselineSignal]] = defaultdict(list)
    for signal in signals:
        entries[signal.signal_index + 1].append(signal)
    active: list[OpenPosition] = []
    results: list[BaselineResult] = []

    for day_index, path in enumerate(paths):
        rows = {str(row.get("symbol") or ""): row for row in _read_day(path)}
        remaining: list[OpenPosition] = []
        for position in active:
            if day_index < position.target_exit_index:
                remaining.append(position)
                continue
            row = rows.get(position.signal.symbol)
            if (
                row is None
                or _truthy(row.get("is_suspended"))
                or _truthy(row.get("is_limit_down"))
            ):
                remaining.append(position)
                continue
            exit_price = float(row.get("open") or 0)
            if exit_price <= 0:
                remaining.append(position)
                continue
            gross = exit_price / position.entry_price - 1
            net_growth = (
                (1 + gross)
                * (1 - slippage_rate)
                * (1 - commission_rate)
                * (1 - slippage_rate)
                * (1 - commission_rate - stamp_tax_rate)
            )
            benchmark = _benchmark_return(market_returns, position.entry_index, day_index)
            net = net_growth - 1
            results.append(
                BaselineResult(
                    configuration=position.signal.configuration,
                    horizon=position.signal.horizon,
                    offset=position.signal.offset,
                    signal_date=position.signal.signal_date,
                    symbol=position.signal.symbol,
                    entry_date=position.entry_date,
                    exit_date=path.stem,
                    gross_return=gross,
                    net_return=net,
                    benchmark_return=benchmark,
                    net_excess_return=(1 + net) / (1 + benchmark) - 1,
                    transaction_cost=gross - net,
                )
            )
        active = remaining

        for signal in entries.get(day_index, []):
            row = rows.get(signal.symbol)
            if (
                row is None
                or _truthy(row.get("is_suspended"))
                or _truthy(row.get("is_limit_up"))
            ):
                continue
            entry_price = float(row.get("open") or 0)
            if entry_price <= 0:
                continue
            active.append(
                OpenPosition(
                    signal=signal,
                    entry_index=day_index,
                    entry_date=path.stem,
                    entry_price=entry_price,
                    target_exit_index=day_index + signal.horizon,
                )
            )
    return results


def _quantile(values: list[float], probability: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    position = (len(ordered) - 1) * probability
    lower = math.floor(position)
    upper = math.ceil(position)
    if lower == upper:
        return ordered[lower]
    return ordered[lower] + (ordered[upper] - ordered[lower]) * (position - lower)


def _metrics(results: list[BaselineResult]) -> dict[str, Any]:
    if not results:
        return {"trades": 0, "cohorts": 0, "status": "insufficient_data"}
    by_cohort: dict[str, list[BaselineResult]] = defaultdict(list)
    for result in results:
        by_cohort[result.signal_date].append(result)
    cohort_returns = [
        mean(item.net_return for item in by_cohort[day])
        for day in sorted(by_cohort)
    ]
    cohort_excess = [
        mean(item.net_excess_return for item in by_cohort[day])
        for day in sorted(by_cohort)
    ]
    equity = 1.0
    peak = 1.0
    max_drawdown = 0.0
    for value in cohort_returns:
        equity *= 1 + value
        peak = max(peak, equity)
        max_drawdown = min(max_drawdown, equity / peak - 1)
    net_values = [item.net_return for item in results]
    excess_values = [item.net_excess_return for item in results]
    return {
        "status": "completed",
        "trades": len(results),
        "cohorts": len(cohort_returns),
        "mean_net_return": mean(net_values),
        "median_net_return": median(net_values),
        "win_rate": sum(value > 0 for value in net_values) / len(net_values),
        "mean_net_excess_return": mean(excess_values),
        "median_net_excess_return": median(excess_values),
        "excess_win_rate": sum(value > 0 for value in excess_values) / len(excess_values),
        "max_drawdown": max_drawdown,
        "mean_transaction_cost": mean(item.transaction_cost for item in results),
        "turnover_rate_proxy": 1.0,
        "distribution": {
            "p05": _quantile(net_values, 0.05),
            "p25": _quantile(net_values, 0.25),
            "p50": _quantile(net_values, 0.50),
            "p75": _quantile(net_values, 0.75),
            "p95": _quantile(net_values, 0.95),
            "tail_loss_rate": sum(value <= -0.10 for value in net_values) / len(net_values),
        },
        "cohort_mean_net_return": mean(cohort_returns),
        "cohort_mean_net_excess_return": mean(cohort_excess),
    }


def _period_results(
    results: list[BaselineResult],
    start: str,
    end: str,
) -> list[BaselineResult]:
    return [result for result in results if start <= result.signal_date <= end]


def _rolling_metrics(
    results: list[BaselineResult],
    dates: list[str],
    *,
    start: str,
    end: str,
    test_days: int,
    step_days: int,
) -> list[dict[str, Any]]:
    period_dates = [day for day in dates if start <= day <= end]
    windows: list[dict[str, Any]] = []
    for offset in range(0, max(len(period_dates) - test_days + 1, 0), step_days):
        window_dates = period_dates[offset : offset + test_days]
        if len(window_dates) < test_days:
            continue
        metrics = _metrics(
            _period_results(results, window_dates[0], window_dates[-1])
        )
        windows.append(
            {
                "start": window_dates[0],
                "end": window_dates[-1],
                **metrics,
            }
        )
    return windows


def _render_report(document: dict[str, Any]) -> str:
    lines = [
        "# 银河纯量价暂定基线",
        "",
        f"- 状态：`{document['status']}`",
        f"- 数据版本：`{document.get('data_version') or 'unknown'}`",
        f"- 数据区间：{document['data_profile']['first_date']} 至 {document['data_profile']['last_date']}",
        f"- 信号数量：{document['data_profile']['signals']}",
        f"- 成交结果：{document['data_profile']['results']}",
        "",
        "> 当前使用未复权价格与等权市场代理，只可用于研究管线校验，不可作为严格回测或交易依据。",
        "",
        "| 周期 | 训练期起点通过 | 训练期中位超额 | 2025样本外起点通过 | 2025样本外中位超额 |",
        "|---:|---:|---:|---:|---:|",
    ]
    for record in document["horizons"]:
        lines.append(
            f"| {record['horizon']}日 | {record['development']['positive_offsets']}/"
            f"{record['offset_count']} | {record['development']['median_offset_excess']:.2%} | "
            f"{record['rolling_oos']['positive_offsets']}/{record['offset_count']} | "
            f"{record['rolling_oos']['median_offset_excess']:.2%} |"
        )
    lines.extend(
        [
            "",
            "## 选择规则",
            "",
            f"训练期选择：`{document.get('selected_horizon')}`。"
            f"2025 样本外结论：`{document['selection_validation']['decision']}`。"
            "样本外只接受或拒绝训练期选择，不用于改选。",
            "",
            "## 阻塞项",
            "",
        ]
    )
    lines.extend(f"- `{item}`" for item in document["blocked_checks"])
    return "\n".join(lines) + "\n"


def run_yinhe_price_baseline(
    project: Path,
    *,
    start_date: str = "20230101",
    end_date: str = "20260722",
    open_final_holdout: bool = False,
    output_dir: Path | None = None,
) -> dict[str, Any]:
    project = project.resolve()
    loaded_protocol = load_protocol(project)
    protocol = loaded_protocol["document"]
    baseline = protocol["baseline"]
    time_design = protocol["time_design"]
    costs = protocol["costs"]
    horizons = tuple(int(value) for value in protocol["horizons"]["evaluation_days"])
    daily_dir = project / "data" / "processed" / "yinhe_daily"
    paths = _daily_paths(daily_dir, start_date, end_date)
    if not paths:
        raise ValueError(f"未找到银河日线 CSV：{daily_dir}")

    acceptance_path = project / "reports" / "yinhe_acceptance" / "latest.json"
    acceptance = (
        json.loads(acceptance_path.read_text(encoding="utf-8"))
        if acceptance_path.exists()
        else {}
    )
    if not (acceptance.get("readiness") or {}).get("raw_price_research_ready"):
        raise ValueError("银河数据尚未通过 raw_price_research_ready 验收")

    final_holdout_start = _date_key(time_design["final_holdout_start"])
    holdout_marker = project / "state" / "yinhe_baseline_final_holdout_opened.json"
    if open_final_holdout and holdout_marker.exists():
        raise ValueError(f"最终留出集已经打开过，禁止重复运行：{holdout_marker}")
    analysis_paths = (
        paths
        if open_final_holdout
        else [path for path in paths if path.stem < final_holdout_start]
    )
    if not analysis_paths:
        raise ValueError("最终留出集关闭后没有可用于训练或样本外验证的数据")

    parameters = {
        "top_n": int(baseline["candidate_count"]),
        "min_history": 120,
        "momentum_days": 20,
        "min_avg_turnover": 50_000_000.0,
    }
    signals, market_returns = generate_signals(
        analysis_paths,
        horizons=horizons,
        **parameters,
    )
    results = evaluate_signals(
        analysis_paths,
        signals,
        market_returns,
        commission_rate=float(costs["commission_rate_each_side"]),
        stamp_tax_rate=float(costs["stamp_tax_rate_sell"]),
        slippage_rate=float(costs["slippage_rate_each_side"]),
    )

    development = (
        _date_key(time_design["development_start"]),
        _date_key(time_design["development_end"]),
    )
    rolling_oos = (
        _date_key(time_design["rolling_oos_start"]),
        _date_key(time_design["rolling_oos_end"]),
    )
    final_holdout = (
        _date_key(time_design["final_holdout_start"]),
        _date_key(time_design["final_holdout_end"]),
    )
    analysis_dates = [path.stem for path in analysis_paths]
    rolling_test_days = int(time_design["rolling_test_days"])
    rolling_step_days = int(time_design["rolling_step_days"])
    horizon_records: list[dict[str, Any]] = []
    for horizon in horizons:
        offsets = _offsets(horizon)
        period_records: dict[str, Any] = {}
        for label, boundaries in (
            ("development", development),
            ("rolling_oos", rolling_oos),
        ):
            offset_metrics = {
                str(offset): _metrics(
                    _period_results(
                        [
                            result
                            for result in results
                            if result.horizon == horizon and result.offset == offset
                        ],
                        *boundaries,
                    )
                )
                for offset in offsets
            }
            if label == "rolling_oos":
                for offset, values in offset_metrics.items():
                    configuration_results = [
                        result
                        for result in results
                        if result.horizon == horizon
                        and result.offset == int(offset)
                    ]
                    windows = _rolling_metrics(
                        configuration_results,
                        analysis_dates,
                        start=boundaries[0],
                        end=boundaries[1],
                        test_days=rolling_test_days,
                        step_days=rolling_step_days,
                    )
                    completed_windows = [
                        window
                        for window in windows
                        if window.get("status") == "completed"
                    ]
                    values["rolling_windows"] = windows
                    values["positive_rolling_window_ratio"] = (
                        sum(
                            window["median_net_excess_return"] > 0
                            for window in completed_windows
                        )
                        / len(completed_windows)
                        if completed_windows
                        else 0.0
                    )
            completed = [
                value
                for value in offset_metrics.values()
                if value.get("status") == "completed"
            ]
            period_records[label] = {
                "positive_offsets": sum(
                    value["median_net_excess_return"] > 0
                    for value in completed
                ),
                "median_offset_excess": (
                    median(value["median_net_excess_return"] for value in completed)
                    if completed
                    else 0.0
                ),
                "median_positive_rolling_window_ratio": (
                    median(
                        value.get("positive_rolling_window_ratio", 0.0)
                        for value in completed
                    )
                    if label == "rolling_oos" and completed
                    else None
                ),
                "offsets": offset_metrics,
            }
        if open_final_holdout:
            period_records["final_holdout"] = {
                str(offset): _metrics(
                    _period_results(
                        [
                            result
                            for result in results
                            if result.horizon == horizon and result.offset == offset
                        ],
                        *final_holdout,
                    )
                )
                for offset in offsets
            }
        horizon_records.append(
            {
                "horizon": horizon,
                "offset_count": len(offsets),
                **period_records,
            }
        )

    minimum_ratio = float(protocol["multiple_starts"]["minimum_positive_start_ratio"])
    eligible = [
        record
        for record in horizon_records
        if record["development"]["positive_offsets"]
        / max(record["offset_count"], 1)
        >= minimum_ratio
        and record["development"]["median_offset_excess"] >= 0
    ]
    selected = (
        max(
            eligible,
            key=lambda record: record["development"]["median_offset_excess"],
        )["horizon"]
        if eligible
        else None
    )
    selected_record = next(
        (record for record in horizon_records if record["horizon"] == selected),
        None,
    )
    gate = protocol["stability_gate"]
    selected_oos_results = (
        _period_results(
            [result for result in results if result.horizon == selected],
            *rolling_oos,
        )
        if selected is not None
        else []
    )
    selected_oos_metrics = _metrics(selected_oos_results)
    selection_checks = {
        "selected_in_development": selected is not None,
        "minimum_oos_observations": (
            selected_oos_metrics.get("trades", 0)
            >= int(gate["minimum_oos_observations"])
        ),
        "minimum_rebalance_cohorts": (
            selected_oos_metrics.get("cohorts", 0)
            >= int(gate["minimum_rebalance_cohorts"])
        ),
        "minimum_positive_rolling_window_ratio": (
            bool(selected_record)
            and selected_record["rolling_oos"]["median_positive_rolling_window_ratio"]
            >= float(gate["minimum_positive_rolling_window_ratio"])
        ),
        "minimum_median_net_excess_return": (
            selected_oos_metrics.get("median_net_excess_return", float("-inf"))
            >= float(gate["minimum_median_net_excess_return"])
        ),
        "maximum_drawdown": (
            abs(selected_oos_metrics.get("max_drawdown", float("-inf")))
            <= float(gate["maximum_drawdown"])
        ),
    }
    selection_validation = {
        "decision": (
            "accepted_provisionally"
            if all(selection_checks.values())
            else "rejected_or_insufficient"
        ),
        "checks": selection_checks,
        "rolling_oos_metrics": selected_oos_metrics,
    }
    output = output_dir or project / "reports" / "yinhe_price_baseline"
    output.mkdir(parents=True, exist_ok=True)
    document: dict[str, Any] = {
        "status": "provisional_raw_price_only",
        "generated_at": datetime.now(UTC).isoformat(),
        "model_id": baseline["model_id"],
        "research_only": True,
        "execution_allowed": False,
        "data_version": acceptance.get("data_version"),
        "protocol_sha256": loaded_protocol["protocol_sha256"],
        "parameters": parameters,
        "benchmark": "daily_equal_weight_open_return_proxy",
        "data_profile": {
            "first_date": analysis_paths[0].stem,
            "last_date": analysis_paths[-1].stem,
            "available_last_date": paths[-1].stem,
            "files": len(analysis_paths),
            "signals": len(signals),
            "results": len(results),
        },
        "selected_horizon": selected,
        "selection_validation": selection_validation,
        "final_holdout_opened": open_final_holdout,
        "horizons": horizon_records,
        "blocked_checks": [
            "forward_adjustment_continuity",
            "point_in_time_security_states",
            "survivorship_bias",
            "official_market_and_industry_benchmarks",
            "strict_information_timing",
        ],
    }
    (output / "latest.json").write_text(
        json.dumps(document, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    (output / "latest.md").write_text(_render_report(document), encoding="utf-8")
    (output / "latest_results.json").write_text(
        json.dumps([asdict(result) for result in results], ensure_ascii=False),
        encoding="utf-8",
    )
    if open_final_holdout:
        holdout_marker.parent.mkdir(parents=True, exist_ok=True)
        holdout_marker.write_text(
            json.dumps(
                {
                    "opened_at": document["generated_at"],
                    "data_version": document["data_version"],
                    "protocol_sha256": document["protocol_sha256"],
                    "report_json": str(output / "latest.json"),
                },
                ensure_ascii=False,
                indent=2,
            )
            + "\n",
            encoding="utf-8",
        )
    return {
        "status": document["status"],
        "data_version": document["data_version"],
        "files": len(analysis_paths),
        "signals": len(signals),
        "results": len(results),
        "selected_horizon": selected,
        "final_holdout_opened": open_final_holdout,
        "report_json": str(output / "latest.json"),
        "report_markdown": str(output / "latest.md"),
        "results_json": str(output / "latest_results.json"),
    }

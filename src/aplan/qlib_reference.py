from __future__ import annotations

import argparse
import csv
import gc
import hashlib
import json
import math
import tomllib
from collections import defaultdict, deque
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from statistics import mean
from typing import Iterable, Sequence

from .strategy import (
    Evidence,
    SignalIntent,
    StrategyContext,
    StrategyMetadata,
    StrategyStatus,
    UnifiedSignal,
    new_signal,
)


STRATEGY_ID = "qlib_alpha158_linear_lite_reference_v0_1"
FEATURE_NAMES = (
    "RESI5",
    "WVMA5",
    "RSQR5",
    "KLEN",
    "RSQR10",
    "CORR5",
    "CORD5",
    "CORR10",
    "ROC60",
    "RESI10",
    "VSTD5",
    "RSQR60",
    "CORR60",
    "WVMA60",
    "STD5",
    "RSQR20",
    "CORD60",
    "CORD10",
    "CORR20",
    "KLOW",
)


@dataclass(frozen=True, slots=True)
class Bar:
    open: float
    high: float
    low: float
    close: float
    volume: float
    turnover: float
    suspended: bool = False
    limit_up: bool = False
    limit_down: bool = False


@dataclass(frozen=True, slots=True)
class Observation:
    trade_date: str
    symbol: str
    features: tuple[float, ...]
    future_return: float
    turnover: float


def _safe_div(numerator: float, denominator: float) -> float:
    if abs(denominator) < 1e-12:
        return 0.0
    return numerator / denominator


def _std(values: Sequence[float]) -> float:
    if not values:
        return 0.0
    center = sum(values) / len(values)
    return math.sqrt(sum((value - center) ** 2 for value in values) / len(values))


def _average(values: Sequence[float]) -> float:
    return sum(values) / len(values) if values else 0.0


def _corr(left: Sequence[float], right: Sequence[float]) -> float:
    if len(left) != len(right) or not left:
        return 0.0
    left_mean = sum(left) / len(left)
    right_mean = sum(right) / len(right)
    numerator = sum(
        (left_value - left_mean) * (right_value - right_mean)
        for left_value, right_value in zip(left, right, strict=True)
    )
    denominator = math.sqrt(
        sum((value - left_mean) ** 2 for value in left)
        * sum((value - right_mean) ** 2 for value in right)
    )
    return _safe_div(numerator, denominator)


def _linear_stats(values: Sequence[float]) -> tuple[float, float]:
    """返回当前点线性回归残差和 R²；时间坐标为 0..n-1。"""
    if len(values) < 2:
        return 0.0, 0.0
    n = len(values)
    x_mean = (n - 1) / 2
    y_mean = sum(values) / n
    x_variance = sum((index - x_mean) ** 2 for index in range(n))
    slope = _safe_div(
        sum((index - x_mean) * (value - y_mean) for index, value in enumerate(values)),
        x_variance,
    )
    intercept = y_mean - slope * x_mean
    fitted = [intercept + slope * index for index in range(n)]
    residual = values[-1] - fitted[-1]
    total = sum((value - y_mean) ** 2 for value in values)
    error = sum((value - estimate) ** 2 for value, estimate in zip(values, fitted, strict=True))
    r_squared = 1 - _safe_div(error, total) if total > 1e-12 else 0.0
    return residual, max(0.0, min(1.0, r_squared))


def _window_features(bars: Sequence[Bar], window: int) -> dict[str, float]:
    selected = list(bars)[-window:]
    changes = list(bars)[-(window + 1):]
    closes = [bar.close for bar in selected]
    volumes = [max(bar.volume, 0.0) for bar in selected]
    change_closes = [bar.close for bar in changes]
    change_volumes = [max(bar.volume, 0.0) for bar in changes]
    returns = [
        _safe_div(change_closes[index], change_closes[index - 1]) - 1
        for index in range(1, len(change_closes))
    ]
    weighted_moves = [
        abs(value) * change_volumes[index]
        for index, value in enumerate(returns, start=1)
    ]
    close_ratios = [
        _safe_div(change_closes[index], change_closes[index - 1])
        for index in range(1, len(change_closes))
    ]
    volume_log_ratios = [
        math.log(
            max(
                _safe_div(change_volumes[index], change_volumes[index - 1]) + 1,
                1e-12,
            )
        )
        for index in range(1, len(change_volumes))
    ]
    residual, r_squared = _linear_stats(closes)
    return {
        "RESI": _safe_div(residual, closes[-1]),
        "RSQR": r_squared,
        "CORR": _corr(closes, [math.log(value + 1) for value in volumes]),
        "CORD": _corr(close_ratios, volume_log_ratios),
        "WVMA": _safe_div(_std(weighted_moves), _average(weighted_moves))
        if weighted_moves and abs(_average(weighted_moves)) > 1e-12
        else 0.0,
        "VSTD": _safe_div(_std(volumes), volumes[-1]),
        "STD": _safe_div(_std(closes), closes[-1]),
    }


def alpha158_selected20(bars: Sequence[Bar]) -> tuple[float, ...] | None:
    """计算 Qlib 公开的 20 个精选 Alpha158 特征的 APlan 等价实现。"""
    if len(bars) < 61:
        return None
    bars = list(bars)
    latest = bars[-1]
    if latest.open <= 0 or latest.close <= 0 or latest.volume <= 0:
        return None
    w5 = _window_features(bars, 5)
    w10 = _window_features(bars, 10)
    w20 = _window_features(bars, 20)
    w60 = _window_features(bars, 60)
    klen = _safe_div(latest.high - latest.low, latest.open)
    klow = _safe_div(min(latest.open, latest.close) - latest.low, latest.open)
    values = (
        w5["RESI"],
        w5["WVMA"],
        w5["RSQR"],
        klen,
        w10["RSQR"],
        w5["CORR"],
        w5["CORD"],
        w10["CORR"],
        _safe_div(bars[-61].close, latest.close),
        w10["RESI"],
        w5["VSTD"],
        w60["RSQR"],
        w60["CORR"],
        w60["WVMA"],
        w5["STD"],
        w20["RSQR"],
        w60["CORD"],
        w10["CORD"],
        w20["CORR"],
        klow,
    )
    if not all(math.isfinite(value) for value in values):
        return None
    return values


def _parse_bool(value: str | int | float | None) -> bool:
    return str(value or "").strip().lower() in {"1", "true", "yes"}


def load_bars(path: Path) -> dict[str, Bar]:
    bars: dict[str, Bar] = {}
    with path.open(newline="", encoding="utf-8") as handle:
        for row in csv.DictReader(handle):
            symbol = str(row.get("symbol") or row.get("ts_code") or "").split(".", 1)[0]
            if len(symbol) != 6 or not symbol.isdigit():
                continue
            try:
                bars[symbol] = Bar(
                    open=float(row["open"]),
                    high=float(row["high"]),
                    low=float(row["low"]),
                    close=float(row["close"]),
                    volume=float(row.get("volume") or row.get("vol") or 0),
                    turnover=float(row.get("turnover") or row.get("amount") or 0),
                    suspended=_parse_bool(row.get("is_suspended")),
                    limit_up=_parse_bool(row.get("is_limit_up")),
                    limit_down=_parse_bool(row.get("is_limit_down")),
                )
            except (KeyError, TypeError, ValueError):
                continue
    return bars


def available_dates(data_root: Path) -> list[str]:
    return sorted(path.stem for path in data_root.glob("20??????.csv"))


def _rank_labels(values: list[tuple[int, float]]) -> dict[int, float]:
    ordered = sorted(values, key=lambda item: item[1])
    if len(ordered) == 1:
        return {ordered[0][0]: 0.0}
    return {
        original_index: rank / (len(ordered) - 1) - 0.5
        for rank, (original_index, _) in enumerate(ordered)
    }


def _sampled(trade_date: str, symbol: str, rate: float) -> bool:
    if rate >= 1:
        return True
    digest = hashlib.sha256(f"{trade_date}|{symbol}".encode()).digest()
    value = int.from_bytes(digest[:8], "big") / 2**64
    return value < rate


def build_observations(
    data_root: Path,
    *,
    signal_start: str,
    signal_end: str,
    sample_rate: float = 1.0,
    audit: dict[str, object] | None = None,
) -> list[Observation]:
    dates = available_dates(data_root)
    histories: dict[str, deque[Bar]] = defaultdict(lambda: deque(maxlen=61))
    pending: deque[tuple[str, list[tuple[str, tuple[float, ...], float]]]] = deque()
    previous_closes: dict[str, float] = {}
    observations: list[Observation] = []
    previous_date: datetime | None = None

    for trade_date in dates:
        current_date = datetime.strptime(trade_date, "%Y%m%d")
        if previous_date is not None and (current_date - previous_date).days > 14:
            histories.clear()
            pending.clear()
            previous_closes = {}
        previous_date = current_date

        day_bars = load_bars(data_root / f"{trade_date}.csv")
        if audit is not None:
            audit["files_read"] = int(audit.get("files_read") or 0) + 1
            audit["rows_read"] = int(audit.get("rows_read") or 0) + len(day_bars)
            audit["first_date_read"] = min(
                str(audit.get("first_date_read") or trade_date), trade_date
            )
            audit["last_date_read"] = max(
                str(audit.get("last_date_read") or trade_date), trade_date
            )
            if trade_date >= "20250101":
                audit["rows_2025_or_later"] = (
                    int(audit.get("rows_2025_or_later") or 0) + len(day_bars)
                )
        current_closes = {symbol: bar.close for symbol, bar in day_bars.items()}

        if len(pending) >= 2:
            pending_date, candidates = pending.popleft()
            if signal_start <= pending_date <= signal_end:
                day_rows: list[Observation] = []
                for symbol, features, turnover in candidates:
                    first = previous_closes.get(symbol)
                    second = current_closes.get(symbol)
                    if first is None or second is None or first <= 0:
                        continue
                    day_rows.append(
                        Observation(
                            pending_date,
                            symbol,
                            features,
                            second / first - 1,
                            turnover,
                        )
                    )
                observations.extend(day_rows)

        candidates: list[tuple[str, tuple[float, ...], float]] = []
        for symbol, bar in day_bars.items():
            histories[symbol].append(bar)
            if bar.suspended or bar.limit_up or bar.turnover <= 0:
                continue
            if not _sampled(trade_date, symbol, sample_rate):
                continue
            features = alpha158_selected20(histories[symbol])
            if features is None:
                continue
            candidates.append((symbol, features, bar.turnover))
        pending.append((trade_date, candidates))
        previous_closes = current_closes
        if trade_date > signal_end and not any(
            pending_date <= signal_end for pending_date, _ in pending
        ):
            break
    return observations


def _fit_ridge(
    observations: Sequence[Observation],
    *,
    alpha: float,
    clip_zscore: float,
) -> dict[str, object]:
    try:
        import numpy as np
    except ImportError as exc:  # pragma: no cover - local research dependency guard
        raise RuntimeError("运行开源基线需要 numpy") from exc

    features = np.asarray([row.features for row in observations], dtype=np.float64)
    if features.shape[0] < len(FEATURE_NAMES) * 5:
        raise ValueError("训练样本不足")
    labels = np.empty(features.shape[0], dtype=np.float64)
    by_date: dict[str, list[tuple[int, float]]] = defaultdict(list)
    for index, row in enumerate(observations):
        by_date[row.trade_date].append((index, row.future_return))
    for values in by_date.values():
        for index, label in _rank_labels(values).items():
            labels[index] = label

    medians = np.median(features, axis=0)
    mad = np.median(np.abs(features - medians), axis=0)
    scales = np.where(mad > 1e-12, mad * 1.4826, 1.0)
    features -= medians
    features /= scales
    np.clip(features, -clip_zscore, clip_zscore, out=features)
    np.nan_to_num(features, copy=False)
    feature_sum = features.sum(axis=0)
    gram = np.empty((features.shape[1] + 1, features.shape[1] + 1))
    gram[0, 0] = features.shape[0]
    gram[0, 1:] = feature_sum
    gram[1:, 0] = feature_sum
    gram[1:, 1:] = features.T @ features
    rhs = np.concatenate(([labels.sum()], features.T @ labels))
    penalty = np.eye(gram.shape[0]) * alpha
    penalty[0, 0] = 0
    coefficients = np.linalg.solve(gram + penalty, rhs)
    return {
        "feature_names": list(FEATURE_NAMES),
        "medians": medians.tolist(),
        "scales": scales.tolist(),
        "coefficients": coefficients.tolist(),
        "alpha": alpha,
        "clip_zscore": clip_zscore,
        "training_rows": len(observations),
        "training_days": len(by_date),
    }


def _scores(
    model: dict[str, object],
    observations: Sequence[Observation],
) -> dict[str, list[tuple[Observation, float]]]:
    medians = [float(value) for value in model["medians"]]
    scales = [float(value) for value in model["scales"]]
    coefficients = [float(value) for value in model["coefficients"]]
    clip = float(model["clip_zscore"])
    grouped: dict[str, list[tuple[Observation, float]]] = defaultdict(list)
    for row in observations:
        normalized = [
            max(-clip, min(clip, _safe_div(value - median, scale)))
            for value, median, scale in zip(row.features, medians, scales, strict=True)
        ]
        score = coefficients[0] + sum(
            coefficient * value
            for coefficient, value in zip(coefficients[1:], normalized, strict=True)
        )
        grouped[row.trade_date].append((row, score))
    return grouped


def _score_features(
    model: dict[str, object],
    features: Sequence[float],
) -> float:
    medians = [float(value) for value in model["medians"]]
    scales = [float(value) for value in model["scales"]]
    coefficients = [float(value) for value in model["coefficients"]]
    clip = float(model["clip_zscore"])
    normalized = [
        max(-clip, min(clip, _safe_div(value - median, scale)))
        for value, median, scale in zip(features, medians, scales, strict=True)
    ]
    return coefficients[0] + sum(
        coefficient * value
        for coefficient, value in zip(coefficients[1:], normalized, strict=True)
    )


def _rank_ic(rows: Sequence[tuple[Observation, float]]) -> float:
    if len(rows) < 2:
        return 0.0
    score_order = sorted(range(len(rows)), key=lambda index: rows[index][1])
    return_order = sorted(range(len(rows)), key=lambda index: rows[index][0].future_return)
    score_rank = [0.0] * len(rows)
    return_rank = [0.0] * len(rows)
    for rank, index in enumerate(score_order):
        score_rank[index] = float(rank)
    for rank, index in enumerate(return_order):
        return_rank[index] = float(rank)
    return _corr(score_rank, return_rank)


def _next_holdings(
    holdings: set[str],
    ranked: Sequence[tuple[Observation, float]],
    *,
    topk: int,
    n_drop: int,
) -> set[str]:
    ranked_symbols = [row.symbol for row, _ in ranked]
    available = set(ranked_symbols)
    last = holdings & available
    new_candidates = [
        symbol for symbol in ranked_symbols if symbol not in last
    ][: n_drop + max(0, topk - len(last))]
    combined = [
        symbol
        for symbol in ranked_symbols
        if symbol in last or symbol in set(new_candidates)
    ]
    bottom = set(combined[-n_drop:]) if n_drop else set()
    sells = last & bottom
    buys = new_candidates[: len(sells) + max(0, topk - len(last))]
    return (last - sells) | set(buys)


def evaluate_topk_dropout(
    grouped_scores: dict[str, list[tuple[Observation, float]]],
    *,
    topk: int,
    n_drop: int,
    open_cost: float,
    close_cost: float,
    min_names_per_day: int,
    score_output_root: Path | None = None,
) -> tuple[
    dict[str, object],
    str | None,
    list[tuple[Observation, float]],
]:
    holdings: set[str] = set()
    daily_net_excess: list[float] = []
    daily_rank_ic: list[float] = []
    positive_months: dict[str, list[float]] = defaultdict(list)
    latest_date: str | None = None
    latest_rows: list[tuple[Observation, float]] = []
    equity = 1.0
    peak = 1.0
    max_drawdown = 0.0

    for trade_date in sorted(grouped_scores):
        rows = grouped_scores[trade_date]
        if len(rows) < min_names_per_day:
            continue
        ranked = sorted(rows, key=lambda item: item[1], reverse=True)
        row_by_symbol = {row.symbol: row for row, _ in ranked}
        new_holdings = _next_holdings(
            holdings, ranked, topk=topk, n_drop=n_drop
        )
        if not new_holdings:
            continue
        buys_count = len(new_holdings - holdings)
        sells_count = len(holdings - new_holdings)
        portfolio_return = mean(
            row_by_symbol[symbol].future_return for symbol in new_holdings
        )
        benchmark_return = mean(row.future_return for row, _ in ranked)
        cost = (
            buys_count / topk * open_cost
            + sells_count / topk * close_cost
        )
        net_excess = portfolio_return - benchmark_return - cost
        daily_net_excess.append(net_excess)
        positive_months[trade_date[:6]].append(net_excess)
        daily_rank_ic.append(_rank_ic(rows))
        equity *= 1 + net_excess
        peak = max(peak, equity)
        max_drawdown = min(max_drawdown, equity / peak - 1)
        holdings = new_holdings
        if score_output_root is not None:
            _write_score_snapshot(score_output_root / f"{trade_date}.csv", ranked)
        latest_date = trade_date
        latest_rows = ranked

    month_returns = [
        math.prod(1 + value for value in values) - 1
        for values in positive_months.values()
    ]
    summary: dict[str, object] = {
        "validation_days": len(daily_net_excess),
        "mean_rank_ic": mean(daily_rank_ic) if daily_rank_ic else None,
        "positive_rank_ic_ratio": (
            sum(value > 0 for value in daily_rank_ic) / len(daily_rank_ic)
            if daily_rank_ic
            else None
        ),
        "cumulative_net_excess": equity - 1 if daily_net_excess else None,
        "mean_daily_net_excess": (
            mean(daily_net_excess) if daily_net_excess else None
        ),
        "max_drawdown": max_drawdown if daily_net_excess else None,
        "positive_month_ratio": (
            sum(value > 0 for value in month_returns) / len(month_returns)
            if month_returns
            else None
        ),
        "months": len(month_returns),
    }
    return summary, latest_date, latest_rows


def _hash_inputs(data_root: Path, start: str, end: str) -> dict[str, object]:
    digest = hashlib.sha256()
    paths = [
        data_root / f"{day}.csv"
        for day in available_dates(data_root)
        if start <= day <= end
    ]
    total_bytes = 0
    for path in paths:
        digest.update(path.name.encode())
        with path.open("rb") as handle:
            for block in iter(lambda: handle.read(1024 * 1024), b""):
                total_bytes += len(block)
                digest.update(block)
    return {
        "aggregate_sha256": digest.hexdigest(),
        "files": len(paths),
        "bytes": total_bytes,
        "first_date": paths[0].stem if paths else None,
        "last_date": paths[-1].stem if paths else None,
    }


def _audit_qfq_input(
    data_root: Path,
    *,
    train_start: str,
    validation_end: str,
) -> dict[str, object]:
    if data_root.name != "yinhe_daily_qfq":
        raise ValueError("正式性能运行必须显式读取 yinhe_daily_qfq")
    manifest_path = data_root.parent / "yinhe_adj_factor" / "manifest.json"
    if not manifest_path.exists():
        raise ValueError("缺少银河前复权 manifest")
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    if manifest.get("status") not in {"validated", "validated_with_quarantine"}:
        raise ValueError("银河前复权 manifest 未通过验收")
    if int(manifest.get("missing_factor_rows") or 0):
        raise ValueError("银河前复权存在缺失因子行")
    if str(manifest.get("coverage_start") or "") > train_start:
        raise ValueError("银河前复权起点晚于训练起点")
    if str(manifest.get("coverage_end") or "") < validation_end:
        raise ValueError("银河前复权终点早于验证终点")
    return {
        "manifest_path": str(manifest_path),
        "manifest_sha256": hashlib.sha256(manifest_path.read_bytes()).hexdigest(),
        "manifest_status": manifest.get("status"),
        "coverage_start": manifest.get("coverage_start"),
        "coverage_end": manifest.get("coverage_end"),
        "adjusted_rows": manifest.get("adjusted_rows"),
        "missing_factor_rows": manifest.get("missing_factor_rows"),
        "quarantined_symbols": manifest.get("quarantined_symbols") or [],
        "input_files": _hash_inputs(data_root, train_start, validation_end),
    }


def _pipeline_status(latest_score_date: str | None) -> str:
    return "completed_pipeline_pilot" if latest_score_date else "data_unavailable"


def _contains_forbidden_key(value: object) -> bool:
    if isinstance(value, dict):
        return any(
            str(key).lower() in {"future_return", "outcome"}
            or _contains_forbidden_key(nested)
            for key, nested in value.items()
        )
    if isinstance(value, list):
        return any(_contains_forbidden_key(item) for item in value)
    return False


def run_current_inference(
    *,
    data_root: Path,
    model_path: Path,
    conclusion_as_of: str,
    price_as_of: str,
    preview_path: Path,
    full_scores_path: Path,
    top_n: int = 50,
) -> dict[str, object]:
    conclusion_as_of = "".join(
        character for character in conclusion_as_of if character.isdigit()
    )[:8]
    price_as_of = "".join(
        character for character in price_as_of if character.isdigit()
    )[:8]
    if len(conclusion_as_of) != 8 or len(price_as_of) != 8:
        raise ValueError("inference as_of 与 price_as_of 必须为 YYYYMMDD")
    if price_as_of > conclusion_as_of:
        raise ValueError("price_as_of 不得晚于结论日")
    dates = [day for day in available_dates(data_root) if day <= price_as_of]
    if not dates or dates[-1] != price_as_of:
        raise ValueError(f"qfq 不包含价格日 {price_as_of}")
    history_dates = dates[-61:]
    if len(history_dates) < 61:
        raise ValueError("当前影子推理至少需要 61 个交易日")
    if any(day > price_as_of for day in history_dates):
        raise ValueError("当前影子推理不得读取信号日之后的数据")

    model_document = json.loads(model_path.read_text(encoding="utf-8"))
    model = model_document.get("model")
    if not isinstance(model, dict):
        raise ValueError("冻结 model.json 缺少 model 参数")
    if int(model.get("training_rows") or 0) <= 0:
        raise ValueError("冻结 model.json 没有有效训练记录")

    histories: dict[str, deque[Bar]] = defaultdict(lambda: deque(maxlen=61))
    file_rows = 0
    for day in history_dates:
        bars = load_bars(data_root / f"{day}.csv")
        file_rows += len(bars)
        for symbol, bar in bars.items():
            histories[symbol].append(bar)
    latest = load_bars(data_root / f"{price_as_of}.csv")
    excluded: dict[str, int] = defaultdict(int)
    scored: list[dict[str, object]] = []
    for symbol, bar in latest.items():
        if bar.suspended:
            excluded["suspended"] += 1
            continue
        if bar.limit_up:
            excluded["limit_up"] += 1
            continue
        if bar.turnover <= 0:
            excluded["nonpositive_turnover"] += 1
            continue
        features = alpha158_selected20(histories.get(symbol, ()))
        if features is None:
            excluded["insufficient_or_invalid_features"] += 1
            continue
        scored.append(
            {
                "symbol": symbol,
                "model_score": _score_features(model, features),
            }
        )
    scored.sort(key=lambda row: float(row["model_score"]), reverse=True)
    denominator = max(len(scored) - 1, 1)
    for rank, row in enumerate(scored, 1):
        row["rank"] = rank
        row["score_percentile"] = round(100 * (1 - (rank - 1) / denominator), 6)

    full_scores_path.parent.mkdir(parents=True, exist_ok=True)
    with full_scores_path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(
            handle,
            fieldnames=("symbol", "rank", "model_score", "score_percentile"),
        )
        writer.writeheader()
        writer.writerows(scored)

    evidence = [
        {
            "kind": "frozen_external_model_score",
            "summary": "冻结2023训练模型的当前横截面排序",
            "source": "cloud_qfq_frozen_pilot",
            "observed_at": price_as_of,
        }
    ]
    risks = [
        "正式研究门控结论为reject",
        "当前结果是影子推理，不代表模型已验证通过",
        "Qlib轻量派生实现不是官方benchmark原样复现",
    ]
    invalidation = ["数据哈希变化", "冻结模型哈希变化", "输入未通过验收"]
    items = [
        {
            **row,
            "evidence": evidence,
            "risks": risks,
            "invalidation": invalidation,
        }
        for row in scored[:top_n]
    ]
    data_audit = _hash_inputs(data_root, history_dates[0], price_as_of)
    preview: dict[str, object] = {
        "model_id": STRATEGY_ID,
        "as_of": conclusion_as_of,
        "price_as_of": price_as_of,
        "evidence_as_of": conclusion_as_of,
        "generated_at": datetime.now(UTC).isoformat(),
        "source": "cloud_qfq_frozen_pilot",
        "data_hash": data_audit["aggregate_sha256"],
        "model_hash": hashlib.sha256(model_path.read_bytes()).hexdigest(),
        "provisional": True,
        "formal_gate": "reject",
        "execution_eligible": False,
        "holdout_boundary_opened": True,
        "conclusion_status": "current_shadow_inference",
        "evidence_review": {
            "status": "evidence_gap",
            "score_impact": "none",
            "details": (
                "未加载截至结论日严格PIT可用的证券状态或公告风险；"
                "未跨时点填补，模型分数仅使用价格日及以前的冻结量价特征"
            ),
        },
        "coverage": {
            "latest_observed_symbols": len(latest),
            "scored_symbols": len(scored),
            "excluded_symbols": len(latest) - len(scored),
            "coverage_rate": (
                round(len(scored) / len(latest), 8) if latest else None
            ),
            "excluded_by_reason": dict(sorted(excluded.items())),
            "history_first_date": history_dates[0],
            "history_last_date": history_dates[-1],
            "history_files": len(history_dates),
            "history_rows_read": file_rows,
        },
        "items": items,
    }
    if _contains_forbidden_key(preview):
        raise ValueError("当前影子推理预览包含禁止的未来结果字段")
    preview_path.parent.mkdir(parents=True, exist_ok=True)
    preview_path.write_text(
        json.dumps(preview, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    return {
        "status": "current_shadow_inference",
        "as_of": conclusion_as_of,
        "price_as_of": price_as_of,
        "latest_observed_symbols": len(latest),
        "scored_symbols": len(scored),
        "excluded_symbols": len(latest) - len(scored),
        "top_n": len(items),
        "data_hash": preview["data_hash"],
        "model_hash": preview["model_hash"],
        "preview_hash": hashlib.sha256(preview_path.read_bytes()).hexdigest(),
        "preview_path": str(preview_path),
        "full_scores_path": str(full_scores_path),
        "formal_gate": "reject",
        "execution_eligible": False,
        "holdout_boundary_opened": True,
    }


def _write_score_snapshot(
    path: Path,
    rows: Sequence[tuple[Observation, float]],
) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    ordered = sorted(rows, key=lambda item: item[1], reverse=True)
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(
            handle,
            fieldnames=("symbol", "model_score", "rank"),
        )
        writer.writeheader()
        for rank, (row, score) in enumerate(ordered, 1):
            writer.writerow(
                {
                    "symbol": row.symbol,
                    "model_score": f"{score:.12g}",
                    "rank": rank,
                }
            )


def run_pilot(
    *,
    project_root: Path,
    data_root: Path,
    config_path: Path,
    output_root: Path,
    input_price_kind: str | None = None,
    engineering_sample_rate: float | None = None,
) -> dict[str, object]:
    config = tomllib.loads(config_path.read_text(encoding="utf-8"))
    data_config = config["data"]
    model_config = config["model"]
    portfolio_config = config["portfolio"]
    configured_price_kind = str(data_config["price_kind"])
    actual_price_kind = input_price_kind or configured_price_kind
    performance_usable = actual_price_kind == configured_price_kind
    if engineering_sample_rate is not None and performance_usable:
        raise ValueError("正式性能运行不得使用 engineering_sample_rate")
    train_sample_rate = float(data_config["training_sample_rate"])
    validation_sample_rate = 1.0
    if engineering_sample_rate is not None:
        train_sample_rate = min(train_sample_rate, engineering_sample_rate)
        validation_sample_rate = engineering_sample_rate
    if data_config["open_2025"] or data_config["open_2026_holdout"]:
        raise ValueError("外部基线首轮不得打开 2025 或 2026")
    if str(data_config["validation_end"]) >= "20250101":
        raise ValueError("validation_end 越过 2025 封存边界")
    if performance_usable and train_sample_rate != 1.0:
        raise ValueError("正式 full-universe pilot 的 training_sample_rate 必须为 1.0")

    input_audit = (
        _audit_qfq_input(
            data_root,
            train_start=str(data_config["train_start"]),
            validation_end=str(data_config["validation_end"]),
        )
        if performance_usable
        else {"status": "engineering_input_not_audited"}
    )
    train_read_audit: dict[str, object] = {}
    validation_read_audit: dict[str, object] = {}

    train = build_observations(
        data_root,
        signal_start=str(data_config["train_start"]),
        signal_end=str(data_config["train_end"]),
        sample_rate=train_sample_rate,
        audit=train_read_audit,
    )
    train_rows = len(train)
    model = _fit_ridge(
        train,
        alpha=float(model_config["alpha"]),
        clip_zscore=float(model_config["clip_zscore"]),
    )
    del train
    gc.collect()
    validation = build_observations(
        data_root,
        signal_start=str(data_config["validation_start"]),
        signal_end=str(data_config["validation_end"]),
        sample_rate=validation_sample_rate,
        audit=validation_read_audit,
    )
    validation_rows = len(validation)
    grouped = _scores(model, validation)
    del validation
    gc.collect()
    score_root = output_root / "scores"
    evaluation, latest_date, latest_rows = evaluate_topk_dropout(
        grouped,
        topk=int(portfolio_config["topk"]),
        n_drop=int(portfolio_config["n_drop"]),
        open_cost=float(portfolio_config["open_cost"]),
        close_cost=float(portfolio_config["close_cost"]),
        min_names_per_day=int(portfolio_config["min_names_per_day"]),
        score_output_root=score_root,
    )
    stopping = config["stopping"]
    if not performance_usable:
        gate = "engineering_only"
    else:
        passed = (
            int(evaluation["validation_days"] or 0)
            >= int(stopping["minimum_validation_days"])
            and float(evaluation["mean_rank_ic"] or -math.inf)
            >= float(stopping["minimum_mean_rank_ic"])
            and float(evaluation["positive_month_ratio"] or 0)
            >= float(stopping["minimum_positive_month_ratio"])
            and (
                not bool(stopping["require_positive_net_excess"])
                or float(evaluation["cumulative_net_excess"] or -math.inf) > 0
            )
        )
        gate = "passed_phase1" if passed else "reject"

    output_root.mkdir(parents=True, exist_ok=True)
    model_document = {
        "strategy_id": STRATEGY_ID,
        "status": "research",
        "provenance": config["provenance"],
        "data": data_config,
        "actual_input_price_kind": actual_price_kind,
        "performance_usable": performance_usable,
        "engineering_sample_rate": engineering_sample_rate,
        "model": model,
        "input_audit": input_audit,
        "read_audit": {
            "train": train_read_audit,
            "validation": validation_read_audit,
        },
        "2025_opened": False,
        "2026_holdout_opened": False,
    }
    (output_root / "model.json").write_text(
        json.dumps(model_document, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    result = {
        "strategy_id": STRATEGY_ID,
        "status": _pipeline_status(latest_date),
        "classification": "external_reference_not_official_qlib_reproduction",
        "actual_input_price_kind": actual_price_kind,
        "performance_usable": performance_usable,
        "train_rows": train_rows,
        "validation_rows": validation_rows,
        "latest_score_date": latest_date,
        "evaluation": evaluation,
        "pre_registered_gate": gate,
        "input_audit": input_audit,
        "read_audit": {
            "train": train_read_audit,
            "validation": validation_read_audit,
            "rows_2025_or_later": (
                int(train_read_audit.get("rows_2025_or_later") or 0)
                + int(validation_read_audit.get("rows_2025_or_later") or 0)
            ),
        },
        "hashes": {
            "implementation_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
            "config_sha256": hashlib.sha256(config_path.read_bytes()).hexdigest(),
        },
        "qlib_differences": [
            "精选20个Alpha158特征，不是完整Alpha158",
            "APlan表达式求值器，不安装Qlib运行时",
            "银河全A股前复权输入，不是Qlib CSI300数据集",
            "APlan等权TopkDropout近似，不含整股、最小佣金与现金撮合",
            "净超额基准为同日可用全市场等权，不是官方CSI300指数",
            "冻结时间切分替代Qlib公开benchmark时间切分",
        ],
        "2025_opened": False,
        "2026_holdout_opened": False,
    }
    if latest_date:
        _write_score_snapshot(score_root / f"{latest_date}.csv", latest_rows)
        context = StrategyContext(
            latest_date,
            project_root,
            hashlib.sha256(
                (output_root / "scores" / f"{latest_date}.csv").read_bytes()
            ).hexdigest(),
        )
        plugin = QlibAlpha158LinearLiteReference(output_root)
        signals = [signal.to_dict() for signal in plugin.generate(context)]
        (output_root / "unified_signals.json").write_text(
            json.dumps(signals, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        result["unified_signal_count"] = len(signals)
    (output_root / "pilot_summary.json").write_text(
        json.dumps(result, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    evaluation_lines = [
        "# Qlib 派生外部基线试跑报告",
        "",
        f"- 状态：`{result['status']}`",
        f"- 门控：`{result['pre_registered_gate']}`",
        f"- 性能数字可用于判断：`{str(performance_usable).lower()}`",
        f"- 实际价格输入口径：`{actual_price_kind}`",
        f"- 训练观测：{train_rows:,}",
        f"- 验证观测：{validation_rows:,}",
        f"- 验证交易日：{evaluation['validation_days']}",
        f"- 平均 Rank IC：{evaluation['mean_rank_ic']}",
        f"- 累计净超额：{evaluation['cumulative_net_excess']}",
        f"- 最大回撤：{evaluation['max_drawdown']}",
        f"- 正收益月份比例：{evaluation['positive_month_ratio']}",
        f"- 统一 WATCH 信号：{result.get('unified_signal_count', 0)}",
        f"- 2025+ 实际读取行数：{result['read_audit']['rows_2025_or_later']}",
        "- 2025 已打开：false",
        "- 2026 最终留出集已打开：false",
        "",
        "本报告是 APlan 对 Qlib 公开组件的轻量派生基线，不是 Qlib 官方 benchmark "
        "收益复现，也不构成买卖建议。",
    ]
    (output_root / "report.md").write_text(
        "\n".join(evaluation_lines) + "\n",
        encoding="utf-8",
    )
    return result


class QlibAlpha158LinearLiteReference:
    metadata = StrategyMetadata(
        strategy_id=STRATEGY_ID,
        version="0.1.0",
        name="Qlib Alpha158 精选20特征 + Ridge 外部参考",
        status=StrategyStatus.RESEARCH,
    )

    def __init__(self, artifact_root: Path, *, top_n: int = 50) -> None:
        self.artifact_root = artifact_root
        self.top_n = top_n

    def generate(self, context: StrategyContext) -> list[UnifiedSignal]:
        path = self.artifact_root / "scores" / f"{context.trade_date}.csv"
        if not path.exists():
            return []
        rows: list[dict[str, str]] = []
        with path.open(newline="", encoding="utf-8") as handle:
            rows.extend(csv.DictReader(handle))
        selected = rows[: self.top_n]
        denominator = max(len(selected) - 1, 1)
        signals: list[UnifiedSignal] = []
        for index, row in enumerate(selected):
            score = 100 * (1 - index / denominator)
            signals.append(
                new_signal(
                    metadata=self.metadata,
                    context=context,
                    symbol=row["symbol"],
                    intent=SignalIntent.WATCH,
                    horizon_days=1,
                    score=score,
                    confidence=0.0,
                    target_weight=0.0,
                    evidence=(
                        Evidence(
                            "external_model_score",
                            f"Qlib 派生 Ridge 横截面排名第 {row['rank']}",
                            "github:microsoft/qlib",
                            context.trade_date,
                        ),
                    ),
                    risks=(
                        "外部派生基线尚未通过 APlan 冻结样本外门控",
                        "轻量适配版不是 Qlib 官方 benchmark 的原样复现",
                    ),
                    invalidation=(
                        "模型验证未通过或数据/实现哈希变化时立即失效",
                    ),
                )
            )
        return signals


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="运行 Qlib 派生外部基线流程试跑")
    parser.add_argument("--project-root", type=Path, default=Path.cwd())
    parser.add_argument(
        "--data-root",
        type=Path,
        default=Path("data/processed/daily"),
    )
    parser.add_argument(
        "--config",
        type=Path,
        default=Path("config/qlib_alpha158_linear_lite_reference.toml"),
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=Path("reports/qlib_alpha158_linear_lite_reference"),
    )
    parser.add_argument(
        "--input-price-kind",
        help=(
            "仅在输入口径与冻结配置不同时显式填写；此时结果只可用于工程冒烟，"
            "不得用于策略性能判断"
        ),
    )
    parser.add_argument(
        "--engineering-sample-rate",
        type=float,
        help="仅供 performance_usable=false 的工程冒烟抽样",
    )
    parser.add_argument("--inference-as-of", help="运行冻结模型无标签当前影子推理")
    parser.add_argument(
        "--price-as-of",
        help="模型价格与特征截止日；非交易日结论必须显式指定最近完整交易日",
    )
    parser.add_argument(
        "--model",
        type=Path,
        default=Path("reports/qlib_alpha158_linear_lite_reference/model.json"),
    )
    parser.add_argument("--preview-output", type=Path)
    parser.add_argument("--full-scores-output", type=Path)
    return parser


def main(argv: Iterable[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.inference_as_of:
        conclusion_as_of = "".join(
            character for character in args.inference_as_of if character.isdigit()
        )[:8]
        price_as_of = "".join(
            character
            for character in (args.price_as_of or args.inference_as_of)
            if character.isdigit()
        )[:8]
        preview = args.preview_output or (
            Path("data/research/model_previews")
            / STRATEGY_ID
            / f"{conclusion_as_of}.json"
        )
        full_scores = args.full_scores_output or (
            args.output / "current_shadow_scores" / f"{price_as_of}.csv"
        )
        result = run_current_inference(
            data_root=args.data_root.resolve(),
            model_path=args.model.resolve(),
            conclusion_as_of=conclusion_as_of,
            price_as_of=price_as_of,
            preview_path=preview.resolve(),
            full_scores_path=full_scores.resolve(),
        )
    else:
        result = run_pilot(
            project_root=args.project_root.resolve(),
            data_root=args.data_root.resolve(),
            config_path=args.config.resolve(),
            output_root=args.output.resolve(),
            input_price_kind=args.input_price_kind,
            engineering_sample_rate=args.engineering_sample_rate,
        )
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

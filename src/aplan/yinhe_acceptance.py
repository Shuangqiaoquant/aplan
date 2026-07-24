from __future__ import annotations

import csv
import hashlib
import json
import math
from datetime import UTC, datetime
from pathlib import Path
from statistics import median
from typing import Any

from .quality import file_sha256
from .validation_protocol import load_protocol


DAILY_REQUIRED_FIELDS = {
    "symbol",
    "trade_date",
    "open",
    "high",
    "low",
    "close",
    "volume",
    "turnover",
}


def _date_key(value: Any) -> str:
    text = str(value or "").replace("-", "").strip()
    return text[:8] if len(text) >= 8 and text[:8].isdigit() else ""


def _float(value: Any) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def _load_json(path: Path) -> dict[str, Any] | None:
    if not path.exists():
        return None
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return None
    return value if isinstance(value, dict) else None


def _check(
    check_id: str,
    category: str,
    status: str,
    severity: str,
    summary: str,
    evidence: dict[str, Any],
    impact: str,
    remediation: str,
) -> dict[str, Any]:
    return {
        "check_id": check_id,
        "category": category,
        "status": status,
        "severity": severity,
        "summary": summary,
        "evidence": evidence,
        "impact": impact,
        "remediation": remediation,
    }


def _load_calendar(path: Path | None, start: str, end: str) -> tuple[list[str], Path | None]:
    if path is None or not path.exists():
        return [], path
    dates: set[str] = set()
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        for row in csv.DictReader(handle):
            day = _date_key(row.get("trade_date") or row.get("cal_date") or row.get("date"))
            is_open = str(row.get("is_open", "1")).strip().lower()
            if start <= day <= end and is_open not in {"0", "false", "no"}:
                dates.add(day)
    return sorted(dates), path


def _load_current_universe(
    project: Path,
) -> tuple[set[str], dict[str, dict[str, str]], dict[str, int]]:
    symbols_path = project / "data" / "processed" / "yinhe_symbols.txt"
    securities_path = project / "data" / "processed" / "yinhe_securities.csv"
    symbols = {
        line.strip().split(".", 1)[0]
        for line in symbols_path.read_text(encoding="utf-8").splitlines()
        if len(line.strip().split(".", 1)[0]) == 6
    } if symbols_path.exists() else set()
    securities: dict[str, dict[str, str]] = {}
    state_mismatches = {"st": 0, "delisting": 0}
    if securities_path.exists():
        with securities_path.open("r", encoding="utf-8-sig", newline="") as handle:
            for row in csv.DictReader(handle):
                symbol = str(row.get("symbol") or "").split(".", 1)[0].zfill(6)
                if len(symbol) != 6 or not symbol.isdigit():
                    continue
                securities[symbol] = {key: str(value or "") for key, value in row.items()}
                name = str(row.get("name") or "").upper()
                is_st = str(row.get("is_st") or "0").lower() in {"1", "true", "yes"}
                is_delisting = str(row.get("is_delisting_risk") or "0").lower() in {
                    "1",
                    "true",
                    "yes",
                }
                state_mismatches["st"] += int(("ST" in name) != is_st)
                state_mismatches["delisting"] += int(("退" in name) != is_delisting)
    return symbols, securities, state_mismatches


def _status_manifest_checks(
    project: Path,
    start: str,
    end: str,
) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any]]:
    history_path = project / "data" / "processed" / "security_history" / "manifest.json"
    history = _load_json(history_path)
    required_states = {"listing", "delisting", "st", "suspension"}
    state_fields = set((history or {}).get("status_fields") or [])
    history_ok = bool(
        history
        and history.get("point_in_time") is True
        and _date_key(history.get("coverage_start")) <= start
        and _date_key(history.get("coverage_end")) >= end
        and required_states.issubset(state_fields)
    )
    point_in_time = _check(
        "point_in_time_security_states",
        "temporal_integrity",
        "pass" if history_ok else "blocked",
        "critical",
        "历史证券状态具备时点口径" if history_ok else "缺少完整的历史上市、退市、ST 与停牌状态",
        {
            "manifest_path": str(history_path),
            "manifest_present": history is not None,
            "point_in_time": (history or {}).get("point_in_time"),
            "status_fields": sorted(state_fields),
        },
        "缺少历史状态会造成幸存者偏差、错误股票池和错误可交易性判断。",
        "构建带生效时间的 security_history，并在 manifest 中声明覆盖期和状态字段。",
    )
    survivorship = _check(
        "survivorship_bias",
        "temporal_integrity",
        "pass" if history_ok else "blocked",
        "critical",
        "股票池可按历史时点重建" if history_ok else "当前股票池无法证明不存在幸存者偏差",
        {"point_in_time_universe": history_ok, "manifest_path": str(history_path)},
        "仅使用当前存续股票回看历史会系统性高估策略表现。",
        "回测必须按每个信号日重建当时可投资股票池。",
    )
    timing_ok = bool(
        history_ok
        and (history or {}).get("strict_availability_lag") is True
        and (history or {}).get("availability_timestamp_field")
    )
    timing = _check(
        "strict_information_timing",
        "timeliness",
        "pass" if timing_ok else "blocked",
        "critical",
        "输入数据满足严格可得时点" if timing_ok else "尚不能证明所有字段在信号时点已经发布",
        {
            "strict_availability_lag": (history or {}).get("strict_availability_lag"),
            "availability_timestamp_field": (history or {}).get("availability_timestamp_field"),
        },
        "未记录发布时间会引入前视偏差。",
        "保存 source_published_at/available_at，并在特征生成时执行交易日滞后。",
    )
    return point_in_time, survivorship, timing


def _adjustment_check(project: Path, start: str, end: str) -> dict[str, Any]:
    candidates = [
        project / "data" / "processed" / "yinhe_adj_factor" / "manifest.json",
        project / "data" / "processed" / "yinhe_adjustment" / "manifest.json",
    ]
    manifest_path = next((path for path in candidates if path.exists()), candidates[0])
    manifest = _load_json(manifest_path)
    mode = str((manifest or {}).get("mode") or "")
    continuity_breaks = (manifest or {}).get("continuity_breaks")
    passed = bool(
        manifest
        and "forward" in mode.lower()
        and _date_key(manifest.get("coverage_start")) <= start
        and _date_key(manifest.get("coverage_end")) >= end
        and continuity_breaks == 0
        and manifest.get("raw_prices_preserved") is True
    )
    return _check(
        "forward_adjustment_continuity",
        "price_adjustment",
        "pass" if passed else "blocked",
        "critical",
        "前复权价格连续且原始价格保留" if passed else "前复权因子与连续性尚未验收",
        {
            "manifest_path": str(manifest_path),
            "manifest_present": manifest is not None,
            "mode": mode or None,
            "continuity_breaks": continuity_breaks,
            "coverage_start": (manifest or {}).get("coverage_start"),
            "coverage_end": (manifest or {}).get("coverage_end"),
        },
        "未处理分红送转会产生虚假收益和动量信号。",
        "接入 QueryExFactorTable，生成前复权序列并对除权日前后连续性做自动检查。",
    )


def _infer_turnover_unit(files: list[Path], *, sample_limit: int = 100_000) -> float | None:
    samples: list[float] = []
    for path in files:
        with path.open("r", encoding="utf-8-sig", newline="") as handle:
            for row in csv.DictReader(handle):
                close = _float(row.get("close"))
                volume = _float(row.get("volume"))
                turnover = _float(row.get("turnover"))
                if close and volume and turnover and close > 0 and volume > 0 and turnover > 0:
                    samples.append(turnover / volume / close)
                    if len(samples) >= sample_limit:
                        return median(samples)
    return median(samples) if samples else None


def repair_yinhe_turnover_units(
    project: Path,
    *,
    start_date: str,
    end_date: str,
) -> dict[str, Any]:
    start = _date_key(start_date)
    end = _date_key(end_date)
    if not start or not end or start > end:
        raise ValueError("修复起止日期无效")
    daily_dir = project / "data" / "processed" / "yinhe_daily"
    files = sorted(
        path for path in daily_dir.glob("*.csv") if start <= _date_key(path.stem) <= end
    )
    if not files:
        raise ValueError(f"未找到待修复银河日线：{daily_dir}")
    state_path = project / "state" / f"yinhe_turnover_repair_{start}_{end}.json"
    state = _load_json(state_path) or {}
    completed = set(state.get("completed_files") or [])
    scale = float(state.get("scale") or 0)
    before_median = state.get("before_unit_median")
    if not scale:
        before_median = _infer_turnover_unit(files)
        if before_median is None:
            raise ValueError("无法从现有数据推断成交额单位")
        if 0.2 <= before_median <= 5.0:
            return {
                "status": "already_normalized",
                "files": len(files),
                "unit_median": before_median,
                "scale": 1,
            }
        if not 0.0002 <= before_median <= 0.005:
            raise ValueError(f"成交额单位比值 {before_median} 不在可安全修复范围")
        scale = 1_000.0

    state_path.parent.mkdir(parents=True, exist_ok=True)
    repaired_rows = int(state.get("repaired_rows") or 0)
    for index, path in enumerate(files, 1):
        relative = str(path.relative_to(project))
        if relative in completed:
            continue
        with path.open("r", encoding="utf-8-sig", newline="") as handle:
            reader = csv.DictReader(handle)
            fieldnames = tuple(reader.fieldnames or ())
            rows = list(reader)
        if "turnover" not in fieldnames:
            raise ValueError(f"{path} 缺少 turnover 字段")
        for row in rows:
            value = _float(row.get("turnover"))
            if value is not None:
                row["turnover"] = value * scale
                repaired_rows += 1
        temporary = path.with_suffix(".csv.tmp")
        with temporary.open("w", encoding="utf-8", newline="") as handle:
            writer = csv.DictWriter(handle, fieldnames=fieldnames, extrasaction="ignore")
            writer.writeheader()
            writer.writerows(rows)
        temporary.replace(path)
        completed.add(relative)
        state = {
            "schema_version": 1,
            "status": "running",
            "start_date": start,
            "end_date": end,
            "scale": scale,
            "before_unit_median": before_median,
            "completed_files": sorted(completed),
            "repaired_rows": repaired_rows,
        }
        state_path.write_text(json.dumps(state, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        if index % 100 == 0 or index == len(files):
            print(f"银河成交额单位修复：{index}/{len(files)}")

    after_median = _infer_turnover_unit(files)
    state.update(
        {
            "status": "completed",
            "completed_at": datetime.now(UTC).isoformat(),
            "after_unit_median": after_median,
            "raw_data_preserved": True,
        }
    )
    state_path.write_text(json.dumps(state, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    manifest_path = project / "data" / "processed" / "yinhe_turnover_unit_manifest.json"
    manifest = {
        key: value for key, value in state.items() if key != "completed_files"
    }
    manifest["state_path"] = str(state_path)
    manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return {
        "status": "completed",
        "files": len(files),
        "repaired_rows": repaired_rows,
        "scale": scale,
        "before_unit_median": before_median,
        "after_unit_median": after_median,
        "state_path": str(state_path),
        "manifest_path": str(manifest_path),
    }
def render_acceptance_report(report: dict[str, Any]) -> str:
    profile = report["dataset_profile"]
    readiness = report["readiness"]
    lines = [
        "# 银河三年日线数据验收报告",
        "",
        f"- 生成时间：{report['generated_at']}",
        f"- 数据版本：`{report['data_version']}`",
        f"- 验证协议：`{report['protocol']['protocol_id']}` / `{report['protocol']['sha256']}`",
        f"- 总体状态：**{report['status']}**",
        f"- 原始量价研究可用：**{readiness['raw_price_research_ready']}**",
        f"- 严格样本外回测可用：**{readiness['strict_backtest_ready']}**",
        "",
        "## 数据概况",
        "",
        f"- 日期范围：{profile['first_date']} 至 {profile['last_date']}",
        f"- 数据文件：{profile['file_count']} 个",
        f"- 总行数：{profile['row_count']:,}",
        f"- 当前股票池：{profile['current_universe_symbols']:,} 只",
        f"- 日覆盖率中位数：{profile['median_current_universe_coverage']:.2%}",
        f"- 重复主键：{profile['duplicate_keys']:,}",
        f"- 无效 OHLC：{profile['invalid_ohlc_rows']:,}",
        f"- 零成交额比例：{profile['zero_turnover_ratio']:.2%}",
        "",
        "## 验收项",
        "",
        "| 验收项 | 状态 | 严重度 | 结论 |",
        "|---|---|---|---|",
    ]
    for item in report["checks"]:
        lines.append(
            f"| `{item['check_id']}` | {item['status']} | {item['severity']} | "
            f"{item['summary']} |"
        )
    lines.extend(["", "## 未决阻塞", ""])
    blocked = [item for item in report["checks"] if item["status"] == "blocked"]
    if blocked:
        for item in blocked:
            lines.append(f"- **{item['summary']}**：{item['remediation']}")
    else:
        lines.append("- 无。")
    lines.extend(
        [
            "",
            "## 校验与版本",
            "",
            f"- 聚合 SHA-256：`{report['manifest']['aggregate_sha256']}`",
            f"- Manifest：`{report['manifest']['path']}`",
            "",
            "本报告只判断数据是否适合研究和严格回测，不构成策略有效性或投资结论。",
            "",
        ]
    )
    return "\n".join(lines)


def run_yinhe_acceptance(
    project: Path,
    *,
    start_date: str,
    end_date: str,
    calendar_file: Path | None = None,
    output_dir: Path | None = None,
) -> dict[str, Any]:
    start = _date_key(start_date)
    end = _date_key(end_date)
    if not start or not end or start > end:
        raise ValueError("验收起止日期无效")
    protocol = load_protocol(project)
    thresholds = protocol["document"]["data_acceptance"]
    daily_dir = project / "data" / "processed" / "yinhe_daily"
    files = sorted(
        path
        for path in daily_dir.glob("*.csv")
        if start <= _date_key(path.stem) <= end
    )
    if not files:
        raise ValueError(f"未找到验收区间内的银河日线：{daily_dir}")

    symbols, securities, state_mismatches = _load_current_universe(project)
    calendar_path = calendar_file or project / "data" / "processed" / "trade_calendar.csv"
    expected_dates, calendar_path = _load_calendar(calendar_path, start, end)
    actual_dates: list[str] = []
    daily_metrics: list[dict[str, Any]] = []
    manifest_files: list[dict[str, Any]] = []
    previous_close: dict[str, float] = {}
    unit_samples: list[float] = []
    row_count = 0
    duplicate_keys = 0
    invalid_ohlc = 0
    wrong_dates = 0
    negative_volume_turnover = 0
    zero_turnover = 0
    large_raw_jumps = 0

    for path in files:
        day = _date_key(path.stem)
        actual_dates.append(day)
        observed: set[str] = set()
        rows_in_file = 0
        file_duplicates = 0
        file_invalid = 0
        gem_counts = {"300": 0, "301": 0}
        with path.open("r", encoding="utf-8-sig", newline="") as handle:
            reader = csv.DictReader(handle)
            missing_columns = DAILY_REQUIRED_FIELDS - set(reader.fieldnames or [])
            if missing_columns:
                raise ValueError(f"{path} 缺少字段：{sorted(missing_columns)}")
            for row in reader:
                rows_in_file += 1
                row_count += 1
                symbol = str(row.get("symbol") or "").split(".", 1)[0].zfill(6)
                if symbol in observed:
                    duplicate_keys += 1
                    file_duplicates += 1
                observed.add(symbol)
                wrong_dates += int(_date_key(row.get("trade_date")) != day)
                if symbol[:3] in gem_counts:
                    gem_counts[symbol[:3]] += 1
                prices = [_float(row.get(field)) for field in ("open", "high", "low", "close")]
                volume = _float(row.get("volume"))
                turnover = _float(row.get("turnover"))
                suspended = str(row.get("is_suspended") or "0").lower() in {"1", "true", "yes"}
                valid_prices = all(value is not None and value > 0 for value in prices)
                if valid_prices:
                    open_price, high, low, close = (float(value) for value in prices)
                    valid_prices = high >= max(open_price, close) and low <= min(open_price, close)
                if not valid_prices and not suspended:
                    invalid_ohlc += 1
                    file_invalid += 1
                if volume is None or turnover is None or volume < 0 or turnover < 0:
                    negative_volume_turnover += 1
                elif turnover == 0:
                    zero_turnover += 1
                elif volume > 0 and valid_prices and len(unit_samples) < 100_000:
                    unit_samples.append(turnover / volume / float(prices[3]))
                close_value = float(prices[3]) if valid_prices else None
                previous = previous_close.get(symbol)
                if previous and close_value and abs(close_value / previous - 1) > 0.35:
                    large_raw_jumps += 1
                if close_value:
                    previous_close[symbol] = close_value

        eligible = {
            symbol
            for symbol in symbols
            if not _date_key(securities.get(symbol, {}).get("list_date"))
            or _date_key(securities.get(symbol, {}).get("list_date")) <= day
        }
        covered = len(observed & eligible)
        coverage = covered / len(eligible) if eligible else 0.0
        daily_metrics.append(
            {
                "trade_date": day,
                "rows": rows_in_file,
                "unique_symbols": len(observed),
                "eligible_current_universe": len(eligible),
                "covered_current_universe": covered,
                "current_universe_coverage": round(coverage, 6),
                "duplicates": file_duplicates,
                "invalid_ohlc": file_invalid,
                "gem_300_rows": gem_counts["300"],
                "gem_301_rows": gem_counts["301"],
            }
        )
        manifest_files.append(
            {
                "path": str(path.relative_to(project)),
                "trade_date": day,
                "size_bytes": path.stat().st_size,
                "rows": rows_in_file,
                "sha256": file_sha256(path),
            }
        )

    actual_set = set(actual_dates)
    expected_set = set(expected_dates)
    missing_dates = sorted(expected_set - actual_set)
    unexpected_dates = sorted(actual_set - expected_set) if expected_dates else []
    coverage_values = [item["current_universe_coverage"] for item in daily_metrics]
    median_coverage = median(coverage_values)
    latest = daily_metrics[-1]
    zero_ratio = zero_turnover / row_count if row_count else 0.0
    unit_median = median(unit_samples) if unit_samples else None
    manifest_payload = {
        "schema_version": 1,
        "provider": "china_galaxy_yinhe",
        "dataset": "daily_ohlcv_raw",
        "coverage_start": actual_dates[0],
        "coverage_end": actual_dates[-1],
        "protocol_sha256": protocol["protocol_sha256"],
        "files": manifest_files,
    }
    auxiliary_paths = [
        project / "data" / "processed" / "yinhe_symbols.txt",
        project / "data" / "processed" / "yinhe_securities.csv",
        calendar_path,
    ]
    manifest_payload["auxiliary_files"] = [
        {
            "path": str(path.relative_to(project)) if path.is_relative_to(project) else str(path),
            "size_bytes": path.stat().st_size,
            "sha256": file_sha256(path),
        }
        for path in auxiliary_paths
        if path is not None and path.exists()
    ]
    aggregate_sha256 = hashlib.sha256(
        json.dumps(manifest_files, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    manifest_payload["aggregate_sha256"] = aggregate_sha256
    manifest_path = project / "data" / "processed" / "yinhe_daily_manifest.json"
    manifest_path.write_text(
        json.dumps(manifest_payload, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )

    checks: list[dict[str, Any]] = []
    checks.append(
        _check(
            "frozen_validation_protocol",
            "governance",
            "pass",
            "critical",
            "验证协议哈希锁有效",
            {"protocol_sha256": protocol["protocol_sha256"]},
            "防止观察结果后静默修改验证口径。",
            "任何变化必须新建协议版本并重新冻结。",
        )
    )
    if expected_dates:
        range_ok = actual_dates[0] <= expected_dates[0] and actual_dates[-1] >= expected_dates[-1]
        range_status = "pass" if range_ok else "fail"
    else:
        range_ok = False
        range_status = "blocked"
    checks.append(
        _check(
            "requested_date_range",
            "completeness",
            range_status,
            "critical",
            (
                "数据覆盖独立日历定义的目标交易日边界"
                if range_status == "pass"
                else "数据未覆盖独立日历定义的边界"
                if range_status == "fail"
                else "缺少独立交易日历，无法判定首尾自然日对应的交易日"
            ),
            {
                "requested_start": start,
                "requested_end": end,
                "actual_start": actual_dates[0],
                "actual_end": actual_dates[-1],
            },
            "缺少首尾区间会使训练、样本外或最终留出集不完整。",
            "接入独立交易日历；若确认缺少边界交易日，再继续断点回填。",
        )
    )
    calendar_status = "pass" if expected_dates and not missing_dates else ("fail" if expected_dates else "blocked")
    checks.append(
        _check(
            "official_trading_calendar",
            "completeness",
            calendar_status,
            "critical",
            (
                "官方交易日完整"
                if calendar_status == "pass"
                else "存在缺失交易日"
                if calendar_status == "fail"
                else "缺少独立交易日历，无法证明整日无缺口"
            ),
            {
                "calendar_path": str(calendar_path),
                "expected_dates": len(expected_dates),
                "actual_files": len(actual_dates),
                "missing_dates": missing_dates[:50],
                "unexpected_dates": unexpected_dates[:50],
            },
            "整日缺失会同时影响所有股票且不易从覆盖率发现。",
            "接入交易所或独立供应商交易日历后重跑。",
        )
    )
    coverage_ok = (
        median_coverage >= float(thresholds["minimum_median_daily_coverage"])
        and latest["current_universe_coverage"] >= float(thresholds["minimum_latest_coverage"])
    )
    lowest = sorted(daily_metrics, key=lambda item: item["current_universe_coverage"])[:20]
    checks.append(
        _check(
            "daily_symbol_coverage",
            "completeness",
            "pass" if coverage_ok else "fail",
            "high",
            "每日股票覆盖率达到当前股票池代理门槛" if coverage_ok else "每日股票覆盖率不足",
            {
                "median": median_coverage,
                "latest": latest["current_universe_coverage"],
                "threshold_median": thresholds["minimum_median_daily_coverage"],
                "threshold_latest": thresholds["minimum_latest_coverage"],
                "lowest_days": lowest,
                "denominator": "current universe filtered by current list_date",
            },
            "低覆盖率会改变横截面排名；当前口径仍不能替代历史时点股票池。",
            "补齐低覆盖日期，并在 security_history 完成后改用历史分母复验。",
        )
    )
    gem_latest = {"300": latest["gem_300_rows"], "301": latest["gem_301_rows"]}
    gem_ok = all(gem_latest[prefix] > 0 for prefix in thresholds["required_gem_prefixes"])
    checks.append(
        _check(
            "gem_300_301_inclusion",
            "universe",
            "pass" if gem_ok else "fail",
            "high",
            "创业板 300、301 股票均已包含" if gem_ok else "创业板 300 或 301 股票缺失",
            {"latest_trade_date": latest["trade_date"], "latest_counts": gem_latest},
            "板块缺失会造成系统性的市场覆盖偏差。",
            "检查证券类型、市场映射和股票池构建规则。",
        )
    )
    state_ok = not sum(state_mismatches.values())
    checks.append(
        _check(
            "current_security_state_consistency",
            "universe",
            "pass" if state_ok else "warn",
            "medium",
            "当前 ST/退市风险标记与名称一致" if state_ok else "当前证券状态标记存在不一致",
            {"mismatches": state_mismatches, "security_rows": len(securities)},
            "当前状态不一致会污染最新候选池。",
            "核对银河状态编码并修正标准化映射。",
        )
    )
    checks.extend(_status_manifest_checks(project, start, end))
    uniqueness_ok = duplicate_keys <= int(thresholds["maximum_duplicate_keys"]) and wrong_dates == 0
    checks.append(
        _check(
            "daily_primary_key_uniqueness",
            "uniqueness",
            "pass" if uniqueness_ok else "fail",
            "critical",
            "symbol+trade_date 主键唯一且日期一致" if uniqueness_ok else "存在重复主键或日期错位",
            {"duplicate_keys": duplicate_keys, "wrong_date_rows": wrong_dates},
            "重复或错日会直接扭曲收益、成交量和信号。",
            "定位对应日期文件，按供应商主键去重并保留冲突证据。",
        )
    )
    price_ok = invalid_ohlc <= int(thresholds["maximum_invalid_ohlc_rows"]) and negative_volume_turnover == 0
    checks.append(
        _check(
            "ohlcv_domain_validity",
            "validity",
            "pass" if price_ok else "fail",
            "critical",
            "OHLC 与成交字段满足基本域规则" if price_ok else "发现异常价格或负成交字段",
            {
                "invalid_ohlc_rows": invalid_ohlc,
                "negative_volume_or_turnover_rows": negative_volume_turnover,
                "large_raw_close_jumps_over_35pct": large_raw_jumps,
            },
            "无效价格会产生不可解释收益；大跳变需结合复权因子复核。",
            "修复无效行，并在复权后复验大幅跳变。",
        )
    )
    zero_ok = zero_ratio <= float(thresholds["maximum_zero_turnover_ratio"])
    checks.append(
        _check(
            "zero_turnover_rate",
            "validity",
            "pass" if zero_ok else "warn",
            "medium",
            "零成交额比例在门槛内" if zero_ok else "零成交额比例偏高",
            {"zero_turnover_rows": zero_turnover, "zero_turnover_ratio": zero_ratio},
            "可能代表停牌，也可能是字段映射或单位问题。",
            "结合历史停牌状态区分合理零值与数据缺失。",
        )
    )
    units_ok = unit_median is not None and 0.2 <= unit_median <= 5.0
    checks.append(
        _check(
            "price_volume_turnover_units",
            "consistency",
            "pass" if units_ok else "fail",
            "high",
            "成交额/成交量/价格关系支持元、股口径" if units_ok else "无法确认成交额与成交量单位",
            {
                "sample_count": len(unit_samples),
                "median_turnover_div_volume_close": unit_median,
                "expected_near": 1.0,
                "declared_units": {
                    "price": thresholds["price_unit"],
                    "volume": thresholds["volume_unit"],
                    "turnover": thresholds["turnover_unit"],
                },
            },
            "单位错误会将流动性和容量指标放大或缩小 100/1000 倍。",
            "与供应商字段文档及另一数据源抽样交叉核对。",
        )
    )
    checks.append(_adjustment_check(project, start, end))
    checks.append(
        _check(
            "versioned_manifest_hashes",
            "lineage",
            "pass",
            "critical",
            "已生成逐文件哈希、聚合哈希和数据版本",
            {
                "file_count": len(manifest_files),
                "aggregate_sha256": aggregate_sha256,
                "manifest_path": str(manifest_path),
            },
            "可检测静默改写并复现具体数据版本。",
            "每日增量后生成新 manifest，历史报告继续引用旧聚合哈希。",
        )
    )

    any_fail = any(item["status"] == "fail" for item in checks)
    critical_fail = any(item["status"] == "fail" and item["severity"] == "critical" for item in checks)
    critical_blocked = any(
        item["status"] == "blocked" and item["severity"] == "critical" for item in checks
    )
    status = (
        "failed"
        if any_fail
        else "blocked_for_strict_backtest"
        if critical_blocked
        else "passed_with_warnings"
        if any(item["status"] == "warn" for item in checks)
        else "passed"
    )
    generated_at = datetime.now(UTC).isoformat()
    data_version = f"yinhe_daily_{start}_{end}_{aggregate_sha256[:12]}"
    report = {
        "schema_version": 1,
        "generated_at": generated_at,
        "status": status,
        "data_version": data_version,
        "scope": {"start_date": start, "end_date": end, "grain": "symbol_trade_date"},
        "protocol": {
            "protocol_id": protocol["document"]["protocol_id"],
            "version": protocol["document"]["version"],
            "sha256": protocol["protocol_sha256"],
        },
        "readiness": {
            "ingestion_integrity_ready": not any_fail,
            "raw_price_research_ready": not any_fail and coverage_ok and gem_ok and units_ok,
            "strict_backtest_ready": status in {"passed", "passed_with_warnings"},
            "execution_allowed": False,
        },
        "dataset_profile": {
            "first_date": actual_dates[0],
            "last_date": actual_dates[-1],
            "file_count": len(files),
            "row_count": row_count,
            "current_universe_symbols": len(symbols),
            "median_current_universe_coverage": median_coverage,
            "latest_current_universe_coverage": latest["current_universe_coverage"],
            "duplicate_keys": duplicate_keys,
            "invalid_ohlc_rows": invalid_ohlc,
            "wrong_date_rows": wrong_dates,
            "zero_turnover_ratio": zero_ratio,
            "large_raw_close_jumps_over_35pct": large_raw_jumps,
            "unit_inference_median": unit_median,
        },
        "checks": checks,
        "daily_metrics": daily_metrics,
        "manifest": {
            "path": str(manifest_path.relative_to(project)),
            "aggregate_sha256": aggregate_sha256,
            "file_count": len(manifest_files),
        },
    }
    folder = output_dir or project / "reports" / "yinhe_acceptance"
    history = folder / "history"
    folder.mkdir(parents=True, exist_ok=True)
    history.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    json_content = json.dumps(report, ensure_ascii=False, indent=2) + "\n"
    markdown_content = render_acceptance_report(report)
    latest_json = folder / "latest.json"
    latest_md = folder / "latest.md"
    historical_json = history / f"{stamp}_{aggregate_sha256[:12]}.json"
    historical_md = history / f"{stamp}_{aggregate_sha256[:12]}.md"
    latest_json.write_text(json_content, encoding="utf-8")
    latest_md.write_text(markdown_content, encoding="utf-8")
    historical_json.write_text(json_content, encoding="utf-8")
    historical_md.write_text(markdown_content, encoding="utf-8")
    return {
        "status": status,
        "data_version": data_version,
        "readiness": report["readiness"],
        "profile": report["dataset_profile"],
        "failed_checks": [
            item["check_id"] for item in checks if item["status"] == "fail"
        ],
        "blocked_checks": [
            item["check_id"] for item in checks if item["status"] == "blocked"
        ],
        "paths": {
            "json": str(latest_json),
            "markdown": str(latest_md),
            "manifest": str(manifest_path),
            "history_json": str(historical_json),
        },
    }

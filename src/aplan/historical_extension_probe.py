from __future__ import annotations

import argparse
import contextlib
import hashlib
import io
import json
import math
import os
import re
import tempfile
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Callable, Mapping

from .yinhe_adjustment import _factor_rows
from .yinhe_benchmarks import (
    MARKET_INDICES,
    _constituent_rows,
    _market_rows,
    _master_rows as _industry_master_rows,
    _records,
)
from .yinhe_security_history import (
    _master_rows as _security_master_rows,
    _status_rows,
    _symbol_key,
    _vendor_code,
)
from .yinhe_sync import YinheConfig, normalize_daily_rows


PROBE_DATES = ("20150105", "20150706", "20151231")
REPRESENTATIVE_SYMBOLS = ("600000", "000001", "600519", "000333", "300059", "300750")
REQUIRED_MARKET_INDICES = ("000001.SH", "000300.SH", "000905.SH", "399001.SZ")
INDEX_LAUNCH_DATES = {"000688.SH": "20200723"}
JOIN_THRESHOLD = 0.98
INDUSTRY_THRESHOLD = 0.95
MAX_DELISTED_SAMPLES = 6
MAX_PERSISTED_SAMPLE_ROWS = 80
SCHEMA_VERSION = 1


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _date(value: Any) -> str:
    if hasattr(value, "strftime"):
        return value.strftime("%Y%m%d")
    digits = "".join(character for character in str(value or "") if character.isdigit())
    return digits[:8] if len(digits) >= 8 else ""


def _field(row: Mapping[str, Any], *names: str) -> Any:
    upper = {str(key).upper(): value for key, value in row.items()}
    for name in names:
        value = upper.get(name.upper())
        if value not in (None, ""):
            return value
    return None


def _unwrap(value: Any) -> tuple[Any, list[str]]:
    if (
        isinstance(value, (list, tuple))
        and len(value) == 2
        and isinstance(value[1], list)
    ):
        return value[0], [_redact(str(item))[:300] for item in value[1]]
    return value, []


def _redact(message: str) -> str:
    message = re.sub(
        r"(?i)(token|password|username)\s*['\":=]+\s*[^,\s}\]]+",
        r"\1=<redacted>",
        message,
    )
    return re.sub(
        r"\b[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-"
        r"[0-9a-fA-F]{4}-[0-9a-fA-F]{12}\b",
        "<redacted-uuid>",
        message,
    )


def _weight_rows(value: Any, dates: set[str]) -> list[dict[str, Any]]:
    payload, errors = _unwrap(value)
    if errors:
        raise ValueError(f"industry_weight: {errors}")
    output: list[dict[str, Any]] = []
    for row in _records(payload):
        day = _date(_field(row, "TRADE_DATE", "DATE", "INDEX"))
        symbol = _symbol_key(_field(row, "CON_CODE", "SECURITY_CODE", "SYMBOL"))
        index_code = str(_field(row, "INDEX_CODE") or "").upper().replace("_", ".")
        if day not in dates or not symbol or not index_code:
            continue
        try:
            weight = float(_field(row, "WEIGHT"))
        except (TypeError, ValueError):
            weight = math.nan
        output.append(
            {
                "trade_date": day,
                "symbol": symbol,
                "index_code": index_code,
                "weight": weight if math.isfinite(weight) else None,
            }
        )
    return output


def _listed_on(row: Mapping[str, Any], day: str) -> bool:
    listed = _date(row.get("list_date"))
    delisted = _date(row.get("delist_date"))
    return bool(listed and listed <= day and (not delisted or day < delisted))


def _constituent_map(
    rows: list[dict[str, Any]],
    dates: tuple[str, ...],
) -> dict[tuple[str, str], str]:
    result: dict[tuple[str, str], str] = {}
    for row in rows:
        start = _date(row.get("in_date"))
        end = _date(row.get("out_date"))
        symbol = _symbol_key(row.get("symbol"))
        index_code = str(row.get("index_code") or "")
        if not start or not symbol or not index_code:
            continue
        for day in dates:
            if start <= day and (not end or day < end):
                result.setdefault((day, symbol), index_code)
    return result


def _safe_call(
    label: str,
    function: Callable[[], Any],
    errors: list[dict[str, str]],
) -> Any:
    try:
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            return function()
    except Exception as exc:  # noqa: BLE001 - supplier exceptions are not stable
        message = _redact(str(exc))
        errors.append(
            {
                "layer": label,
                "exception_type": type(exc).__name__,
                "message": message[:500],
            }
        )
        return None


def _ratio(numerator: int, denominator: int) -> float | None:
    return round(numerator / denominator, 6) if denominator else None


def evaluate_probe(
    *,
    dates: tuple[str, ...],
    historical_pools: dict[str, set[str]],
    master_rows: list[dict[str, str]],
    sample_symbols: list[str],
    daily_rows: list[dict[str, Any]],
    status_rows: list[tuple[Any, ...]],
    factor_rows: list[tuple[str, str, float]],
    market_rows: list[dict[str, Any]],
    weight_rows: list[dict[str, Any]],
    constituent_rows: list[dict[str, Any]],
    errors: list[dict[str, str]],
) -> dict[str, Any]:
    master = {row["symbol"]: row for row in master_rows}
    expected = {
        (day, symbol)
        for day in dates
        for symbol in sample_symbols
        if symbol in historical_pools.get(day, set())
        and _listed_on(master.get(symbol, {}), day)
    }
    legal_absence = [
        {"trade_date": day, "symbol": symbol, "reason": "not_listed_or_not_in_historical_pool"}
        for day in dates
        for symbol in sample_symbols
        if (day, symbol) not in expected
    ]
    raw_keys = {
        (_date(row.get("trade_date")), _symbol_key(row.get("symbol")))
        for row in daily_rows
    }
    status_map = {
        (_date(row[0]), _symbol_key(row[1])): {"is_suspended": bool(row[3])}
        for row in status_rows
    }
    status_keys = set(status_map)
    factor_keys = {(_date(row[0]), _symbol_key(row[1])) for row in factor_rows}
    suspended = {
        key
        for key in expected
        if status_map.get(key, {}).get("is_suspended")
    }
    legal_absence.extend(
        {
            "trade_date": day,
            "symbol": symbol,
            "reason": "suspended_no_daily_bar",
        }
        for day, symbol in sorted(suspended - raw_keys)
    )
    tradable_expected = expected - suspended
    triple_keys = tradable_expected & raw_keys & status_keys & factor_keys

    weight_map = {
        (row["trade_date"], row["symbol"]): row["index_code"]
        for row in weight_rows
    }
    constituent_map = _constituent_map(constituent_rows, dates)
    industry_source = "daily_weight" if weight_rows else "constituent_interval"
    industry_map = weight_map if weight_rows else constituent_map
    industry_keys = expected & set(industry_map)
    outdate_disagreements = sum(
        key in weight_map and key in constituent_map and weight_map[key] != constituent_map[key]
        for key in expected
    )

    market_keys = {
        (row["trade_date"], row["index_code"]) for row in market_rows
    }
    required_market_expected = {
        (day, code) for day in dates for code in REQUIRED_MARKET_INDICES
    }
    prelaunch = [
        {"trade_date": day, "index_code": code, "reason": "index_not_launched"}
        for day in dates
        for code, launch in INDEX_LAUNCH_DATES.items()
        if day < launch and (day, code) not in market_keys
    ]
    missing_market = sorted(required_market_expected - market_keys)

    delisted = [
        {
            "symbol": symbol,
            "name": master[symbol].get("name", ""),
            "list_date": master[symbol].get("list_date", ""),
            "delist_date": master[symbol].get("delist_date", ""),
            "present_dates": [
                day for day in dates if symbol in historical_pools.get(day, set())
            ],
        }
        for symbol in sample_symbols
        if master.get(symbol, {}).get("delist_date")
        and master[symbol]["delist_date"] < "20230101"
    ]

    join_coverage = _ratio(len(triple_keys), len(tradable_expected))
    industry_coverage = _ratio(len(industry_keys), len(expected))
    historical_pool_ok = all(historical_pools.get(day) for day in dates) and bool(delisted)
    join_ok = join_coverage is not None and join_coverage >= JOIN_THRESHOLD
    market_ok = not missing_market
    industry_weight_ok = (
        industry_source == "daily_weight"
        and industry_coverage is not None
        and industry_coverage >= INDUSTRY_THRESHOLD
    )
    constituent_interval_ok = (
        industry_source == "constituent_interval"
        and industry_coverage is not None
        and industry_coverage >= INDUSTRY_THRESHOLD
    )
    matrix = {
        "historical_security_pool": {
            "status": "passed" if historical_pool_ok else "blocked",
            "pool_sizes": {day: len(historical_pools.get(day, set())) for day in dates},
            "delisted_sample_count": len(delisted),
            "code_lineage": "partial_no_general_a_share_mapping_api",
        },
        "raw_daily": {
            "status": "passed" if tradable_expected <= raw_keys else "blocked",
            "coverage": _ratio(
                len(tradable_expected & raw_keys), len(tradable_expected)
            ),
            "suspended_without_bar": len(suspended - raw_keys),
        },
        "daily_security_status": {
            "status": "passed" if expected <= status_keys else "blocked",
            "coverage": _ratio(len(expected & status_keys), len(expected)),
        },
        "backward_factor_join": {
            "status": "passed" if expected <= factor_keys else "blocked",
            "coverage": _ratio(len(expected & factor_keys), len(expected)),
            "absolute_adjusted_prices_built": False,
        },
        "raw_status_factor_join": {
            "status": "passed" if join_ok else "blocked",
            "coverage": join_coverage,
            "threshold": JOIN_THRESHOLD,
        },
        "official_market_indices": {
            "status": "passed" if market_ok else "blocked",
            "required_pairs": len(required_market_expected),
            "observed_pairs": len(required_market_expected & market_keys),
            "missing_pairs": [
                {"trade_date": day, "index_code": code}
                for day, code in missing_market
            ],
            "legal_prelaunch": prelaunch,
        },
        "shenwan_level1_pit": {
            "status": "passed" if industry_weight_ok else "blocked",
            "source": industry_source,
            "coverage": industry_coverage,
            "threshold": INDUSTRY_THRESHOLD,
            "constituent_interval_coverage": (
                _ratio(len(expected & set(constituent_map)), len(expected))
            ),
            "constituent_interval_structurally_complete": constituent_interval_ok,
            "outdate_revision_semantics": (
                "unsafe_as_strict_pit_without_daily_weight_archive"
            ),
            "weight_vs_interval_disagreements": outdate_disagreements,
        },
    }
    market_only = historical_pool_ok and join_ok and market_ok
    market_and_industry = market_only and industry_weight_ok
    if market_and_industry:
        recommendation = "freeze_market_and_industry_history_protocol"
        overall = "passed"
    elif market_only:
        recommendation = "freeze_market_only_history_protocol"
        overall = "passed_market_only"
    else:
        recommendation = "blocked_do_not_start_bulk_download"
        overall = "blocked"
    return {
        "schema_version": SCHEMA_VERSION,
        "status": overall,
        "probe_dates": list(dates),
        "sample_symbols": sample_symbols,
        "expected_symbol_dates": len(expected),
        "expected_tradable_symbol_dates": len(tradable_expected),
        "legal_absence": legal_absence,
        "delisted_evidence": delisted,
        "matrix": matrix,
        "recommendation": recommendation,
        "supplier_errors": errors,
        "bulk_download_started": False,
        "models_run": False,
        "model_registry_modified": False,
        "rows_from_2025_or_later": 0,
        "holdout_2026_opened": False,
    }


def run_probe(project: Path, env_file: Path) -> dict[str, Any]:
    dates = PROBE_DATES
    project = project.resolve()
    output = project / "reports" / "historical_extension_probe"
    output.mkdir(parents=True, exist_ok=True)
    errors: list[dict[str, str]] = []
    config = YinheConfig.from_env(env_file)
    ad: Any | None = None

    historical_pools: dict[str, set[str]] = {}
    master_rows: list[dict[str, str]] = []
    sample_symbols: list[str] = []
    daily_rows: list[dict[str, Any]] = []
    statuses: list[tuple[Any, ...]] = []
    factors: list[tuple[str, str, float]] = []
    market_rows: list[dict[str, Any]] = []
    weights: list[dict[str, Any]] = []
    constituents: list[dict[str, Any]] = []

    with tempfile.TemporaryDirectory(prefix="aplan_historical_probe_") as cache_raw:
        cache = f"{Path(cache_raw).resolve()}{os.sep}"
        try:
            import AmazingData as ad_module  # type: ignore[import-not-found]

            ad = ad_module
            logged_in = _safe_call(
                "login",
                lambda: ad.login(
                    username=config.username,
                    password=config.password,
                    host=config.server_vip,
                    port=config.server_port,
                ),
                errors,
            )
            if not logged_in:
                raise ValueError("AmazingData login failed")
            base = ad.BaseData()
            info = ad.InfoData()
            calendar = list(_safe_call("calendar", base.get_calendar, errors) or [])
            calendar_dates = {_date(day) for day in calendar}
            if not set(dates) <= calendar_dates:
                raise ValueError("Frozen probe dates are not all official trading dates")
            market = ad.MarketData(calendar)

            for day in dates:
                codes = _safe_call(
                    f"historical_code_pool:{day}",
                    lambda day=day: base.get_hist_code_list(
                        security_type="EXTRA_STOCK_A_SH_SZ",
                        start_date=int(day),
                        end_date=int(day),
                        local_path=cache,
                    ),
                    errors,
                ) or []
                historical_pools[day] = {
                    _symbol_key(code) for code in codes if _symbol_key(code)
                }
            union_codes = sorted(set().union(*historical_pools.values()))
            vendor_union = [_vendor_code(symbol) for symbol in union_codes]
            basic = _safe_call(
                "stock_basic",
                lambda: info.get_stock_basic(vendor_union),
                errors,
            )
            master_rows = _security_master_rows(basic, vendor_union)
            master = {row["symbol"]: row for row in master_rows}
            delisted = sorted(
                (
                    symbol
                    for symbol in union_codes
                    if master.get(symbol, {}).get("delist_date")
                    and master[symbol]["delist_date"] < "20230101"
                ),
                key=lambda symbol: master[symbol]["delist_date"],
            )[:MAX_DELISTED_SAMPLES]
            sample_symbols = sorted(
                set(REPRESENTATIVE_SYMBOLS) | set(delisted)
            )
            vendor_samples = [_vendor_code(symbol) for symbol in sample_symbols]

            for day in dates:
                raw = _safe_call(
                    f"raw_daily:{day}",
                    lambda day=day: market.query_kline(
                        vendor_samples,
                        begin_date=int(day),
                        end_date=int(day),
                        period=ad.constant.Period.day.value,
                    ),
                    errors,
                )
                payload, payload_errors = _unwrap(raw)
                errors.extend(
                    {"layer": f"raw_daily:{day}", "exception_type": "supplier", "message": item}
                    for item in payload_errors
                )
                daily_rows.extend(normalize_daily_rows(_records(payload), day))
                status = _safe_call(
                    f"security_status:{day}",
                    lambda day=day: info.get_history_stock_status(
                        vendor_samples,
                        local_path=cache,
                        is_local=False,
                        begin_date=int(day),
                        end_date=int(day),
                    ),
                    errors,
                )
                statuses.extend(_status_rows(status, day, day))

            factor_frame = _safe_call(
                "backward_factor",
                lambda: base.get_backward_factor(
                    vendor_samples, local_path=cache, is_local=False
                ),
                errors,
            )
            if factor_frame is not None:
                factors = list(_factor_rows(factor_frame, dates[0], dates[-1]))
                factors = [row for row in factors if row[0] in set(dates)]

            for day in dates:
                indices = _safe_call(
                    f"market_indices:{day}",
                    lambda day=day: market.query_kline(
                        list(MARKET_INDICES),
                        begin_date=int(day),
                        end_date=int(day),
                        period=ad.constant.Period.day.value,
                    ),
                    errors,
                )
                market_rows.extend(_market_rows(indices, day, day))

            industry_base = _safe_call(
                "industry_base",
                lambda: info.get_industry_base_info(local_path=cache, is_local=False),
                errors,
            )
            industry_codes = [
                row["index_code"] for row in _industry_master_rows(industry_base)
            ]
            if industry_codes:
                for day in dates:
                    value = _safe_call(
                        f"industry_weight:{day}",
                        lambda day=day: info.get_industry_weight(
                            industry_codes,
                            local_path=cache,
                            is_local=False,
                            begin_date=int(day),
                            end_date=int(day),
                        ),
                        errors,
                    )
                    if value is not None:
                        weights.extend(_weight_rows(value, {day}))
                value = _safe_call(
                    "industry_constituents",
                    lambda: info.get_industry_constituent(
                        industry_codes, local_path=cache, is_local=False
                    ),
                    errors,
                )
                constituents = _constituent_rows(value)
        finally:
            if ad is not None and hasattr(ad, "logout"):
                _safe_call("logout", lambda: ad.logout(config.username), errors)

    evidence = evaluate_probe(
        dates=dates,
        historical_pools=historical_pools,
        master_rows=master_rows,
        sample_symbols=sample_symbols,
        daily_rows=daily_rows,
        status_rows=statuses,
        factor_rows=factors,
        market_rows=market_rows,
        weight_rows=weights,
        constituent_rows=constituents,
        errors=errors,
    )
    evidence["generated_at"] = datetime.now(UTC).isoformat()
    evidence["persisted_raw_supplier_responses"] = False
    evidence["compact_row_counts"] = {
        "raw_daily": len(daily_rows),
        "daily_status": len(statuses),
        "backward_factor": len(factors),
        "market_indices": len(market_rows),
        "industry_weights": len(weights),
        "industry_constituents_inspected": len(constituents),
    }
    evidence["samples"] = {
        "raw_daily": daily_rows[:MAX_PERSISTED_SAMPLE_ROWS],
        "daily_status": [
            {
                "trade_date": row[0],
                "symbol": row[1],
                "is_st": row[2],
                "is_suspended": row[3],
                "is_ex_dividend": row[4],
                "is_ex_right": row[5],
            }
            for row in statuses[:MAX_PERSISTED_SAMPLE_ROWS]
        ],
        "factor": [
            {"trade_date": row[0], "symbol": row[1], "factor_present": True}
            for row in factors[:MAX_PERSISTED_SAMPLE_ROWS]
        ],
    }
    evidence_path = output / "evidence.json"
    evidence_path.write_text(
        json.dumps(evidence, ensure_ascii=False, indent=2, default=str) + "\n",
        encoding="utf-8",
    )
    report_path = output / "report.md"
    report_path.write_text(_markdown(evidence), encoding="utf-8")
    manifest = {
        "schema_version": SCHEMA_VERSION,
        "status": evidence["status"],
        "provider": "China Galaxy AmazingData",
        "probe_dates": list(dates),
        "sample_symbols": sample_symbols,
        "recommendation": evidence["recommendation"],
        "thresholds": {
            "raw_status_factor_join": JOIN_THRESHOLD,
            "industry_pit": INDUSTRY_THRESHOLD,
        },
        "hashes": {
            "evidence.json": _sha256(evidence_path),
            "report.md": _sha256(report_path),
            "implementation": _sha256(Path(__file__)),
        },
        "constraints": {
            "bulk_download_started": False,
            "models_run": False,
            "model_registry_modified": False,
            "rows_from_2025_or_later": 0,
            "holdout_2026_opened": False,
        },
        "generated_at": evidence["generated_at"],
    }
    manifest_path = output / "manifest.json"
    manifest_path.write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    return {
        "status": evidence["status"],
        "probe_dates": list(dates),
        "sample_symbols": len(sample_symbols),
        "recommendation": evidence["recommendation"],
        "join_coverage": evidence["matrix"]["raw_status_factor_join"]["coverage"],
        "industry_pit_coverage": evidence["matrix"]["shenwan_level1_pit"]["coverage"],
        "supplier_errors": len(errors),
        "manifest": str(manifest_path),
        "report": str(report_path),
        "holdout_2026_opened": False,
    }


def _markdown(evidence: Mapping[str, Any]) -> str:
    lines = [
        "# 2015 历史扩展最小覆盖探针",
        "",
        f"- 状态：`{evidence['status']}`",
        f"- 日期：{', '.join(evidence['probe_dates'])}",
        f"- 样本证券：{len(evidence['sample_symbols'])}",
        f"- 建议：`{evidence['recommendation']}`",
        "- 2025/2026 数据读取：0 行；2026 留出集保持关闭",
        "- 未启动批量下载、未运行模型、未修改模型注册状态",
        "",
        "| 层 | 状态 | 覆盖率/说明 |",
        "|---|---|---|",
    ]
    for name, result in evidence["matrix"].items():
        coverage = result.get("coverage")
        detail = f"{coverage:.2%}" if isinstance(coverage, float) else ""
        if name == "official_market_indices":
            detail = f"{result['observed_pairs']}/{result['required_pairs']}"
        if name == "historical_security_pool":
            detail = (
                f"退市样本 {result['delisted_sample_count']}；"
                f"代码沿革 {result['code_lineage']}"
            )
        if name == "shenwan_level1_pit":
            detail = f"{detail}；source={result['source']}"
        lines.append(f"| `{name}` | `{result['status']}` | {detail} |")
    lines.extend(
        [
            "",
            "## 退市样本证据",
            "",
        ]
    )
    for row in evidence["delisted_evidence"]:
        lines.append(
            f"- {row['symbol']} {row['name']}："
            f"{row['list_date']} 至 {row['delist_date']}，"
            f"探针出现日={','.join(row['present_dates'])}"
        )
    lines.extend(
        [
            "",
            "## PIT 判断",
            "",
            "- 行业严格 PIT 仅接受当日行业权重；当前成分区间的 OUTDATE 会随最新数据修订，"
            "不能单独证明历史时点可见性。",
            "- 复权层只验证因子存在及同证券日连接，不构造或保存以最新时点归一化的绝对复权价格。",
            "- 合法不存在、证券当日未上市、指数尚未发布均与真实缺失分列。",
            "",
            "## 供应商错误",
            "",
        ]
    )
    if evidence["supplier_errors"]:
        for row in evidence["supplier_errors"]:
            lines.append(
                f"- `{row['layer']}` / `{row['exception_type']}`：{row['message']}"
            )
    else:
        lines.append("- 无")
    return "\n".join(lines) + "\n"


def main() -> None:
    parser = argparse.ArgumentParser(description="银河 2015 历史扩展三日期只读探针")
    parser.add_argument("--root", default=".")
    parser.add_argument("--env-file", default=".env")
    args = parser.parse_args()
    result = run_probe(Path(args.root), Path(args.env_file))
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()

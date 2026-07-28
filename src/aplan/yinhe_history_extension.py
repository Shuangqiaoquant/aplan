from __future__ import annotations

import argparse
import csv
import hashlib
import json
import sqlite3
from datetime import UTC, datetime
from pathlib import Path
from statistics import median
from typing import Any, Iterable


def _date_key(value: Any) -> str:
    text = "".join(character for character in str(value or "") if character.isdigit())
    return text[:8] if len(text) >= 8 else ""


def _symbol_key(value: Any) -> str:
    symbol = str(value or "").strip().split(".", 1)[0]
    return symbol if len(symbol) == 6 and symbol.isdigit() else ""


def _file_hash(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def build_historical_symbol_pool(
    project: Path,
    *,
    start_date: str,
    end_date: str,
    output_path: Path,
) -> dict[str, Any]:
    start, end = _date_key(start_date), _date_key(end_date)
    master_path = project / "data" / "processed" / "security_history" / "security_master.csv"
    if not master_path.exists():
        raise FileNotFoundError(f"缺少历史证券主数据：{master_path}")
    selected: set[str] = set()
    source_rows = 0
    excluded_not_yet_listed = 0
    excluded_already_delisted = 0
    with master_path.open(encoding="utf-8-sig", newline="") as handle:
        for row in csv.DictReader(handle):
            source_rows += 1
            symbol = _symbol_key(row.get("symbol") or row.get("ts_code"))
            list_date = _date_key(row.get("list_date"))
            delist_date = _date_key(row.get("delist_date"))
            if not symbol:
                continue
            if list_date and list_date > end:
                excluded_not_yet_listed += 1
                continue
            if delist_date and delist_date < start:
                excluded_already_delisted += 1
                continue
            selected.add(symbol)
    if not selected:
        raise ValueError("历史证券主数据未产生可回填股票池")
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text("\n".join(sorted(selected)) + "\n", encoding="utf-8")
    return {
        "status": "completed",
        "start_date": start,
        "end_date": end,
        "source_rows": source_rows,
        "symbols": len(selected),
        "excluded_not_yet_listed": excluded_not_yet_listed,
        "excluded_already_delisted": excluded_already_delisted,
        "output_path": str(output_path),
        "output_sha256": _file_hash(output_path),
    }


def _calendar_dates(path: Path, start: str, end: str) -> list[str]:
    with path.open(encoding="utf-8-sig", newline="") as handle:
        return sorted(
            {
                day
                for row in csv.DictReader(handle)
                if (day := _date_key(row.get("trade_date") or row.get("date")))
                and start <= day <= end
                and str(row.get("is_open") or "1").lower() not in {"0", "false", "no"}
            }
        )


def _profile_files(paths: Iterable[Path]) -> dict[str, Any]:
    rows = 0
    duplicate_keys = 0
    invalid_ohlc_rows = 0
    wrong_date_rows = 0
    missing_state_fields = 0
    row_counts: dict[str, int] = {}
    unit_samples: list[float] = []
    for path in paths:
        seen: set[tuple[str, str]] = set()
        file_rows = 0
        with path.open(encoding="utf-8-sig", newline="") as handle:
            reader = csv.DictReader(handle)
            fields = set(reader.fieldnames or ())
            missing_state_fields += int(
                not {"is_suspended", "is_limit_up", "is_limit_down"}.issubset(fields)
            )
            for row in reader:
                rows += 1
                file_rows += 1
                symbol = _symbol_key(row.get("symbol"))
                trade_date = _date_key(row.get("trade_date"))
                key = (trade_date, symbol)
                duplicate_keys += int(key in seen)
                seen.add(key)
                wrong_date_rows += int(trade_date != path.stem)
                try:
                    open_price = float(row.get("open") or 0)
                    high = float(row.get("high") or 0)
                    low = float(row.get("low") or 0)
                    close = float(row.get("close") or 0)
                except (TypeError, ValueError):
                    invalid_ohlc_rows += 1
                    continue
                invalid_ohlc_rows += int(
                    min(open_price, high, low, close) <= 0
                    or high < max(open_price, close, low)
                    or low > min(open_price, close, high)
                )
                if len(unit_samples) < 100_000:
                    try:
                        volume = float(row.get("volume") or 0)
                        turnover = float(row.get("turnover") or 0)
                    except (TypeError, ValueError):
                        volume = turnover = 0
                    if close > 0 and volume > 0 and turnover > 0:
                        unit_samples.append(turnover / volume / close)
        row_counts[path.stem] = file_rows
    return {
        "rows": rows,
        "row_counts": row_counts,
        "duplicate_keys": duplicate_keys,
        "invalid_ohlc_rows": invalid_ohlc_rows,
        "wrong_date_rows": wrong_date_rows,
        "files_missing_state_fields": missing_state_fields,
        "unit_inference_median": median(unit_samples) if unit_samples else None,
    }


def _status_counts(project: Path, start: str, end: str) -> dict[str, int]:
    database = (
        project / "data" / "processed" / "security_history" / "daily_status.sqlite3"
    )
    if not database.exists():
        return {}
    connection = sqlite3.connect(f"file:{database}?mode=ro", uri=True)
    try:
        return {
            str(trade_date): int(count)
            for trade_date, count in connection.execute(
                "SELECT trade_date, COUNT(*) FROM daily_status "
                "WHERE trade_date BETWEEN ? AND ? GROUP BY trade_date",
                (start, end),
            )
        }
    finally:
        connection.close()


def _coverage_profile(
    row_counts: dict[str, int],
    status_counts: dict[str, int],
    expected_dates: list[str],
) -> dict[str, Any]:
    rates = {
        day: row_counts.get(day, 0) / status_counts[day]
        for day in expected_dates
        if status_counts.get(day, 0) > 0
    }
    values = list(rates.values())
    return {
        "dates_with_denominator": len(values),
        "median": median(values) if values else None,
        "minimum": min(values) if values else None,
        "maximum": max(values) if values else None,
        "below_95pct_dates": [day for day, rate in rates.items() if rate < 0.95],
    }


def audit_history_coverage(
    project: Path,
    *,
    start_date: str,
    end_date: str,
    output_dir: Path,
) -> dict[str, Any]:
    start, end = _date_key(start_date), _date_key(end_date)
    calendar_path = project / "data" / "processed" / "trade_calendar.csv"
    calendar = _calendar_dates(calendar_path, start, end)
    raw_root = project / "data" / "processed" / "yinhe_daily"
    qfq_root = project / "data" / "processed" / "yinhe_daily_qfq"
    raw_paths = {
        path.stem: path
        for path in raw_root.glob("20??????.csv")
        if start <= path.stem <= end
    }
    qfq_paths = {
        path.stem: path
        for path in qfq_root.glob("20??????.csv")
        if start <= path.stem <= end
    }
    status_counts = _status_counts(project, start, end)
    years: dict[str, dict[str, Any]] = {}
    for year in sorted({day[:4] for day in calendar}):
        expected = [day for day in calendar if day.startswith(year)]
        raw_year = [raw_paths[day] for day in expected if day in raw_paths]
        qfq_year = [qfq_paths[day] for day in expected if day in qfq_paths]
        raw_profile = _profile_files(raw_year)
        qfq_profile = _profile_files(qfq_year)
        years[year] = {
            "expected_trade_dates": len(expected),
            "raw_files": len(raw_year),
            "qfq_files": len(qfq_year),
            "missing_raw_dates": [day for day in expected if day not in raw_paths],
            "missing_qfq_dates": [day for day in expected if day not in qfq_paths],
            "empty_raw_files": sum(path.stat().st_size <= 20 for path in raw_year),
            "empty_qfq_files": sum(path.stat().st_size <= 20 for path in qfq_year),
            "raw_profile": raw_profile,
            "qfq_profile": qfq_profile,
            "raw_cross_section_coverage": _coverage_profile(
                raw_profile["row_counts"], status_counts, expected
            ),
            "qfq_cross_section_coverage": _coverage_profile(
                qfq_profile["row_counts"], status_counts, expected
            ),
        }
    adjustment_path = project / "data" / "processed" / "yinhe_adj_factor" / "manifest.json"
    security_path = project / "data" / "processed" / "security_history" / "manifest.json"
    adjustment = json.loads(adjustment_path.read_text(encoding="utf-8")) if adjustment_path.exists() else {}
    security = json.loads(security_path.read_text(encoding="utf-8")) if security_path.exists() else {}
    failures: list[str] = []
    for year, value in years.items():
        if value["missing_raw_dates"] or value["missing_qfq_dates"]:
            failures.append(f"{year}:missing_trade_date_files")
        if value["empty_raw_files"] or value["empty_qfq_files"]:
            failures.append(f"{year}:empty_files")
        for layer in ("raw_profile", "qfq_profile"):
            profile = value[layer]
            if profile["duplicate_keys"]:
                failures.append(f"{year}:{layer}:duplicate_keys")
            if profile["invalid_ohlc_rows"]:
                failures.append(f"{year}:{layer}:invalid_ohlc")
            if profile["wrong_date_rows"]:
                failures.append(f"{year}:{layer}:wrong_date_rows")
            if profile["files_missing_state_fields"]:
                failures.append(f"{year}:{layer}:missing_state_fields")
            unit_median = profile["unit_inference_median"]
            if unit_median is None or not 0.2 <= unit_median <= 5.0:
                failures.append(f"{year}:{layer}:unverified_units")
    if adjustment.get("status") not in {"validated", "validated_with_quarantine"}:
        failures.append("adjustment_manifest_not_validated")
    if int(adjustment.get("missing_factor_rows") or 0) != 0:
        failures.append("missing_adjustment_factors")
    if security.get("point_in_time") is not True:
        failures.append("security_history_not_point_in_time")
    report = {
        "schema_version": 1,
        "status": "validated" if not failures else "failed_validation",
        "failed_checks": failures,
        "generated_at": datetime.now(UTC).isoformat(),
        "coverage_start": start,
        "coverage_end": end,
        "source": "China Galaxy AmazingData",
        "price_mode": "raw_preserved_and_forward_adjusted_from_backward_factor",
        "calendar_path": str(calendar_path),
        "years": years,
        "adjustment": {
            key: adjustment.get(key)
            for key in (
                "status",
                "coverage_start",
                "coverage_end",
                "daily_files",
                "adjusted_rows",
                "missing_factor_rows",
                "continuity_breaks",
                "quarantined_symbols",
            )
        },
        "security_history": {
            key: security.get(key)
            for key in (
                "status",
                "point_in_time",
                "coverage_start",
                "coverage_end",
                "security_count",
                "status_rows",
                "missing_trade_dates",
            )
        },
    }
    output_dir.mkdir(parents=True, exist_ok=True)
    json_path = output_dir / "latest.json"
    json_path.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    markdown_path = output_dir / "latest.md"
    lines = [
        "# 银河历史扩展覆盖验收",
        "",
        f"- 状态：`{report['status']}`",
        f"- 覆盖：{start} 至 {end}",
        f"- 生成时间：{report['generated_at']}",
        "",
        "| 年份 | 交易日 | raw文件 | qfq文件 | raw行数 | qfq行数 |",
        "|---:|---:|---:|---:|---:|---:|",
    ]
    for year, value in years.items():
        lines.append(
            f"| {year} | {value['expected_trade_dates']} | {value['raw_files']} | "
            f"{value['qfq_files']} | {value['raw_profile']['rows']} | "
            f"{value['qfq_profile']['rows']} |"
        )
    markdown_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return {
        **report,
        "report_json": str(json_path),
        "report_markdown": str(markdown_path),
        "report_sha256": _file_hash(json_path),
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="构建银河历史股票池并审计年度覆盖")
    parser.add_argument("command", choices=("build-pool", "audit"))
    parser.add_argument("--root", type=Path, default=Path("."))
    parser.add_argument("--start", required=True)
    parser.add_argument("--end", required=True)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    project = args.root.resolve()
    if args.command == "build-pool":
        output = args.output or project / "data" / "processed" / "yinhe_symbols_2021_2027.txt"
        result = build_historical_symbol_pool(
            project,
            start_date=args.start,
            end_date=args.end,
            output_path=output.resolve(),
        )
    else:
        output = args.output or project / "reports" / "yinhe_history_extension"
        result = audit_history_coverage(
            project,
            start_date=args.start,
            end_date=args.end,
            output_dir=output.resolve(),
        )
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()

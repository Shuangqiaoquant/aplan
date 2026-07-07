from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

REQUIRED_DAILY_FIELDS = {
    "ts_code",
    "trade_date",
    "open",
    "high",
    "low",
    "close",
    "pre_close",
    "pct_chg",
    "vol",
    "amount",
}


@dataclass(slots=True)
class QualityReport:
    trade_date: str
    passed: bool
    row_count: int
    unique_symbols: int
    sha256: str
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    metrics: dict[str, float] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def validate_daily_snapshot(
    path: Path,
    trade_date: str,
    *,
    previous_row_count: int | None = None,
    minimum_rows: int = 3_000,
) -> QualityReport:
    errors: list[str] = []
    warnings: list[str] = []
    document = json.loads(path.read_text(encoding="utf-8"))
    rows = document.get("rows", [])
    if not isinstance(rows, list):
        raise ValueError("daily 快照 rows 必须是数组")

    symbols: set[str] = set()
    duplicates = 0
    invalid_ohlc = 0
    wrong_dates = 0
    missing_fields = 0
    zero_turnover = 0
    for row in rows:
        if not REQUIRED_DAILY_FIELDS.issubset(row):
            missing_fields += 1
            continue
        symbol = str(row["ts_code"])
        if symbol in symbols:
            duplicates += 1
        symbols.add(symbol)
        if str(row["trade_date"]) != trade_date:
            wrong_dates += 1
        try:
            open_price = float(row["open"])
            high = float(row["high"])
            low = float(row["low"])
            close = float(row["close"])
            if (
                min(open_price, high, low, close) <= 0
                or high < max(open_price, close)
                or low > min(open_price, close)
            ):
                invalid_ohlc += 1
            if float(row["amount"] or 0) <= 0:
                zero_turnover += 1
        except (TypeError, ValueError):
            invalid_ohlc += 1

    row_count = len(rows)
    if row_count < minimum_rows:
        errors.append(f"行数 {row_count} 低于最低要求 {minimum_rows}")
    if missing_fields:
        errors.append(f"{missing_fields} 行缺少必要字段")
    if duplicates:
        errors.append(f"发现 {duplicates} 个重复证券代码")
    if wrong_dates:
        errors.append(f"发现 {wrong_dates} 行交易日期不一致")
    if invalid_ohlc:
        errors.append(f"发现 {invalid_ohlc} 行无效 OHLC")
    if row_count and zero_turnover / row_count > 0.05:
        warnings.append(f"零成交额比例偏高：{zero_turnover / row_count:.2%}")

    coverage_change = 0.0
    if previous_row_count:
        coverage_change = row_count / previous_row_count - 1
        if abs(coverage_change) > 0.10:
            warnings.append(f"相对上一交易日行数变化 {coverage_change:.2%}")

    return QualityReport(
        trade_date=trade_date,
        passed=not errors,
        row_count=row_count,
        unique_symbols=len(symbols),
        sha256=file_sha256(path),
        errors=errors,
        warnings=warnings,
        metrics={
            "duplicate_rows": float(duplicates),
            "invalid_ohlc_rows": float(invalid_ohlc),
            "wrong_date_rows": float(wrong_dates),
            "missing_field_rows": float(missing_fields),
            "zero_turnover_ratio": zero_turnover / row_count if row_count else 0.0,
            "coverage_change": coverage_change,
        },
    )


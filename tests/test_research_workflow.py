from __future__ import annotations

import csv
import unittest
from datetime import date, timedelta
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any

from aplan.research_workflow import run_research_report


def _write_csv(path: Path, rows: list[dict[str, object]], fields: tuple[str, ...]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        writer.writerows(rows)


def _bars(
    symbol: str,
    *,
    slope: float,
    turnover_slope: float,
    start: date = date(2025, 1, 1),
) -> list[dict[str, object]]:
    rows: list[dict[str, object]] = []
    for index in range(25):
        close = 10 + index * slope
        rows.append(
            {
                "symbol": symbol,
                "trade_date": (start + timedelta(days=index)).isoformat(),
                "open": close,
                "high": close * 1.01,
                "low": close * 0.99,
                "close": close,
                "volume": 1_000_000,
                "turnover": 100_000_000 + index * turnover_slope,
                "is_suspended": "0",
                "is_limit_up": "0",
                "is_limit_down": "0",
            }
        )
    return rows


class ResearchWorkflowTests(unittest.TestCase):
    def test_auto_akshare_fundamentals_only_syncs_candidate_pool(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            bars_path = root / "bars.csv"
            securities_path = root / "securities.csv"
            _write_csv(
                bars_path,
                _bars("300001", slope=0.25, turnover_slope=2_000_000)
                + _bars("300002", slope=-0.05, turnover_slope=-500_000),
                (
                    "symbol",
                    "trade_date",
                    "open",
                    "high",
                    "low",
                    "close",
                    "volume",
                    "turnover",
                    "is_suspended",
                    "is_limit_up",
                    "is_limit_down",
                ),
            )
            _write_csv(
                securities_path,
                [
                    {
                        "symbol": "300001",
                        "name": "强势股份",
                        "list_date": "2020-01-01",
                        "industry": "测试",
                        "is_st": "0",
                        "is_delisting_risk": "0",
                    },
                    {
                        "symbol": "300002",
                        "name": "弱势股份",
                        "list_date": "2020-01-01",
                        "industry": "测试",
                        "is_st": "0",
                        "is_delisting_risk": "0",
                    },
                ],
                ("symbol", "name", "list_date", "industry", "is_st", "is_delisting_risk"),
            )
            synced_symbols: list[str] = []

            def fake_syncer(
                project: Path,
                symbols: list[str],
                **_: Any,
            ) -> dict[str, Any]:
                synced_symbols.extend(symbols)
                output = project / "data" / "processed" / "akshare_fundamentals" / "20250125.csv"
                _write_csv(
                    output,
                    [
                        {
                            "symbol": symbols[0],
                            "period_end": "2024-12-31",
                            "publish_time": "2025-01-25T00:00:00+00:00",
                            "source": "fake",
                            "source_hash": "a" * 64,
                            "revenue_growth": "0.05",
                            "net_profit_growth": "-0.40",
                            "roe": "0.08",
                            "operating_cashflow_to_profit": "-0.20",
                            "debt_to_assets": "0.80",
                        }
                    ],
                    (
                        "symbol",
                        "period_end",
                        "publish_time",
                        "source",
                        "source_hash",
                        "revenue_growth",
                        "net_profit_growth",
                        "roe",
                        "operating_cashflow_to_profit",
                        "debt_to_assets",
                    ),
                )
                return {
                    "symbols": len(symbols),
                    "fundamental_rows": 1,
                    "failures": {},
                    "processed_path": str(output),
                }

            result = run_research_report(
                root,
                bars_path=bars_path,
                securities_path=securities_path,
                as_of=date(2025, 1, 25),
                top_n=1,
                auto_akshare_fundamentals=True,
                akshare_candidate_pool=1,
                fundamental_syncer=fake_syncer,
            )

            self.assertEqual(synced_symbols, ["300001"])
            self.assertEqual(result.candidates[0].symbol, "300001")
            self.assertIn("基本面快照 2024-12-31", result.report)
            self.assertIn("基本面风险：净利润同比大幅下滑", result.report)
            self.assertIn("AkShare 基本面自动补证：已启用", result.report)

    def test_research_workflow_accepts_daily_bar_directory(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            bars_dir = root / "daily"
            securities_path = root / "securities.csv"
            rows = _bars("300001", slope=0.20, turnover_slope=2_000_000)
            for index, row in enumerate(rows):
                _write_csv(
                    bars_dir / f"202501{index + 1:02d}.csv",
                    [row],
                    (
                        "symbol",
                        "trade_date",
                        "open",
                        "high",
                        "low",
                        "close",
                        "volume",
                        "turnover",
                        "is_suspended",
                        "is_limit_up",
                        "is_limit_down",
                    ),
                )
            _write_csv(
                securities_path,
                [
                    {
                        "symbol": "300001",
                        "name": "强势股份",
                        "list_date": "2020-01-01",
                        "industry": "测试",
                        "is_st": "0",
                        "is_delisting_risk": "0",
                    }
                ],
                ("symbol", "name", "list_date", "industry", "is_st", "is_delisting_risk"),
            )

            result = run_research_report(
                root,
                bars_path=bars_dir,
                securities_path=securities_path,
                as_of=date(2025, 1, 25),
                top_n=1,
            )

            self.assertEqual(result.candidates[0].symbol, "300001")


if __name__ == "__main__":
    unittest.main()

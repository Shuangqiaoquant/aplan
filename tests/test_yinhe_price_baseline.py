from __future__ import annotations

import csv
import json
import shutil
from datetime import date, timedelta
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from aplan.validation_protocol import freeze_protocol
from aplan.yinhe_price_baseline import (
    _metrics,
    evaluate_signals,
    generate_signals,
    run_yinhe_price_baseline,
)


FIELDS = (
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
)


def _business_days(start: date, count: int) -> list[date]:
    days: list[date] = []
    current = start
    while len(days) < count:
        if current.weekday() < 5:
            days.append(current)
        current += timedelta(days=1)
    return days


def _write_market(
    project: Path,
    count: int = 150,
    *,
    start: date = date(2023, 1, 2),
) -> list[Path]:
    daily = project / "data" / "processed" / "yinhe_daily"
    daily.mkdir(parents=True)
    for day_index, day in enumerate(_business_days(start, count)):
        path = daily / f"{day:%Y%m%d}.csv"
        with path.open("w", encoding="utf-8", newline="") as handle:
            writer = csv.DictWriter(handle, fieldnames=FIELDS)
            writer.writeheader()
            for symbol_index in range(12):
                symbol = f"600{symbol_index:03d}"
                drift = 0.002 + symbol_index * 0.0002
                close = 10 * (1 + drift) ** day_index
                writer.writerow(
                    {
                        "symbol": symbol,
                        "trade_date": f"{day:%Y%m%d}",
                        "open": close * 0.999,
                        "high": close * 1.01,
                        "low": close * 0.99,
                        "close": close,
                        "volume": 10_000_000,
                        "turnover": close * 10_000_000,
                        "is_suspended": 0,
                        "is_limit_up": 0,
                        "is_limit_down": 0,
                    }
                )
    return sorted(daily.glob("*.csv"))


class YinhePriceBaselineTests(unittest.TestCase):
    def test_streaming_signal_and_evaluation_produces_costed_results(self) -> None:
        with TemporaryDirectory() as tmp:
            paths = _write_market(Path(tmp))
            signals, benchmark = generate_signals(
                paths,
                horizons=(5,),
                top_n=3,
                min_history=20,
                momentum_days=10,
                min_avg_turnover=1,
            )
            results = evaluate_signals(
                paths,
                signals,
                benchmark,
                commission_rate=0.0003,
                stamp_tax_rate=0.0005,
                slippage_rate=0.001,
            )

            self.assertTrue(signals)
            self.assertTrue(results)
            self.assertTrue(all(result.net_return < result.gross_return for result in results))
            metrics = _metrics(results)
            self.assertEqual(metrics["status"], "completed")
            self.assertIn("distribution", metrics)
            self.assertGreater(metrics["mean_transaction_cost"], 0)

    def test_runner_requires_accepted_data_and_keeps_holdout_closed(self) -> None:
        with TemporaryDirectory() as tmp:
            project = Path(tmp)
            config = project / "config"
            config.mkdir()
            source = Path(__file__).resolve().parents[1] / "config" / "validation_protocol.toml"
            (config / "validation_protocol.toml").write_bytes(source.read_bytes())
            freeze_protocol(project, change_reason="baseline test")
            paths = _write_market(project)
            acceptance = project / "reports" / "yinhe_acceptance"
            acceptance.mkdir(parents=True)
            (acceptance / "latest.json").write_text(
                json.dumps(
                    {
                        "data_version": "test-data-v1",
                        "readiness": {"raw_price_research_ready": True},
                    }
                ),
                encoding="utf-8",
            )

            result = run_yinhe_price_baseline(
                project,
                start_date=paths[0].stem,
                end_date=paths[-1].stem,
            )

            self.assertEqual(result["status"], "provisional_raw_price_only")
            self.assertFalse(result["final_holdout_opened"])
            report = json.loads(Path(result["report_json"]).read_text(encoding="utf-8"))
            self.assertFalse(report["execution_allowed"])
            self.assertIn("forward_adjustment_continuity", report["blocked_checks"])

    def test_runner_rejects_unaccepted_data(self) -> None:
        with TemporaryDirectory() as tmp:
            project = Path(tmp)
            config = project / "config"
            config.mkdir()
            source = Path(__file__).resolve().parents[1] / "config" / "validation_protocol.toml"
            (config / "validation_protocol.toml").write_bytes(source.read_bytes())
            freeze_protocol(project, change_reason="baseline test")
            paths = _write_market(project)

            with self.assertRaisesRegex(ValueError, "raw_price_research_ready"):
                run_yinhe_price_baseline(
                    project,
                    start_date=paths[0].stem,
                    end_date=paths[-1].stem,
                )

    def test_runner_does_not_calculate_final_holdout_by_default(self) -> None:
        with TemporaryDirectory() as tmp:
            project = Path(tmp)
            config = project / "config"
            config.mkdir()
            source = Path(__file__).resolve().parents[1] / "config" / "validation_protocol.toml"
            (config / "validation_protocol.toml").write_bytes(source.read_bytes())
            freeze_protocol(project, change_reason="holdout test")
            paths = _write_market(
                project,
                count=180,
                start=date(2025, 7, 1),
            )
            acceptance = project / "reports" / "yinhe_acceptance"
            acceptance.mkdir(parents=True)
            (acceptance / "latest.json").write_text(
                json.dumps(
                    {
                        "data_version": "test-data-v2",
                        "readiness": {"raw_price_research_ready": True},
                    }
                ),
                encoding="utf-8",
            )

            result = run_yinhe_price_baseline(
                project,
                start_date=paths[0].stem,
                end_date=paths[-1].stem,
            )
            report = json.loads(Path(result["report_json"]).read_text(encoding="utf-8"))
            trades = json.loads(Path(result["results_json"]).read_text(encoding="utf-8"))

            self.assertLess(report["data_profile"]["last_date"], "20260101")
            self.assertGreaterEqual(report["data_profile"]["available_last_date"], "20260101")
            self.assertTrue(all(item["signal_date"] < "20260101" for item in trades))

    def test_runner_prefers_validated_forward_adjusted_files(self) -> None:
        with TemporaryDirectory() as tmp:
            project = Path(tmp)
            config = project / "config"
            config.mkdir()
            source = Path(__file__).resolve().parents[1] / "config" / "validation_protocol.toml"
            (config / "validation_protocol.toml").write_bytes(source.read_bytes())
            freeze_protocol(project, change_reason="adjusted baseline test")
            raw_paths = _write_market(project)
            raw_dir = project / "data" / "processed" / "yinhe_daily"
            adjusted_dir = project / "data" / "processed" / "yinhe_daily_qfq"
            shutil.copytree(raw_dir, adjusted_dir)
            factor_dir = project / "data" / "processed" / "yinhe_adj_factor"
            factor_dir.mkdir()
            (factor_dir / "manifest.json").write_text(
                json.dumps(
                    {
                        "status": "validated",
                        "continuity_breaks": 0,
                        "missing_factor_rows": 0,
                        "raw_prices_preserved": True,
                        "coverage_start": raw_paths[0].stem,
                        "coverage_end": raw_paths[-1].stem,
                    }
                ),
                encoding="utf-8",
            )
            acceptance = project / "reports" / "yinhe_acceptance"
            acceptance.mkdir(parents=True)
            (acceptance / "latest.json").write_text(
                json.dumps(
                    {
                        "data_version": "test-adjusted-v1",
                        "readiness": {"raw_price_research_ready": True},
                    }
                ),
                encoding="utf-8",
            )

            result = run_yinhe_price_baseline(
                project,
                start_date=raw_paths[0].stem,
                end_date="20991231",
            )
            report = json.loads(Path(result["report_json"]).read_text(encoding="utf-8"))

            self.assertEqual(result["status"], "provisional_adjusted_price_only")
            self.assertEqual(report["price_mode"], "forward_adjusted")
            self.assertNotIn("forward_adjustment_continuity", report["blocked_checks"])

from __future__ import annotations

import csv
import hashlib
import json
import tempfile
import unittest
from collections import deque
from pathlib import Path

from aplan.qlib_reference import (
    Bar,
    FEATURE_NAMES,
    Observation,
    QlibAlpha158LinearLiteReference,
    _next_holdings,
    _pipeline_status,
    alpha158_selected20,
    run_current_inference,
)
from aplan.strategy import SignalIntent, StrategyContext
from aplan.strategy_registry import StrategyRegistry


class QlibReferenceTests(unittest.TestCase):
    def test_pipeline_status_uses_streamed_latest_score_date(self) -> None:
        self.assertEqual(_pipeline_status("20241008"), "completed_pipeline_pilot")
        self.assertEqual(_pipeline_status(None), "data_unavailable")

    def test_selected_feature_vector_is_finite_and_named(self) -> None:
        bars = [
            Bar(
                open=10 + index * 0.02,
                high=10.2 + index * 0.02,
                low=9.8 + index * 0.02,
                close=10.05 + index * 0.02,
                volume=1_000_000 + index * 1_000,
                turnover=10_000_000,
            )
            for index in range(61)
        ]
        features = alpha158_selected20(deque(bars, maxlen=61))
        self.assertIsNotNone(features)
        self.assertEqual(len(features or ()), len(FEATURE_NAMES))

    def test_wvma_uses_five_changes_from_six_bars(self) -> None:
        bars = [
            Bar(
                open=10 + index,
                high=11 + index,
                low=9 + index,
                close=10 + index,
                volume=1_000 + index * 100,
                turnover=10_000,
            )
            for index in range(61)
        ]
        features = alpha158_selected20(bars)
        self.assertIsNotNone(features)
        selected = bars[-6:]
        weighted_moves = [
            abs(selected[index].close / selected[index - 1].close - 1)
            * selected[index].volume
            for index in range(1, 6)
        ]
        center = sum(weighted_moves) / len(weighted_moves)
        expected = (
            sum((value - center) ** 2 for value in weighted_moves)
            / len(weighted_moves)
        ) ** 0.5 / center
        self.assertAlmostEqual((features or ())[1], expected)

    def test_topk_dropout_does_not_sell_a_holding_still_above_new_candidate(self) -> None:
        def row(symbol: str) -> Observation:
            return Observation("20240102", symbol, (0.0,) * 20, 0.0, 1.0)

        ranked = [
            (row("000001"), 4.0),
            (row("000002"), 3.0),
            (row("000003"), 2.0),
            (row("000004"), 1.0),
        ]
        result = _next_holdings(
            {"000001", "000002", "000003"},
            ranked,
            topk=3,
            n_drop=1,
        )
        self.assertEqual(result, {"000001", "000002", "000003"})

    def test_current_inference_reads_no_future_labels_or_files(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            data_root = root / "yinhe_daily_qfq"
            data_root.mkdir()
            dates = [f"2024{index:04d}" for index in range(101, 163)]
            fields = (
                "symbol",
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
            for offset, day in enumerate(dates):
                with (data_root / f"{day}.csv").open(
                    "w", newline="", encoding="utf-8"
                ) as handle:
                    writer = csv.DictWriter(handle, fieldnames=fields)
                    writer.writeheader()
                    for symbol_index, symbol in enumerate(("600000", "000001")):
                        close = 10 + offset * 0.01 + symbol_index * 0.1
                        writer.writerow(
                            {
                                "symbol": symbol,
                                "open": close,
                                "high": close + 0.1,
                                "low": close - 0.1,
                                "close": close,
                                "volume": 1_000_000 + offset,
                                "turnover": 10_000_000,
                                "is_suspended": 0,
                                "is_limit_up": 0,
                                "is_limit_down": 0,
                            }
                        )
            future = data_root / "20250102.csv"
            future.write_text("future_return,outcome\n9,9\n", encoding="utf-8")
            model_path = root / "model.json"
            model_path.write_text(
                json.dumps(
                    {
                        "model": {
                            "medians": [0.0] * 20,
                            "scales": [1.0] * 20,
                            "coefficients": [0.0] + [1.0] * 20,
                            "clip_zscore": 3.0,
                            "training_rows": 100,
                        }
                    }
                ),
                encoding="utf-8",
            )
            preview = root / "preview.json"
            result = run_current_inference(
                data_root=data_root,
                model_path=model_path,
                conclusion_as_of="20240725",
                price_as_of=dates[-2],
                preview_path=preview,
                full_scores_path=root / "scores.csv",
            )
            document = json.loads(preview.read_text(encoding="utf-8"))

            self.assertEqual(result["status"], "current_shadow_inference")
            self.assertEqual(document["as_of"], "20240725")
            self.assertEqual(document["price_as_of"], dates[-2])
            self.assertEqual(document["evidence_as_of"], "20240725")
            self.assertEqual(document["coverage"]["history_last_date"], dates[-2])
            self.assertEqual(document["evidence_review"]["status"], "evidence_gap")
            self.assertEqual(document["evidence_review"]["score_impact"], "none")
            self.assertIn("items", document)
            self.assertNotIn("candidates", document)
            self.assertIsInstance(document["items"][0]["invalidation"], list)
            self.assertIn("summary", document["items"][0]["evidence"][0])
            self.assertGreaterEqual(document["items"][0]["score_percentile"], 0)
            self.assertLessEqual(document["items"][0]["score_percentile"], 100)
            self.assertTrue(document["holdout_boundary_opened"])
            serialized = json.dumps(document)
            self.assertNotIn("future_return", serialized)
            self.assertNotIn("outcome", serialized)
            self.assertFalse(document["execution_eligible"])
            self.assertEqual(document["formal_gate"], "reject")

    def test_research_adapter_emits_watch_only_non_actionable_signals(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            score_path = root / "scores" / "20241008.csv"
            score_path.parent.mkdir(parents=True)
            with score_path.open("w", newline="", encoding="utf-8") as handle:
                writer = csv.DictWriter(
                    handle,
                    fieldnames=(
                        "symbol",
                        "model_score",
                        "rank",
                    ),
                )
                writer.writeheader()
                writer.writerow(
                    {
                        "symbol": "600000",
                        "model_score": "0.2",
                        "rank": "1",
                    }
                )
            context = StrategyContext(
                "20241008",
                root,
                hashlib.sha256(score_path.read_bytes()).hexdigest(),
            )
            registry = StrategyRegistry()
            registry.register(QlibAlpha158LinearLiteReference(root))
            run = registry.run(context, allow_simulation=True)[0]
            self.assertFalse(run.execution_allowed)
            self.assertEqual(run.signals[0].intent, SignalIntent.WATCH)
            self.assertEqual(run.signals[0].target_weight, 0)
            self.assertFalse(run.signals[0].actionable)
            self.assertIn("研究", run.blocked_reason or "")


if __name__ == "__main__":
    unittest.main()

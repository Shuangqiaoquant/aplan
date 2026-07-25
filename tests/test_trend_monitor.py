from __future__ import annotations

import unittest
from datetime import date, timedelta

from aplan.models import DailyBar, Security
from aplan.trend_monitor import detect_trend_signals, select_trend_candidates


def bars_from_closes(
    closes: list[float],
    *,
    latest_volume: float = 1_000_000,
    latest_open: float | None = None,
    latest_low: float | None = None,
) -> list[DailyBar]:
    start = date(2026, 1, 1)
    bars = []
    for index, close in enumerate(closes):
        is_latest = index == len(closes) - 1
        open_price = latest_open if is_latest and latest_open is not None else close
        low = latest_low if is_latest and latest_low is not None else min(open_price, close) - 0.1
        high = max(open_price, close) + 0.1
        bars.append(
            DailyBar(
                symbol="300001",
                trade_date=start + timedelta(days=index),
                open=open_price,
                high=high,
                low=low,
                close=close,
                volume=latest_volume if is_latest else 1_000_000,
                turnover=100_000_000,
            )
        )
    return bars


class TrendMonitorTests(unittest.TestCase):
    def test_detects_volume_breakout_without_future_data(self) -> None:
        history = bars_from_closes(
            [10 + index * 0.02 for index in range(60)] + [13.5],
            latest_volume=2_000_000,
            latest_open=13.0,
        )

        snapshot = detect_trend_signals(history)

        self.assertIsNotNone(snapshot)
        self.assertIn("B1_volume_breakout", snapshot.signals)
        self.assertAlmostEqual(snapshot.volume_ratio, 2.0)

    def test_detects_ma20_pullback_and_ma_cross_as_separate_rules(self) -> None:
        pullback_history = bars_from_closes(
            [10 + index * 0.1 for index in range(60)] + [16.1],
            latest_open=15.2,
            latest_low=15.0,
        )
        cross_history = bars_from_closes([10.0] * 60 + [11.0], latest_open=10.8)

        pullback = detect_trend_signals(pullback_history)
        cross = detect_trend_signals(cross_history)

        self.assertEqual(pullback.signals, ("B2_ma20_pullback",))
        self.assertEqual(cross.signals, ("B3_ma5_cross_ma20",))

    def test_selects_research_only_candidates_and_excludes_limit_up(self) -> None:
        history = bars_from_closes(
            [10 + index * 0.02 for index in range(60)] + [13.5],
            latest_volume=2_000_000,
            latest_open=13.0,
        )
        security = Security("300001", "测试股份", date(2020, 1, 1), "软件")

        candidates = select_trend_candidates(
            [security],
            history,
            history[-1].trade_date,
            top_n=5,
        )
        blocked_history = history[:-1] + [
            DailyBar(
                symbol=history[-1].symbol,
                trade_date=history[-1].trade_date,
                open=history[-1].open,
                high=history[-1].high,
                low=history[-1].low,
                close=history[-1].close,
                volume=history[-1].volume,
                turnover=history[-1].turnover,
                is_limit_up=True,
            )
        ]

        self.assertEqual(len(candidates), 1)
        self.assertEqual(candidates[0].decision_band, "research_candidate")
        self.assertEqual(candidates[0].entry_style, "trend_breakout_watch")
        self.assertIn("research_only", candidates[0].risks[-1])
        self.assertEqual(
            select_trend_candidates([security], blocked_history, history[-1].trade_date),
            [],
        )

    def test_requires_enough_history(self) -> None:
        self.assertIsNone(detect_trend_signals(bars_from_closes([10.0] * 60)))


if __name__ == "__main__":
    unittest.main()

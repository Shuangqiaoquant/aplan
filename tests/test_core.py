from __future__ import annotations

import unittest
from datetime import UTC, date, datetime, timedelta

from aplan.backtest import run_fixed_horizon_backtest, summarize
from aplan.announcement_fulltext import FulltextAnalysis
from aplan.announcements import AnnouncementEvent, EventImpact, RiskLevel
from aplan.factors import momentum, percentile_ranks
from aplan.market_regime import assess_market_regime
from aplan.models import Candidate, DailyBar, FundamentalSnapshot, Security, ValuationSnapshot
from aplan.pipeline import select_candidates
from aplan.pretrade import evaluate_pretrade
from aplan.reports import render_daily_report
from aplan.sync import build_daily_valuations
from aplan.universe import eligible_securities


def make_bars(symbol: str, count: int, start: date = date(2025, 1, 1)) -> list[DailyBar]:
    return [
        DailyBar(
            symbol=symbol,
            trade_date=start + timedelta(days=i),
            open=10 + i * 0.1,
            high=10.2 + i * 0.1,
            low=9.8 + i * 0.1,
            close=10.1 + i * 0.1,
            volume=1_000_000,
            turnover=100_000_000,
        )
        for i in range(count)
    ]


def custom_bars(symbol: str, closes: list[float], turnovers: list[float]) -> list[DailyBar]:
    start = date(2025, 1, 1)
    return [
        DailyBar(
            symbol=symbol,
            trade_date=start + timedelta(days=i),
            open=close,
            high=close * 1.01,
            low=close * 0.99,
            close=close,
            volume=1_000_000,
            turnover=turnovers[i],
        )
        for i, close in enumerate(closes)
    ]


class FactorTests(unittest.TestCase):
    def test_momentum_uses_only_history(self) -> None:
        bars = make_bars("300001", 22)
        expected = bars[-1].close / bars[-21].close - 1
        self.assertAlmostEqual(momentum(bars, 20), expected)

    def test_percentile_direction(self) -> None:
        self.assertEqual(percentile_ranks({"a": 1, "b": 2})["b"], 1)
        self.assertEqual(percentile_ranks({"a": 1, "b": 2}, higher_is_better=False)["a"], 1)

    def test_daily_basic_converts_to_valuation_snapshot_rows(self) -> None:
        rows = build_daily_valuations(
            [
                {
                    "ts_code": "300001.SZ",
                    "trade_date": "20260702",
                    "pe": 12.3,
                    "pb": 1.2,
                    "total_mv": 1000,
                    "circ_mv": 900,
                    "turnover_rate": 2.5,
                    "volume_ratio": 1.1,
                }
            ]
        )
        self.assertEqual(rows[0]["symbol"], "300001")
        self.assertEqual(rows[0]["pe"], 12.3)

    def test_market_regime_detects_weak_internal_breadth(self) -> None:
        histories = {
            f"30000{i}": custom_bars(
                f"30000{i}",
                [20 - j * 0.2 for j in range(25)],
                [100_000_000 for _ in range(25)],
            )
            for i in range(5)
        }

        regime = assess_market_regime(histories, min_sample=5)

        self.assertEqual(regime.label, "stress")
        self.assertEqual(regime.score_cap, 64.0)
        self.assertLess(regime.breadth_above_ma20, 0.25)

    def test_valuation_enters_candidate_report_and_negative_pe_is_not_cheap(self) -> None:
        bars = make_bars("300001", 25)
        securities = [Security("300001", "测试股份", date(2020, 1, 1))]
        valuations = [
            ValuationSnapshot(
                "300001",
                bars[-1].trade_date,
                pe=-10,
                pb=1.0,
                total_mv=1000,
                circ_mv=900,
                turnover_rate=2.5,
                volume_ratio=1.1,
            )
        ]
        candidates = select_candidates(
            securities,
            bars,
            bars[-1].trade_date,
            valuations=valuations,
            top_n=1,
        )
        report = render_daily_report(bars[-1].trade_date, candidates)
        self.assertIn("估值快照 PE -10.0 / PB 1.00", report)
        self.assertIn("估值字段为非正值，不能按低估处理", report)
        self.assertNotIn("估值快照尚未接入本评分", report)

    def test_high_risk_announcement_caps_candidate_to_watch_only(self) -> None:
        strong = custom_bars(
            "300001",
            [10 + i * 0.2 for i in range(25)],
            [80_000_000 + i * 3_000_000 for i in range(25)],
        )
        weak = custom_bars(
            "300002",
            [20 - i * 0.1 for i in range(25)],
            [80_000_000 - i * 1_000_000 for i in range(25)],
        )
        securities = [
            Security("300001", "强势股份", date(2020, 1, 1)),
            Security("300002", "弱势股份", date(2020, 1, 1)),
        ]
        event = AnnouncementEvent(
            announcement_id="risk-1",
            symbol="300001",
            event_type="market_risk_warning",
            impact_hint=EventImpact.NEGATIVE,
            risk_level=RiskLevel.HIGH,
            confidence=0.85,
            summary="标题命中事件规则：market_risk_warning",
            evidence=("标题包含“异常波动”",),
            source_url="https://example.test/risk.pdf",
            published_at="2025-01-25T00:00:00+00:00",
            requires_fulltext=True,
        )
        candidates = select_candidates(
            securities,
            strong + weak,
            strong[-1].trade_date,
            announcement_events=[event],
            top_n=2,
        )
        candidate = next(item for item in candidates if item.symbol == "300001")
        report = render_daily_report(strong[-1].trade_date, [candidate])
        self.assertLessEqual(candidate.score, 64)
        self.assertEqual(candidate.decision_band, "watch_only")
        self.assertIn("高风险公告：market_risk_warning", report)
        self.assertIn("公告标题风险规则已接入；全文催化加分尚未启用", report)

    def test_weak_market_regime_caps_candidate_to_research_level(self) -> None:
        strong = custom_bars(
            "300001",
            [10 + i * 0.2 for i in range(25)],
            [80_000_000 + i * 3_000_000 for i in range(25)],
        )
        weak = custom_bars(
            "300002",
            [30 - i * 0.3 for i in range(25)],
            [80_000_000 - i * 1_000_000 for i in range(25)],
        )
        weaker = custom_bars(
            "300003",
            [40 - i * 0.4 for i in range(25)],
            [80_000_000 - i * 1_000_000 for i in range(25)],
        )
        securities = [
            Security("300001", "强势股份", date(2020, 1, 1)),
            Security("300002", "弱势股份", date(2020, 1, 1)),
            Security("300003", "更弱股份", date(2020, 1, 1)),
        ]
        valuations = [
            ValuationSnapshot("300001", strong[-1].trade_date, 12, 1.2, 1000, 900, 2.5, 1.1)
        ]

        candidate = select_candidates(
            securities,
            strong + weak + weaker,
            strong[-1].trade_date,
            valuations=valuations,
            top_n=1,
            market_regime_min_sample=3,
        )[0]
        report = render_daily_report(strong[-1].trade_date, [candidate])

        self.assertLessEqual(candidate.score, 74)
        self.assertIn("市场环境风险", report)
        self.assertIn("市场环境过滤已接入", report)

    def test_weak_industry_caps_candidate_without_positive_industry_scoring(self) -> None:
        target = custom_bars(
            "300001",
            [10 + i * 0.2 for i in range(25)],
            [80_000_000 + i * 3_000_000 for i in range(25)],
        )
        weak_peer = custom_bars(
            "300002",
            [30 - i * 0.3 for i in range(25)],
            [80_000_000 for _ in range(25)],
        )
        weak_peer_2 = custom_bars(
            "300005",
            [40 - i * 0.5 for i in range(25)],
            [80_000_000 for _ in range(25)],
        )
        mid_peer = custom_bars(
            "300003",
            [20 + i * 0.01 for i in range(25)],
            [80_000_000 for _ in range(25)],
        )
        strong_peer = custom_bars(
            "300004",
            [10 + i * 0.4 for i in range(25)],
            [80_000_000 for _ in range(25)],
        )
        securities = [
            Security("300001", "弱行业强股", date(2020, 1, 1), industry="弱行业"),
            Security("300002", "弱行业弱股", date(2020, 1, 1), industry="弱行业"),
            Security("300005", "弱行业更弱", date(2020, 1, 1), industry="弱行业"),
            Security("300003", "中行业", date(2020, 1, 1), industry="中行业"),
            Security("300004", "强行业", date(2020, 1, 1), industry="强行业"),
        ]
        valuations = [
            ValuationSnapshot("300001", target[-1].trade_date, 12, 1.2, 1000, 900, 2.5, 1.1)
        ]

        candidate = next(
            item
            for item in select_candidates(
                securities,
                target + weak_peer + weak_peer_2 + mid_peer + strong_peer,
                target[-1].trade_date,
                valuations=valuations,
                top_n=4,
                market_regime_min_sample=50,
                industry_min_count=3,
            )
            if item.symbol == "300001"
        )
        report = render_daily_report(target[-1].trade_date, [candidate])

        self.assertLessEqual(candidate.score, 74)
        self.assertIn("行业相对弱势", report)
        self.assertIn("行业相对强弱已接入为风险闸门", report)

    def test_pretrade_check_blocks_unclassified_or_risky_candidate(self) -> None:
        candidate = Candidate(
            symbol="300001",
            score=76,
            horizon="swing",
            reasons=(),
            risks=("市场环境风险：弱市", "基本面风险：净利润同比大幅下滑"),
            decision_band="paper_candidate_if_validated",
            entry_style="unclassified_watch",
            evidence_gaps=("公告/催化证据尚未接入本评分", "未找到信号日前已发布的基本面快照"),
        )

        check = evaluate_pretrade(candidate)
        report = render_daily_report(date(2026, 1, 1), [candidate])

        self.assertEqual(check.decision, "watch_only")
        self.assertTrue(check.blockers)
        self.assertIn("模拟买入前检查", report)
        self.assertIn("进入观察而非模拟买入", report)

    def test_fulltext_analysis_strengthens_announcement_risk_review(self) -> None:
        strong = custom_bars(
            "300001",
            [10 + i * 0.2 for i in range(25)],
            [80_000_000 + i * 3_000_000 for i in range(25)],
        )
        securities = [Security("300001", "强势股份", date(2020, 1, 1))]
        event = AnnouncementEvent(
            announcement_id="risk-1",
            symbol="300001",
            event_type="market_risk_warning",
            impact_hint=EventImpact.NEGATIVE,
            risk_level=RiskLevel.HIGH,
            confidence=0.85,
            summary="标题命中事件规则：market_risk_warning",
            evidence=("标题包含“异常波动”",),
            source_url="https://example.test/risk.pdf",
            published_at="2025-01-25T00:00:00+00:00",
            requires_fulltext=True,
        )
        analysis = FulltextAnalysis(
            announcement_id="risk-1",
            symbol="300001",
            event_type="market_risk_warning",
            conclusion="risk_review_required",
            confidence=0.70,
            facts=("收到监管关注函",),
            positive_evidence=("公司称生产经营正常",),
            negative_evidence=("存在交易异常波动风险",),
            uncertainties=("规则分析不能判断信息是否已被市场价格充分反映",),
            source_url="https://example.test/risk.pdf",
            pdf_sha256="a" * 64,
        )
        candidates = select_candidates(
            securities,
            strong,
            strong[-1].trade_date,
            announcement_events=[event],
            fulltext_analyses=[analysis],
            top_n=1,
        )
        report = render_daily_report(strong[-1].trade_date, candidates)
        self.assertIn("公告全文风险确认：market_risk_warning", report)
        self.assertIn("公告全文正面证据待验证", report)
        self.assertIn("公告全文分析已接入；正向催化加分尚未启用", report)
        self.assertLessEqual(candidates[0].score, 64)

    def test_clean_fundamentals_are_filtered_by_publish_time_and_do_not_add_score_yet(self) -> None:
        bars = custom_bars(
            "300001",
            [10 + i * 0.2 for i in range(25)],
            [80_000_000 + i * 3_000_000 for i in range(25)],
        )
        securities = [Security("300001", "强势股份", date(2020, 1, 1))]
        future_snapshot = FundamentalSnapshot(
            symbol="300001",
            period_end=date(2025, 3, 31),
            publish_time=datetime(2025, 2, 1, tzinfo=UTC),
            source="test",
            source_hash="b" * 64,
            revenue_growth=0.10,
            net_profit_growth=0.20,
            roe=0.12,
        )
        visible_snapshot = FundamentalSnapshot(
            symbol="300001",
            period_end=date(2024, 12, 31),
            publish_time=datetime(2025, 1, 10, tzinfo=UTC),
            source="test",
            source_hash="a" * 64,
            revenue_growth=0.05,
            net_profit_growth=0.20,
            roe=0.08,
            operating_cashflow_to_profit=0.8,
            debt_to_assets=0.50,
        )
        baseline = select_candidates(securities, bars, bars[-1].trade_date, top_n=1)[0]
        candidate = select_candidates(
            securities,
            bars,
            bars[-1].trade_date,
            fundamentals=[future_snapshot, visible_snapshot],
            top_n=1,
        )[0]
        report = render_daily_report(bars[-1].trade_date, [candidate])
        self.assertEqual(candidate.score, baseline.score)
        self.assertIn("基本面快照 2024-12-31", report)
        self.assertNotIn("2025-03-31", report)
        self.assertNotIn("基本面风险：", report)
        self.assertIn("基本面风险降级已接入；质量加分尚未启用", report)

    def test_fundamental_risks_cap_candidate_score_without_positive_scoring(self) -> None:
        strong = custom_bars(
            "300001",
            [10 + i * 0.2 for i in range(25)],
            [80_000_000 + i * 3_000_000 for i in range(25)],
        )
        weak = custom_bars(
            "300002",
            [20 - i * 0.1 for i in range(25)],
            [80_000_000 - i * 1_000_000 for i in range(25)],
        )
        bars = strong + weak
        securities = [
            Security("300001", "强势股份", date(2020, 1, 1)),
            Security("300002", "弱势股份", date(2020, 1, 1)),
        ]
        risky_snapshot = FundamentalSnapshot(
            symbol="300001",
            period_end=date(2024, 12, 31),
            publish_time=datetime(2025, 1, 10, tzinfo=UTC),
            source="test",
            source_hash="a" * 64,
            revenue_growth=0.05,
            net_profit_growth=-0.40,
            roe=0.08,
            operating_cashflow_to_profit=-0.2,
            debt_to_assets=0.80,
        )
        valuations = [
            ValuationSnapshot(
                "300001",
                strong[-1].trade_date,
                pe=12,
                pb=1.2,
                total_mv=1000,
                circ_mv=900,
                turnover_rate=2.5,
                volume_ratio=1.1,
            )
        ]
        baseline = next(
            item
            for item in select_candidates(
                securities,
                bars,
                strong[-1].trade_date,
                valuations=valuations,
                top_n=2,
            )
            if item.symbol == "300001"
        )
        candidate = select_candidates(
            securities,
            bars,
            strong[-1].trade_date,
            valuations=valuations,
            fundamentals=[risky_snapshot],
            top_n=2,
        )
        candidate = next(item for item in candidate if item.symbol == "300001")
        report = render_daily_report(strong[-1].trade_date, [candidate])
        self.assertGreater(baseline.score, 64)
        self.assertLess(candidate.score, baseline.score)
        self.assertLessEqual(candidate.score, 64)
        self.assertEqual(candidate.decision_band, "watch_only")
        self.assertIn("基本面风险：净利润同比大幅下滑", report)
        self.assertIn("基本面风险：经营现金流与利润背离", report)
        self.assertIn("基本面风险：资产负债率偏高", report)


class UniverseTests(unittest.TestCase):
    def test_filters_st_and_keeps_chinext(self) -> None:
        bars = make_bars("300001", 25)
        securities = [
            Security("300001", "测试股份", date(2020, 1, 1)),
            Security("000002", "ST测试", date(2020, 1, 1), is_st=True),
        ]
        result = eligible_securities(securities, bars, bars[-1].trade_date)
        self.assertEqual([item.symbol for item in result], ["300001"])


class BacktestTests(unittest.TestCase):
    def test_signal_executes_next_day_not_same_close(self) -> None:
        bars = make_bars("300001", 12)
        signal_date = bars[2].trade_date
        trades = run_fixed_horizon_backtest(
            bars,
            [signal_date],
            lambda _date, _visible: ["300001"],
            holding_days=3,
            slippage_rate=0,
            commission_rate=0,
            stamp_tax_rate=0,
        )
        self.assertEqual(trades[0].entry_date, bars[3].trade_date)
        self.assertEqual(trades[0].exit_date, bars[6].trade_date)
        self.assertGreater(summarize(trades)["mean_return"], 0)

    def test_limit_up_blocks_entry(self) -> None:
        bars = make_bars("300001", 8)
        blocked = DailyBar(
            **{
                **{field: getattr(bars[2], field) for field in bars[2].__dataclass_fields__},
                "is_limit_up": True,
            }
        )
        bars[2] = blocked
        trade = run_fixed_horizon_backtest(
            bars,
            [bars[1].trade_date],
            lambda _date, _visible: ["300001"],
            holding_days=2,
            slippage_rate=0,
            commission_rate=0,
            stamp_tax_rate=0,
        )[0]
        self.assertEqual(trade.entry_date, bars[3].trade_date)


if __name__ == "__main__":
    unittest.main()

from __future__ import annotations

import csv
import sqlite3
import tempfile
import unittest
from pathlib import Path

from aplan.announcement_event_validation import (
    Observation,
    Signal,
    _canonicalizer,
    _apply_training_nomination,
    _gate,
    _load_events,
    _load_benchmarks,
    _matched_controls,
    _purged_development_cutoff,
    _return,
)


class AnnouncementEventValidationTests(unittest.TestCase):
    def test_costs_reduce_gross_return(self) -> None:
        gross, net = _return(10.0, 11.0)
        self.assertAlmostEqual(gross, 0.10)
        self.assertLess(net, gross)

    def test_aliases_resolve_to_one_economic_entity(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "aliases.csv"
            path.write_text(
                "old_symbol,new_symbol,last_old_date,first_new_date,entity_name,reason,source_url\n"
                "000001,000002,20230101,20230102,测试,更名,https://example.test\n",
                encoding="utf-8",
            )
            aliases, digest = _canonicalizer(path)
        self.assertEqual(aliases["000001"], "000002")
        self.assertEqual(len(digest), 64)

    def test_negative_same_day_overrides_positive(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "events.csv"
            with path.open("w", encoding="utf-8", newline="") as handle:
                writer = csv.DictWriter(
                    handle,
                    fieldnames=(
                        "symbol",
                        "usable_from_trade_date",
                        "event_type",
                        "impact_hint",
                    ),
                )
                writer.writeheader()
                writer.writerow(
                    {
                        "symbol": "300001",
                        "usable_from_trade_date": "20250102",
                        "event_type": "share_buyback",
                        "impact_hint": "positive",
                    }
                )
                writer.writerow(
                    {
                        "symbol": "300001",
                        "usable_from_trade_date": "20250102",
                        "event_type": "regulatory_action",
                        "impact_hint": "negative",
                    }
                )
                writer.writerow(
                    {
                        "symbol": "300001",
                        "usable_from_trade_date": "20260102",
                        "event_type": "share_buyback",
                        "impact_hint": "positive",
                    }
                )
            signals, _, audit = _load_events(
                path,
                {},
                {"20250102": 0},
            )
        self.assertEqual(len(signals), 1)
        self.assertEqual(signals[0].direction, "negative")
        self.assertTrue(audit["stopped_at_2026_boundary"])
        self.assertFalse(audit["2026_holdout_opened"])

    def test_unsorted_event_index_is_rejected_before_holdout(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "events.csv"
            path.write_text(
                "symbol,usable_from_trade_date,event_type,impact_hint\n"
                "600000,20250103,share_buyback,positive\n"
                "600001,20240103,share_buyback,positive\n",
                encoding="utf-8",
            )
            with self.assertRaisesRegex(ValueError, "未按"):
                _load_events(
                    path,
                    {},
                    {"20240103": 0, "20250103": 1},
                )

    def test_negative_gate_never_promotes_without_candidate_ablation(self) -> None:
        rows = [
            Observation(
                variant="negative_risk_cohort",
                family="regulatory_action",
                period="rolling_oos",
                horizon=5,
                usable_date=f"2025{month:02d}{day:02d}",
                entry_date=f"2025{month:02d}{day:02d}",
                symbol=f"{index:06d}",
                gross_return=-0.05,
                net_return=-0.053,
                market_excess=-0.04,
                industry_excess=-0.04,
                matched_excess=-0.03,
                market_regime="down_high",
                industry_code="801010.SI",
            )
            for index, (month, day) in enumerate(
                ((month, day) for month in range(1, 13) for day in range(1, 11)),
                1,
            )
        ]
        dates = {row.usable_date: index for index, row in enumerate(rows)}
        result = _gate(
            rows,
            dates,
            negative=True,
            daily_candidate_available=False,
        )
        self.assertNotEqual(result["gate_decision"], "risk_gate")
        self.assertEqual(result["daily_candidate_ablation"], "data_unavailable")

    def test_oos_cannot_nominate_rule_rejected_in_training(self) -> None:
        development = {"gate_decision": "reject"}
        rolling_oos = {"gate_decision": "survive_first_pass", "metrics": {}}
        result = _apply_training_nomination(
            development,
            rolling_oos,
            negative=False,
        )
        self.assertEqual(result["gate_decision"], "reject")
        self.assertFalse(result["training_nominated"])
        self.assertTrue(result["oos_supported"])

    def test_matched_control_requires_pit_industry_membership(self) -> None:
        connection = sqlite3.connect(":memory:")
        connection.row_factory = sqlite3.Row
        connection.execute("ATTACH DATABASE ':memory:' AS security_state")
        connection.execute(
            """
            CREATE TABLE security_state.daily_status (
                trade_date TEXT,
                symbol TEXT,
                is_suspended INTEGER
            )
            """
        )
        connection.execute(
            """
            CREATE TABLE bars (
                trade_date TEXT,
                symbol TEXT,
                open REAL,
                high REAL,
                low REAL,
                close REAL,
                turnover REAL,
                previous_close REAL,
                pre20_return REAL,
                median_turnover20 REAL,
                is_suspended INTEGER,
                is_limit_up INTEGER
            )
            """
        )
        rows = [
            ("20250102", "600000", 10, 10, 10, 10, 100, 9, 0.1, 100, 0, 0),
            ("20250102", "600001", 10, 10, 10, 10, 100, 9, 0.1, 100, 0, 0),
            ("20250102", "600002", 10, 10, 10, 10, 100, 9, 0.2, 100, 0, 0),
            ("20250103", "600001", 10, 10, 10, 10, 100, 9, 0.1, 100, 0, 0),
            ("20250103", "600002", 10, 10, 10, 10, 100, 9, 0.2, 100, 0, 0),
            ("20250106", "600001", 11, 11, 11, 11, 100, 10, 0.1, 100, 0, 0),
            ("20250106", "600002", 9, 9, 9, 9, 100, 10, 0.2, 100, 0, 0),
        ]
        connection.executemany("INSERT INTO bars VALUES (?,?,?,?,?,?,?,?,?,?,?,?)", rows)
        connection.executemany(
            "INSERT INTO security_state.daily_status VALUES (?,?,?)",
            [
                ("20250103", "600001", 0),
                ("20250103", "600002", 0),
            ],
        )
        controls = _matched_controls(
            connection,
            Signal("600000", "20250102", "share_buyback", "positive"),
            {"20250102": 0, "20250103": 1, "20250106": 2},
            "20250103",
            {1: "20250106"},
            "801010.SI",
            {"801010.SI": ["600001", "600002"]},
            {
                "600001": [("801010.SI", "20230101", "20241231")],
                "600002": [("801010.SI", "20230101", "20251231")],
            },
            {},
        )
        _, expected = _return(10, 9)
        self.assertAlmostEqual(controls[1], expected)
        connection.close()

    def test_market_loader_does_not_stop_at_other_index_2026_row(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            project = Path(directory)
            root = project / "data" / "processed" / "benchmarks"
            root.mkdir(parents=True)
            (root / "manifest.json").write_text(
                '{"status":"validated","coverage_end":"20251231",'
                '"point_in_time_constituents":true,"hashes":{}}',
                encoding="utf-8",
            )
            (root / "market_indices.csv").write_text(
                "index_code,trade_date,open,close\n"
                "000001.SH,20260102,10,10\n"
                "000300.SH,20250102,20,21\n",
                encoding="utf-8",
            )
            (root / "industry_daily.csv").write_text(
                "index_code,trade_date,open,close\n"
                "801010.SI,20250102,10,10\n",
                encoding="utf-8",
            )
            (root / "industry_constituents.csv").write_text(
                "index_code,symbol,in_date,out_date\n"
                "801010.SI,600000,20230101,\n",
                encoding="utf-8",
            )
            market, _, _, _, _ = _load_benchmarks(project, {})
        self.assertEqual(market[("000300.SH", "20250102")], (20.0, 21.0))

    def test_development_cutoff_purges_last_sixty_trading_days(self) -> None:
        dates = [f"2024{index:04d}" for index in range(1, 101)]
        self.assertEqual(
            _purged_development_cutoff(dates, "20240100", 60),
            "20240040",
        )


if __name__ == "__main__":
    unittest.main()

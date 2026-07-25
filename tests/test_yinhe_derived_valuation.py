from __future__ import annotations

import sqlite3
import csv
import json
import shutil
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from aplan.yinhe_derived_valuation import (
    PRICE_SCALE,
    PROTOCOL_SHA256,
    _connect_equity,
    _insert_equity,
    _latest_period,
    build_derived_valuations,
    derive_ttm,
    normalize_equity_rows,
)
from aplan.quality import file_sha256


def _row(**values: object) -> sqlite3.Row:
    connection = sqlite3.connect(":memory:")
    connection.row_factory = sqlite3.Row
    columns = list(values)
    connection.execute(
        f"CREATE TABLE facts ({','.join(f'{name} TEXT' for name in columns)})"
    )
    connection.execute(
        f"INSERT INTO facts VALUES ({','.join('?' for _ in columns)})",
        tuple(values.values()),
    )
    result = connection.execute("SELECT * FROM facts").fetchone()
    connection.close()
    assert result is not None
    return result


class YinheDerivedValuationTests(unittest.TestCase):
    def test_equity_requires_announcement_and_uses_next_trade_day(self) -> None:
        calendar = ["20221230", "20230103", "20230104", "20230105"]
        rows, audit = normalize_equity_rows(
            [
                {
                    "MARKET_CODE": "600000.SH",
                    "ANN_DATE": "20230103",
                    "CHANGE_DATE": "20221231",
                    "TOT_SHARE": "2935208.04",
                },
                {
                    "MARKET_CODE": "000001.SZ",
                    "CHANGE_DATE": "20221231",
                    "TOT_SHARE": "1940591.82",
                },
            ],
            calendar,
            "2026-07-25T00:00:00+00:00",
        )

        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["symbol"], "600000")
        self.assertEqual(rows[0]["available_date"], "20230104")
        self.assertEqual(rows[0]["change_date"], "20221231")
        self.assertEqual(rows[0]["total_share"], 2935208.04)
        self.assertEqual(audit["missing_announcement_date"], 1)

    def test_late_announced_share_state_is_not_available_early(self) -> None:
        calendar = ["20230103", "20230104", "20230105", "20230106"]
        rows, _ = normalize_equity_rows(
            [
                {
                    "MARKET_CODE": "600000.SH",
                    "ANN_DATE": "20230105",
                    "CHANGE_DATE": "20230103",
                    "TOT_SHARE": 100.0,
                }
            ],
            calendar,
            "2026-07-25T00:00:00+00:00",
        )
        self.assertEqual(rows[0]["available_date"], "20230106")
        self.assertGreater(rows[0]["available_date"], rows[0]["change_date"])

    def test_annual_ttm_uses_available_annual_profit(self) -> None:
        state = {
            "20221231": _row(
                period_end="20221231",
                net_profit="100",
                usable_from_trade_date="20230331",
            )
        }
        value, available, flags = derive_ttm(state, "20230403")
        self.assertEqual(value, 100.0)
        self.assertEqual(available, "20230331")
        self.assertEqual(flags, [])

    def test_interim_ttm_requires_all_three_components(self) -> None:
        state = {
            "20230630": _row(
                period_end="20230630",
                net_profit="70",
                usable_from_trade_date="20230831",
            ),
            "20221231": _row(
                period_end="20221231",
                net_profit="100",
                usable_from_trade_date="20230331",
            ),
            "20220630": _row(
                period_end="20220630",
                net_profit="40",
                usable_from_trade_date="20220831",
            ),
        }
        value, available, flags = derive_ttm(state, "20230901")
        self.assertEqual(value, 130.0)
        self.assertEqual(available, "20230831")
        self.assertEqual(flags, [])

        state.pop("20220630")
        value, available, flags = derive_ttm(state, "20230901")
        self.assertIsNone(value)
        self.assertEqual(available, "")
        self.assertEqual(flags, ["missing_ttm_component"])

    def test_unknown_or_single_quarter_period_is_rejected(self) -> None:
        state = {
            "20230531": _row(
                period_end="20230531",
                net_profit="10",
                usable_from_trade_date="20230630",
            )
        }
        value, _, flags = derive_ttm(state, "20230703")
        self.assertIsNone(value)
        self.assertEqual(flags, ["single_quarter_or_unknown_report"])

    def test_latest_period_never_uses_future_period(self) -> None:
        state = {
            "20221231": _row(period_end="20221231"),
            "20231231": _row(period_end="20231231"),
        }
        result = _latest_period(state, "20230630")
        self.assertIsNotNone(result)
        self.assertEqual(result[0], "20221231")

    def test_vendor_price_scale_is_explicit(self) -> None:
        self.assertEqual(13_200_000 / PRICE_SCALE, 13.2)

    def test_builds_exact_date_pit_valuation_without_opening_2026(self) -> None:
        with TemporaryDirectory() as tmp:
            project = Path(tmp)
            (project / "config").mkdir()
            source = (
                Path(__file__).resolve().parents[1]
                / "config" / "yinhe_derived_valuation.toml"
            )
            shutil.copy2(source, project / "config" / source.name)
            self.assertEqual(
                file_sha256(project / "config" / source.name),
                PROTOCOL_SHA256,
            )
            processed = project / "data" / "processed"
            processed.mkdir(parents=True)
            (processed / "trade_calendar.csv").write_text(
                "trade_date,is_open\n20221230,1\n20230103,1\n",
                encoding="utf-8",
            )

            equity_root = processed / "yinhe_equity_structure"
            equity_database = equity_root / "equity_structure.sqlite3"
            connection = _connect_equity(equity_database)
            with connection:
                _insert_equity(
                    connection,
                    [{
                        "symbol": "600000",
                        "market_code": "600000.SH",
                        "ann_date": "20221229",
                        "available_date": "20221230",
                        "change_date": "20221201",
                        "ex_change_date": "",
                        "total_share": 100.0,
                        "is_valid": "1",
                        "current_sign": "1",
                        "source_hash": "share-hash",
                        "downloaded_at": "2026-07-25T00:00:00+00:00",
                    }],
                )
            connection.execute("PRAGMA wal_checkpoint(TRUNCATE)")
            connection.close()
            (equity_root / "manifest.json").write_text(
                json.dumps({
                    "status": "validated",
                    "database_path": str(equity_database),
                    "database_sha256": file_sha256(equity_database),
                }),
                encoding="utf-8",
            )

            financial_root = processed / "yinhe_fundamentals"
            financial_root.mkdir()
            financial_database = financial_root / "financial_facts.sqlite3"
            connection = sqlite3.connect(financial_database)
            connection.execute(
                """
                CREATE TABLE financial_facts (
                    table_name TEXT, symbol TEXT, period_end TEXT,
                    statement_type TEXT, actual_ann_date TEXT,
                    usable_from_trade_date TEXT, net_profit REAL, equity REAL,
                    downloaded_at TEXT, source_hash TEXT
                )
                """
            )
            connection.executemany(
                "INSERT INTO financial_facts VALUES (?,?,?,?,?,?,?,?,?,?)",
                [
                    (
                        "income", "600000", "20221231", "1", "20221229",
                        "20221230", 1_000_000.0, None,
                        "2026-07-25T00:00:00+00:00", "profit-hash",
                    ),
                    (
                        "balance_sheet", "600000", "20221231", "1",
                        "20221229", "20221230", None, 5_000_000.0,
                        "2026-07-25T00:00:00+00:00", "equity-hash",
                    ),
                ],
            )
            connection.commit()
            connection.close()
            (financial_root / "manifest.json").write_text(
                json.dumps({
                    "status": "validated",
                    "database_sha256": file_sha256(financial_database),
                }),
                encoding="utf-8",
            )

            security_root = processed / "security_history"
            security_root.mkdir()
            security_database = security_root / "daily_status.sqlite3"
            connection = sqlite3.connect(security_database)
            connection.execute(
                "CREATE TABLE daily_status ("
                "trade_date TEXT,symbol TEXT,is_st INTEGER,is_suspended INTEGER)"
            )
            connection.execute(
                "INSERT INTO daily_status VALUES ('20230103','600000',0,0)"
            )
            connection.commit()
            connection.close()
            (security_root / "manifest.json").write_text(
                json.dumps({
                    "point_in_time": True,
                    "daily_status_sha256": file_sha256(security_database),
                }),
                encoding="utf-8",
            )

            raw_root = processed / "yinhe_daily"
            raw_root.mkdir()
            with (raw_root / "20230103.csv").open(
                "w", encoding="utf-8", newline=""
            ) as handle:
                writer = csv.DictWriter(
                    handle,
                    fieldnames=("symbol", "trade_date", "close"),
                )
                writer.writeheader()
                writer.writerow({
                    "symbol": "600000",
                    "trade_date": "20230103",
                    "close": "13200000",
                })

            result = build_derived_valuations(
                project, start_date="20230103", end_date="20230103"
            )

            self.assertEqual(result["status"], "validated")
            self.assertEqual(result["2026_rows"], 0)
            output = (
                processed / "yinhe_derived_valuations" / "20230103.csv"
            )
            with output.open(encoding="utf-8", newline="") as handle:
                row = next(csv.DictReader(handle))
            self.assertEqual(float(row["raw_close"]), 13.2)
            self.assertEqual(float(row["total_mv_yuan"]), 13_200_000.0)
            self.assertEqual(float(row["pe_ttm"]), 13.2)
            self.assertAlmostEqual(float(row["pb"]), 2.64)


if __name__ == "__main__":
    unittest.main()

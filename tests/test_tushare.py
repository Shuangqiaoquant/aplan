from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from aplan.sync import backfill_market, build_daily_bars
from aplan.tushare import TushareClient, TushareError, load_env


class _Response:
    def __init__(self, body: dict[str, object]) -> None:
        self.body = json.dumps(body).encode()

    def __enter__(self) -> "_Response":
        return self

    def __exit__(self, *_args: object) -> None:
        return None

    def read(self) -> bytes:
        return self.body


class TushareTests(unittest.TestCase):
    def test_load_env_does_not_override_existing_secret(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / ".env"
            path.write_text("TUSHARE_TOKEN=from-file\n", encoding="utf-8")
            with patch.dict(os.environ, {"TUSHARE_TOKEN": "existing"}, clear=False):
                load_env(path)
                self.assertEqual(os.environ["TUSHARE_TOKEN"], "existing")

    @patch("urllib.request.urlopen")
    def test_query_maps_columns_to_rows(self, urlopen: object) -> None:
        urlopen.return_value = _Response(  # type: ignore[attr-defined]
            {"code": 0, "msg": None, "data": {"fields": ["symbol", "name"], "items": [["300001", "测试"]]}}
        )
        rows = TushareClient("secret").query("stock_basic")
        self.assertEqual(rows, [{"symbol": "300001", "name": "测试"}])

    @patch("urllib.request.urlopen")
    def test_api_error_does_not_reveal_token(self, urlopen: object) -> None:
        urlopen.return_value = _Response(  # type: ignore[attr-defined]
            {"code": -2001, "msg": "权限不足", "data": None}
        )
        with self.assertRaisesRegex(TushareError, "权限不足"):
            TushareClient("do-not-print").query("stock_basic")

    def test_build_daily_bars_converts_units_and_open_limit(self) -> None:
        daily = [
            {
                "ts_code": "300001.SZ",
                "trade_date": "20260702",
                "open": 11,
                "high": 11,
                "low": 11,
                "close": 11,
                "pre_close": 10,
                "vol": 123,
                "amount": 456,
            }
        ]
        limits = [{"ts_code": "300001.SZ", "up_limit": 11, "down_limit": 9}]
        bar = build_daily_bars(daily, limits)[0]
        self.assertEqual(bar["symbol"], "300001")
        self.assertEqual(bar["volume"], 12_300)
        self.assertEqual(bar["turnover"], 456_000)
        self.assertEqual(bar["is_limit_up"], "1")

    def test_one_price_rise_is_fallback_limit_when_limit_api_unavailable(self) -> None:
        daily = [
            {
                "ts_code": "300001.SZ",
                "trade_date": "20260702",
                "open": 12,
                "high": 12,
                "low": 12,
                "close": 12,
                "pre_close": 10,
                "vol": 1,
                "amount": 1,
            }
        ]
        self.assertEqual(build_daily_bars(daily, [])[0]["is_limit_up"], "1")

    def test_backfill_local_daily_uses_only_existing_non_empty_daily_snapshots(self) -> None:
        class Client:
            def __init__(self) -> None:
                self.requested: list[str] = []

            def query(self, _api_name: str, **kwargs: object) -> list[dict[str, object]]:
                self.requested.append(str(kwargs["trade_date"]))
                return []

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for day, count in (("20260101", 1), ("20260102", 0), ("20260103", 1)):
                path = root / "data" / "raw" / "tushare" / day
                path.mkdir(parents=True)
                (path / "daily.json").write_text(
                    json.dumps({"row_count": count, "rows": []}),
                    encoding="utf-8",
                )
            client = Client()
            backfill_market(  # type: ignore[arg-type]
                client,
                root,
                "20260101",
                "20260103",
                ("daily_basic",),
                calendar_mode="local-daily",
                delay_seconds=0,
            )
            self.assertEqual(client.requested, ["20260101", "20260103"])


if __name__ == "__main__":
    unittest.main()

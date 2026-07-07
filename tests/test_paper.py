from __future__ import annotations

import unittest

from aplan.paper import PaperOrder, PaperOrderStatus, execute_paper_orders
from aplan.portfolio import PortfolioState, Position
from aplan.risk import OrderSide


def order(side: OrderSide, quantity: int, execute_date: str = "20260707") -> PaperOrder:
    return PaperOrder(
        "order-1",
        "20260706",
        execute_date,
        "300001",
        side,
        quantity,
        "signal-1",
        10,
        PaperOrderStatus.PENDING,
    )


def market(*, one_price: str | None = None) -> list[dict[str, object]]:
    open_price = 12 if one_price == "up" else 8 if one_price == "down" else 10
    return [
        {
            "ts_code": "300001.SZ",
            "open": open_price,
            "high": open_price if one_price else 10.2,
            "low": open_price if one_price else 9.8,
            "close": open_price,
            "pre_close": 10,
        }
    ]


class PaperTests(unittest.TestCase):
    def portfolio(self, cash: float = 100_000) -> PortfolioState:
        return PortfolioState("paper", "20260706", cash, 100_000, 100_000)

    def test_buy_uses_next_open_costs_and_is_not_same_day_sellable(self) -> None:
        portfolio = self.portfolio()
        result = execute_paper_orders(portfolio, [order(OrderSide.BUY, 100)], market(), "20260707")
        self.assertEqual(len(result.fills), 1)
        self.assertEqual(portfolio.positions["300001"].quantity, 100)
        self.assertEqual(portfolio.positions["300001"].available_quantity, 0)
        self.assertLess(portfolio.cash, 99_000)

    def test_t_plus_one_blocks_same_day_sell(self) -> None:
        portfolio = self.portfolio()
        portfolio.positions["300001"] = Position("300001", 100, 10, 10, available_quantity=0)
        portfolio.as_of = "20260707"
        result = execute_paper_orders(portfolio, [order(OrderSide.SELL, 100)], market(), "20260707")
        self.assertFalse(result.fills)
        self.assertEqual(result.rejected[0]["reason"], "T+1可卖数量不足")

    def test_next_day_settlement_allows_sell_and_charges_tax(self) -> None:
        portfolio = self.portfolio()
        portfolio.positions["300001"] = Position("300001", 100, 10, 10, available_quantity=0)
        result = execute_paper_orders(portfolio, [order(OrderSide.SELL, 100)], market(), "20260707")
        self.assertEqual(len(result.fills), 1)
        self.assertGreater(result.fills[0].stamp_tax, 0)

    def test_one_price_limit_up_rejects_buy(self) -> None:
        result = execute_paper_orders(
            self.portfolio(),
            [order(OrderSide.BUY, 100)],
            market(one_price="up"),
            "20260707",
        )
        self.assertFalse(result.fills)
        self.assertIn("涨停", result.rejected[0]["reason"])


if __name__ == "__main__":
    unittest.main()


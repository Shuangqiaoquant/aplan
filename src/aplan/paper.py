from __future__ import annotations

import argparse
import csv
import hashlib
import json
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path
from typing import Any

from .portfolio import PortfolioState, PortfolioStore, Position
from .risk import OrderSide, ProposedOrder


class PaperOrderStatus(StrEnum):
    PENDING = "pending"
    FILLED = "filled"
    REJECTED = "rejected"
    EXPIRED = "expired"


@dataclass(frozen=True, slots=True)
class PaperOrder:
    order_id: str
    submitted_date: str
    execute_date: str
    symbol: str
    side: OrderSide
    quantity: int
    signal_id: str
    reference_price: float
    status: PaperOrderStatus = PaperOrderStatus.PENDING


@dataclass(frozen=True, slots=True)
class PaperFill:
    fill_id: str
    order_id: str
    trade_date: str
    symbol: str
    side: OrderSide
    quantity: int
    fill_price: float
    gross_value: float
    commission: float
    stamp_tax: float
    cash_effect: float


@dataclass(slots=True)
class PaperExecution:
    fills: list[PaperFill]
    rejected: list[dict[str, str]]
    expired: list[str]

    def to_dict(self) -> dict[str, object]:
        return {
            "fills": [asdict(fill) for fill in self.fills],
            "rejected": self.rejected,
            "expired": self.expired,
        }


def make_paper_order(
    order: ProposedOrder,
    submitted_date: str,
    execute_date: str,
) -> PaperOrder:
    raw = "|".join(
        (
            order.signal_id,
            submitted_date,
            execute_date,
            order.symbol,
            order.side.value,
            str(order.quantity),
        )
    )
    return PaperOrder(
        order_id=hashlib.sha256(raw.encode()).hexdigest()[:24],
        submitted_date=submitted_date,
        execute_date=execute_date,
        symbol=order.symbol,
        side=order.side,
        quantity=order.quantity,
        signal_id=order.signal_id,
        reference_price=order.reference_price,
    )


def _one_price(row: dict[str, Any], direction: str) -> bool:
    open_price = float(row["open"])
    flat = (
        abs(open_price - float(row["high"])) < 1e-8
        and abs(open_price - float(row["low"])) < 1e-8
    )
    pre_close = float(row.get("pre_close") or open_price)
    return flat and (open_price > pre_close if direction == "up" else open_price < pre_close)


def execute_paper_orders(
    portfolio: PortfolioState,
    orders: list[PaperOrder],
    rows: list[dict[str, Any]],
    trade_date: str,
    *,
    slippage_rate: float = 0.001,
    commission_rate: float = 0.0003,
    minimum_commission: float = 5.0,
    stamp_tax_rate: float = 0.0005,
) -> PaperExecution:
    if portfolio.as_of < trade_date:
        for position in portfolio.positions.values():
            position.available_quantity = position.quantity

    market = {
        str(row["ts_code"]).split(".", 1)[0]: row
        for row in rows
    }
    fills: list[PaperFill] = []
    rejected: list[dict[str, str]] = []
    expired: list[str] = []

    for order in orders:
        if order.status != PaperOrderStatus.PENDING:
            continue
        if order.execute_date != trade_date:
            if order.execute_date < trade_date:
                expired.append(order.order_id)
            continue
        row = market.get(order.symbol)
        if not row:
            rejected.append({"order_id": order.order_id, "reason": "停牌或缺少行情"})
            continue
        if order.side == OrderSide.BUY and _one_price(row, "up"):
            rejected.append({"order_id": order.order_id, "reason": "一字涨停无法买入"})
            continue
        if order.side == OrderSide.SELL and _one_price(row, "down"):
            rejected.append({"order_id": order.order_id, "reason": "一字跌停无法卖出"})
            continue

        open_price = float(row["open"])
        fill_price = open_price * (
            1 + slippage_rate if order.side == OrderSide.BUY else 1 - slippage_rate
        )
        gross = fill_price * order.quantity
        commission = max(minimum_commission, gross * commission_rate)
        stamp_tax = gross * stamp_tax_rate if order.side == OrderSide.SELL else 0.0
        position = portfolio.positions.get(order.symbol)

        if order.side == OrderSide.BUY:
            required_cash = gross + commission
            if required_cash > portfolio.cash:
                rejected.append({"order_id": order.order_id, "reason": "可用现金不足"})
                continue
            old_quantity = position.quantity if position else 0
            old_cost = position.average_cost * old_quantity if position else 0.0
            new_quantity = old_quantity + order.quantity
            average_cost = (old_cost + gross + commission) / new_quantity
            if position:
                position.quantity = new_quantity
                position.average_cost = average_cost
                position.market_price = fill_price
                # 当日买入部分不可卖；原有可卖数量保持不变。
            else:
                portfolio.positions[order.symbol] = Position(
                    symbol=order.symbol,
                    quantity=new_quantity,
                    average_cost=average_cost,
                    market_price=fill_price,
                    available_quantity=0,
                )
            portfolio.cash -= required_cash
            cash_effect = -required_cash
        else:
            if not position or position.available_quantity < order.quantity:
                rejected.append({"order_id": order.order_id, "reason": "T+1可卖数量不足"})
                continue
            proceeds = gross - commission - stamp_tax
            cost_basis = position.average_cost * order.quantity
            portfolio.realized_pnl += proceeds - cost_basis
            position.quantity -= order.quantity
            position.available_quantity -= order.quantity
            position.market_price = fill_price
            portfolio.cash += proceeds
            cash_effect = proceeds
            if position.quantity == 0:
                del portfolio.positions[order.symbol]

        fill_id = hashlib.sha256(
            f"{order.order_id}|{trade_date}|{fill_price:.8f}".encode()
        ).hexdigest()[:24]
        fills.append(
            PaperFill(
                fill_id=fill_id,
                order_id=order.order_id,
                trade_date=trade_date,
                symbol=order.symbol,
                side=order.side,
                quantity=order.quantity,
                fill_price=fill_price,
                gross_value=gross,
                commission=commission,
                stamp_tax=stamp_tax,
                cash_effect=cash_effect,
            )
        )

    for symbol, position in portfolio.positions.items():
        row = market.get(symbol)
        if row:
            position.market_price = float(row["close"])
    portfolio.as_of = trade_date
    portfolio.peak_nav = max(portfolio.peak_nav, portfolio.nav)
    return PaperExecution(fills, rejected, expired)


def load_daily_rows(project: Path, trade_date: str) -> list[dict[str, Any]]:
    path = project / "data" / "raw" / "tushare" / trade_date / "daily.json"
    if not path.exists():
        raise FileNotFoundError(path)
    return json.loads(path.read_text(encoding="utf-8")).get("rows", [])


def load_orders(path: Path) -> list[PaperOrder]:
    document = json.loads(path.read_text(encoding="utf-8"))
    return [
        PaperOrder(
            **{
                **item,
                "side": OrderSide(item["side"]),
                "status": PaperOrderStatus(item.get("status", "pending")),
            }
        )
        for item in document
    ]


def write_execution(
    project: Path,
    portfolio_id: str,
    trade_date: str,
    execution: PaperExecution,
) -> Path:
    directory = project / "state" / "paper" / portfolio_id / "executions"
    directory.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%S%fZ")
    path = directory / f"{trade_date}_{timestamp}.json"
    path.write_text(
        json.dumps(execution.to_dict(), ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    return path


def main() -> None:
    parser = argparse.ArgumentParser(description="APlan 纸面模拟成交")
    parser.add_argument("command", choices=["execute"])
    parser.add_argument("--portfolio", default="paper-main")
    parser.add_argument("--date", required=True)
    parser.add_argument("--orders", required=True)
    parser.add_argument("--root", default=".")
    parser.add_argument(
        "--confirm-paper-execution",
        action="store_true",
        help="明确确认将修改纸面组合状态；不会连接真实券商",
    )
    args = parser.parse_args()
    if not args.confirm_paper_execution:
        raise SystemExit("必须提供 --confirm-paper-execution")
    project = Path(args.root).resolve()
    store = PortfolioStore(project)
    portfolio = store.load(args.portfolio)
    if not portfolio:
        raise SystemExit(f"组合不存在：{args.portfolio}")
    execution = execute_paper_orders(
        portfolio,
        load_orders(Path(args.orders)),
        load_daily_rows(project, args.date),
        args.date,
    )
    current, history = store.save(portfolio, event=f"paper_execution:{args.date}")
    execution_path = write_execution(
        project,
        args.portfolio,
        args.date,
        execution,
    )
    print(f"成交 {len(execution.fills)}，拒单 {len(execution.rejected)}")
    print(f"组合状态：{current}")
    print(f"组合快照：{history}")
    print(f"成交记录：{execution_path}")


if __name__ == "__main__":
    main()


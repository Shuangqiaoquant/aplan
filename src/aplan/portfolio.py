from __future__ import annotations

import argparse
import json
import os
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any


@dataclass(slots=True)
class Position:
    symbol: str
    quantity: int
    average_cost: float
    market_price: float
    industry: str = "unknown"
    available_quantity: int = 0

    @property
    def market_value(self) -> float:
        return self.quantity * self.market_price


@dataclass(slots=True)
class PortfolioState:
    portfolio_id: str
    as_of: str
    cash: float
    initial_capital: float
    peak_nav: float
    positions: dict[str, Position] = field(default_factory=dict)
    realized_pnl: float = 0.0
    mode: str = "paper"

    @property
    def nav(self) -> float:
        return self.cash + sum(position.market_value for position in self.positions.values())

    @property
    def drawdown(self) -> float:
        return self.nav / self.peak_nav - 1 if self.peak_nav else 0.0

    def to_dict(self) -> dict[str, Any]:
        value = asdict(self)
        value["nav"] = self.nav
        value["drawdown"] = self.drawdown
        return value

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> "PortfolioState":
        positions = {
            symbol: Position(
                **{
                    **position,
                    "available_quantity": position.get(
                        "available_quantity",
                        position.get("quantity", 0),
                    ),
                }
            )
            for symbol, position in value.get("positions", {}).items()
        }
        return cls(
            portfolio_id=value["portfolio_id"],
            as_of=value["as_of"],
            cash=float(value["cash"]),
            initial_capital=float(value["initial_capital"]),
            peak_nav=float(value["peak_nav"]),
            positions=positions,
            realized_pnl=float(value.get("realized_pnl", 0)),
            mode=value.get("mode", "paper"),
        )


class PortfolioStore:
    def __init__(self, project_root: Path) -> None:
        self.directory = project_root / "state" / "portfolios"

    def current_path(self, portfolio_id: str) -> Path:
        return self.directory / portfolio_id / "current.json"

    def load(self, portfolio_id: str) -> PortfolioState | None:
        path = self.current_path(portfolio_id)
        if not path.exists():
            return None
        return PortfolioState.from_dict(json.loads(path.read_text(encoding="utf-8")))

    def save(self, state: PortfolioState, *, event: str) -> tuple[Path, Path]:
        directory = self.directory / state.portfolio_id
        history = directory / "history"
        history.mkdir(parents=True, exist_ok=True)
        timestamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%S%fZ")
        document = {
            "schema_version": 1,
            "event": event,
            "saved_at": datetime.now(UTC).isoformat(),
            "state": state.to_dict(),
        }
        history_path = history / f"{timestamp}.json"
        history_path.write_text(
            json.dumps(document, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        current = self.current_path(state.portfolio_id)
        temporary = current.with_suffix(".tmp")
        temporary.write_text(
            json.dumps(state.to_dict(), ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        os.replace(temporary, current)
        return current, history_path


def main() -> None:
    parser = argparse.ArgumentParser(description="管理 APlan 纸面组合")
    parser.add_argument("command", choices=["init", "show"])
    parser.add_argument("--id", default="paper-main")
    parser.add_argument("--capital", type=float)
    parser.add_argument("--date", default=datetime.now(UTC).strftime("%Y%m%d"))
    parser.add_argument("--root", default=".")
    args = parser.parse_args()
    store = PortfolioStore(Path(args.root).resolve())
    if args.command == "init":
        if args.capital is None or args.capital <= 0:
            raise SystemExit("init 必须提供正数 --capital")
        if store.load(args.id):
            raise SystemExit(f"组合已存在：{args.id}")
        state = PortfolioState(
            portfolio_id=args.id,
            as_of=args.date,
            cash=args.capital,
            initial_capital=args.capital,
            peak_nav=args.capital,
        )
        current, history = store.save(state, event="initialized")
        print(f"组合已初始化：{current}")
        print(f"历史快照：{history}")
    else:
        state = store.load(args.id)
        if not state:
            raise SystemExit(f"组合不存在：{args.id}")
        print(json.dumps(state.to_dict(), ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()

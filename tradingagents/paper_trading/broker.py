from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
import json
import math


@dataclass
class Position:
    ticker: str
    quantity: float = 0.0
    avg_price: float = 0.0

    def market_value(self, price: float) -> float:
        return self.quantity * price

    def unrealized_pnl(self, price: float) -> float:
        return (price - self.avg_price) * self.quantity


@dataclass
class PortfolioSnapshot:
    cash: float
    equity: float
    positions: dict[str, dict[str, float]]
    updated_at: str


@dataclass
class PaperBroker:
    """Tiny JSON-backed paper broker.

    It intentionally supports market orders only.  The engine owns signal
    generation and risk sizing; the broker just enforces cash/position bounds
    and records an auditable ledger.
    """

    state_path: Path
    initial_cash: float = 10_000_000.0
    cash: float = field(init=False)
    positions: dict[str, Position] = field(init=False, default_factory=dict)
    ledger: list[dict[str, Any]] = field(init=False, default_factory=list)

    def __post_init__(self) -> None:
        self.state_path = Path(self.state_path)
        self.state_path.parent.mkdir(parents=True, exist_ok=True)
        self._load()

    def _load(self) -> None:
        if not self.state_path.exists():
            self.cash = float(self.initial_cash)
            self.positions = {}
            self.ledger = []
            self._save()
            return
        data = json.loads(self.state_path.read_text(encoding="utf-8"))
        self.cash = float(data.get("cash", self.initial_cash))
        self.positions = {
            ticker: Position(ticker=ticker, **payload)
            for ticker, payload in data.get("positions", {}).items()
        }
        self.ledger = list(data.get("ledger", []))

    def _save(self) -> None:
        payload = {
            "cash": self.cash,
            "positions": {
                ticker: {"quantity": pos.quantity, "avg_price": pos.avg_price}
                for ticker, pos in self.positions.items()
                if abs(pos.quantity) > 1e-12
            },
            "ledger": self.ledger[-500:],
        }
        self.state_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")

    def buy(self, ticker: str, price: float, notional: float, reason: str) -> dict[str, Any]:
        if price <= 0 or notional <= 0:
            return self._reject(ticker, "BUY", price, 0, "invalid price/notional")
        spend = min(float(notional), self.cash)
        quantity = math.floor(spend / price)
        if quantity <= 0:
            return self._reject(ticker, "BUY", price, 0, "insufficient paper cash")
        cost = quantity * price
        pos = self.positions.get(ticker, Position(ticker=ticker))
        new_qty = pos.quantity + quantity
        pos.avg_price = ((pos.avg_price * pos.quantity) + cost) / new_qty
        pos.quantity = new_qty
        self.positions[ticker] = pos
        self.cash -= cost
        return self._record(ticker, "BUY", price, quantity, cost, reason)

    def sell(self, ticker: str, price: float, fraction: float, reason: str) -> dict[str, Any]:
        pos = self.positions.get(ticker)
        if not pos or pos.quantity <= 0:
            return self._reject(ticker, "SELL", price, 0, "no paper position")
        quantity = max(1, math.floor(pos.quantity * max(0.0, min(1.0, fraction))))
        quantity = min(quantity, math.floor(pos.quantity))
        proceeds = quantity * price
        pos.quantity -= quantity
        if pos.quantity <= 1e-12:
            self.positions.pop(ticker, None)
        else:
            self.positions[ticker] = pos
        self.cash += proceeds
        return self._record(ticker, "SELL", price, quantity, proceeds, reason)

    def hold(self, ticker: str, price: float, reason: str) -> dict[str, Any]:
        return self._record(ticker, "HOLD", price, 0, 0.0, reason)

    def _reject(self, ticker: str, side: str, price: float, quantity: float, reason: str) -> dict[str, Any]:
        event = self._event(ticker, f"REJECT_{side}", price, quantity, 0.0, reason)
        self.ledger.append(event)
        self._save()
        return event

    def _record(self, ticker: str, side: str, price: float, quantity: float, notional: float, reason: str) -> dict[str, Any]:
        event = self._event(ticker, side, price, quantity, notional, reason)
        self.ledger.append(event)
        self._save()
        return event

    @staticmethod
    def _event(ticker: str, side: str, price: float, quantity: float, notional: float, reason: str) -> dict[str, Any]:
        return {
            "ts": datetime.now(timezone.utc).isoformat(),
            "ticker": ticker,
            "side": side,
            "price": round(float(price), 6),
            "quantity": float(quantity),
            "notional": round(float(notional), 2),
            "reason": reason,
        }

    def snapshot(self, latest_prices: dict[str, float] | None = None) -> PortfolioSnapshot:
        latest_prices = latest_prices or {}
        positions = {}
        equity = self.cash
        for ticker, pos in self.positions.items():
            price = float(latest_prices.get(ticker, pos.avg_price))
            value = pos.market_value(price)
            equity += value
            positions[ticker] = {
                "quantity": pos.quantity,
                "avg_price": pos.avg_price,
                "last_price": price,
                "market_value": value,
                "unrealized_pnl": pos.unrealized_pnl(price),
            }
        return PortfolioSnapshot(
            cash=self.cash,
            equity=equity,
            positions=positions,
            updated_at=datetime.now(timezone.utc).isoformat(),
        )

    def snapshot_dict(self, latest_prices: dict[str, float] | None = None) -> dict[str, Any]:
        return asdict(self.snapshot(latest_prices))

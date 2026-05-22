from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Iterable, Any
import json
import time

from .broker import PaperBroker
from .data import MarketSnapshot, collect_market_snapshot

SnapshotProvider = Callable[[str], MarketSnapshot]


@dataclass
class StrategyConfig:
    cash_per_trade: float = 1_000_000.0
    max_position_pct: float = 0.30
    buy_momentum_pct: float = 0.6
    sell_momentum_pct: float = -0.8
    news_score_buy: int = 2
    news_score_sell: int = -2
    stop_loss_pct: float = -3.0
    take_profit_pct: float = 5.0


@dataclass
class Decision:
    ticker: str
    action: str
    reason: str
    confidence: float
    price: float
    change_pct: float | None
    news_score: int

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


class PaperTradingEngine:
    """Realtime-like polling engine for paper trading.

    This is intentionally broker-neutral and safe: it only writes JSON state and
    never places live orders.  A future KIS/Kiwoom adapter can consume the same
    decisions after an explicit live-trading approval gate.
    """

    def __init__(
        self,
        tickers: Iterable[str],
        broker: PaperBroker,
        config: StrategyConfig | None = None,
        snapshot_provider: SnapshotProvider = collect_market_snapshot,
        dashboard_path: Path | None = None,
    ) -> None:
        self.tickers = [t.strip().upper() for t in tickers if t.strip()]
        self.broker = broker
        self.config = config or StrategyConfig()
        self.snapshot_provider = snapshot_provider
        self.dashboard_path = Path(dashboard_path) if dashboard_path else None
        if self.dashboard_path:
            self.dashboard_path.parent.mkdir(parents=True, exist_ok=True)

    def decide(self, snapshot: MarketSnapshot) -> Decision:
        news_score = sum(item.sentiment_hint for item in snapshot.news)
        change = snapshot.change_pct or 0.0
        pos = self.broker.positions.get(snapshot.ticker)
        pnl_pct = None
        if pos and pos.avg_price:
            pnl_pct = (snapshot.price - pos.avg_price) / pos.avg_price * 100.0

        confidence = min(0.95, 0.45 + min(abs(change), 4.0) / 10.0 + min(abs(news_score), 5) / 20.0)
        if pnl_pct is not None and pnl_pct <= self.config.stop_loss_pct:
            return Decision(snapshot.ticker, "SELL", f"paper stop-loss triggered ({pnl_pct:.2f}%)", 0.95, snapshot.price, snapshot.change_pct, news_score)
        if pnl_pct is not None and pnl_pct >= self.config.take_profit_pct:
            return Decision(snapshot.ticker, "SELL", f"paper take-profit triggered ({pnl_pct:.2f}%)", 0.90, snapshot.price, snapshot.change_pct, news_score)
        if change >= self.config.buy_momentum_pct and news_score >= self.config.news_score_buy:
            return Decision(snapshot.ticker, "BUY", f"positive intraday momentum ({change:.2f}%) plus news/social score {news_score}", confidence, snapshot.price, snapshot.change_pct, news_score)
        if change <= self.config.sell_momentum_pct or news_score <= self.config.news_score_sell:
            return Decision(snapshot.ticker, "SELL", f"negative momentum/news guardrail: change {change:.2f}%, score {news_score}", confidence, snapshot.price, snapshot.change_pct, news_score)
        return Decision(snapshot.ticker, "HOLD", f"no paper-trade threshold met: change {change:.2f}%, score {news_score}", confidence, snapshot.price, snapshot.change_pct, news_score)

    def execute_decision(self, decision: Decision) -> dict[str, Any]:
        if decision.action == "BUY":
            latest_prices = {decision.ticker: decision.price}
            equity = self.broker.snapshot(latest_prices).equity
            existing_position = self.broker.positions.get(decision.ticker)
            current_value = existing_position.market_value(decision.price) if existing_position else 0.0
            max_value = equity * self.config.max_position_pct
            allowed_notional = max(0.0, min(self.config.cash_per_trade, max_value - current_value))
            return self.broker.buy(decision.ticker, decision.price, allowed_notional, decision.reason)
        if decision.action == "SELL":
            return self.broker.sell(decision.ticker, decision.price, 0.50, decision.reason)
        return self.broker.hold(decision.ticker, decision.price, decision.reason)

    def tick(self) -> dict[str, Any]:
        snapshots = []
        decisions = []
        events = []
        latest_prices = {}
        for ticker in self.tickers:
            snapshot = self.snapshot_provider(ticker)
            decision = self.decide(snapshot)
            event = self.execute_decision(decision)
            snapshots.append(snapshot.to_dict())
            decisions.append(decision.to_dict())
            events.append(event)
            latest_prices[ticker] = snapshot.price
        portfolio = self.broker.snapshot_dict(latest_prices)
        result = {
            "updated_at": datetime.now(timezone.utc).isoformat(),
            "tickers": self.tickers,
            "portfolio": portfolio,
            "snapshots": snapshots,
            "decisions": decisions,
            "events": events,
            "mode": "paper_trading_only",
        }
        if self.dashboard_path:
            self.write_dashboard_status(result)
        return result

    def run(self, interval_seconds: float = 30.0, iterations: int | None = None) -> None:
        count = 0
        while iterations is None or count < iterations:
            self.tick()
            count += 1
            if iterations is not None and count >= iterations:
                break
            time.sleep(interval_seconds)

    def write_dashboard_status(self, result: dict[str, Any]) -> None:
        if self.dashboard_path is None:
            return
        portfolio = result["portfolio"]
        decisions = result["decisions"]
        snapshots = result["snapshots"]
        status = {
            "updated_at": result["updated_at"],
            "metrics": {
                "mode": "paper trading only",
                "cash": f"{portfolio['cash']:.2f}",
                "equity": f"{portfolio['equity']:.2f}",
                "tickers": ", ".join(result["tickers"]),
                "latency_target": "polling interval controlled by --interval; quote/news fetch timeout ~4s/source",
            },
            "steps": [
                {"title": f"{d['ticker']} {d['action']}", "status": "done", "note": d["reason"]}
                for d in decisions
            ],
            "log": [
                f"{s['ticker']} price={s['price']} change={s['change_pct']} source={s['quote_source']} news={len(s['news'])} notes={'; '.join(s['notes'])}"
                for s in snapshots
            ],
            "paper": result,
        }
        self.dashboard_path.write_text(json.dumps(status, ensure_ascii=False, indent=2), encoding="utf-8")

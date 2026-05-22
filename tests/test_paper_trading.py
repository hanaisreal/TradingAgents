from __future__ import annotations

from pathlib import Path

from tradingagents.paper_trading.broker import PaperBroker
from tradingagents.paper_trading.data import MarketSnapshot, NewsItem
from tradingagents.paper_trading.engine import PaperTradingEngine, StrategyConfig


def test_paper_broker_buy_sell_roundtrip(tmp_path: Path):
    broker = PaperBroker(tmp_path / "state.json", initial_cash=1_000_000)
    buy = broker.buy("005930.KS", 70_000, 200_000, "test buy")
    assert buy["side"] == "BUY"
    assert broker.positions["005930.KS"].quantity == 2
    assert broker.cash == 860_000

    sell = broker.sell("005930.KS", 80_000, 0.5, "test sell")
    assert sell["side"] == "SELL"
    assert broker.positions["005930.KS"].quantity == 1
    assert broker.cash == 940_000

    reloaded = PaperBroker(tmp_path / "state.json", initial_cash=1)
    assert reloaded.cash == broker.cash
    assert reloaded.positions["005930.KS"].quantity == 1


def test_engine_executes_buy_from_momentum_and_news(tmp_path: Path):
    def provider(ticker: str) -> MarketSnapshot:
        return MarketSnapshot(
            ticker=ticker,
            price=100.0,
            previous_close=98.0,
            change_pct=2.04,
            volume=1000,
            timestamp="2026-01-01T00:00:00Z",
            quote_source="fake",
            news=[NewsItem(source="fake", title="호재 급등", sentiment_hint=2)],
        )

    broker = PaperBroker(tmp_path / "state.json", initial_cash=1_000)
    engine = PaperTradingEngine(
        ["TEST"],
        broker,
        config=StrategyConfig(cash_per_trade=300, max_position_pct=1.0),
        snapshot_provider=provider,
        dashboard_path=tmp_path / "status.json",
    )
    result = engine.tick()
    assert result["decisions"][0]["action"] == "BUY"
    assert result["events"][0]["side"] == "BUY"
    assert broker.positions["TEST"].quantity == 3
    assert (tmp_path / "status.json").exists()


def test_engine_holds_without_thresholds(tmp_path: Path):
    def provider(ticker: str) -> MarketSnapshot:
        return MarketSnapshot(
            ticker=ticker,
            price=100.0,
            previous_close=100.0,
            change_pct=0.0,
            volume=None,
            timestamp="2026-01-01T00:00:00Z",
            quote_source="fake",
            news=[],
        )

    broker = PaperBroker(tmp_path / "state.json", initial_cash=1_000)
    engine = PaperTradingEngine(["TEST"], broker, snapshot_provider=provider)
    result = engine.tick()
    assert result["decisions"][0]["action"] == "HOLD"
    assert result["events"][0]["side"] == "HOLD"

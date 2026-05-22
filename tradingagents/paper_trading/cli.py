from __future__ import annotations

import argparse
import json
from pathlib import Path

from .broker import PaperBroker
from .engine import PaperTradingEngine, StrategyConfig


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Run TradingAgents in paper-trading-only realtime polling mode.")
    parser.add_argument("--tickers", required=True, help="Comma-separated tickers, e.g. 005930.KS,247540.KQ")
    parser.add_argument("--cash", type=float, default=10_000_000.0, help="Initial paper cash")
    parser.add_argument("--cash-per-trade", type=float, default=1_000_000.0, help="Max notional per paper order")
    parser.add_argument("--interval", type=float, default=30.0, help="Polling interval in seconds")
    parser.add_argument("--iterations", type=int, default=1, help="Number of ticks; use 0 to run forever")
    parser.add_argument("--state", default=".paper_trading/state.json", help="JSON state path")
    parser.add_argument("--dashboard", default="dashboard/status.json", help="Dashboard status JSON path")
    parser.add_argument("--json", action="store_true", help="Print final tick as JSON")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    tickers = [ticker.strip() for ticker in args.tickers.split(",") if ticker.strip()]
    broker = PaperBroker(Path(args.state), initial_cash=args.cash)
    engine = PaperTradingEngine(
        tickers=tickers,
        broker=broker,
        config=StrategyConfig(cash_per_trade=args.cash_per_trade),
        dashboard_path=Path(args.dashboard) if args.dashboard else None,
    )
    iterations = None if args.iterations == 0 else args.iterations
    if iterations == 1:
        result = engine.tick()
    else:
        engine.run(interval_seconds=args.interval, iterations=iterations)
        result = broker.snapshot_dict()
    if args.json:
        print(json.dumps(result, ensure_ascii=False, indent=2))
    else:
        print(f"paper trading tick complete · tickers={','.join(tickers)} · state={args.state} · dashboard={args.dashboard}")
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())

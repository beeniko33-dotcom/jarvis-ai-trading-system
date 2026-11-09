#!/usr/bin/env python3
"""Command-line entry point for isolated offline strategy research."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SERVER_DIR = ROOT / "jarvis_server"
if str(SERVER_DIR) not in sys.path:
    sys.path.insert(0, str(SERVER_DIR))

from backtester import BacktestEngine


def main() -> int:
    parser = argparse.ArgumentParser(description="Run isolated offline Jarvis backtesting")
    parser.add_argument("--cycles", type=int, default=60, help="Number of synthetic research periods")
    parser.add_argument("--cash", type=float, default=100000.0, help="Starting research balance")
    parser.add_argument("--json", action="store_true", help="Emit machine-readable JSON output")
    args = parser.parse_args()

    payload = BacktestEngine(initial_cash=args.cash, symbols=["BTCUSD", "ETHUSD"]).run(
        cycles=max(args.cycles, 1)
    )
    if args.json:
        print(json.dumps(payload, indent=2))
    else:
        print("Offline synthetic-data backtest only")
        print(f"Portfolio value: ${payload['final_portfolio_value']:,.2f}")
        print(f"Return: {payload['return_pct']:+.2f}%")
        print(f"Trades: {payload['trade_count']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

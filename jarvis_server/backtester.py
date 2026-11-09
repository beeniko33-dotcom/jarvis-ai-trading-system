#!/usr/bin/env python3
"""Backtesting utilities for the Jarvis trading prototype."""

from __future__ import annotations

import math
from dataclasses import dataclass
from random import Random
from typing import Any

from strategy import MomentumStrategy


@dataclass
class BacktestTrade:
    symbol: str
    side: str
    quantity: float
    price: float
    cycle: int


class BacktestEngine:
    """Simple deterministic strategy backtester using synthetic market data."""

    def __init__(self, initial_cash: float = 100000.0, symbols: list[str] | None = None) -> None:
        self.initial_cash = float(initial_cash)
        self.symbols = symbols or ["BTCUSD", "ETHUSD", "AAPL", "NVDA"]
        self.strategy = MomentumStrategy()
        self.rng = Random(7)

    def run(self, cycles: int = 60, max_positions: int = 3, max_position_fraction: float = 0.35) -> dict[str, Any]:
        cash = self.initial_cash
        positions: dict[str, float] = {}
        trade_log: list[BacktestTrade] = []
        market_history: dict[str, list[float]] = {symbol: self._initial_series(symbol) for symbol in self.symbols}

        for cycle in range(1, max(1, cycles) + 1):
            for symbol in self.symbols:
                previous = market_history[symbol][-1]
                trend = math.sin((cycle + len(self.symbols)) / 10.0) * 0.008
                noise = self.rng.uniform(-0.024, 0.024)
                price = previous * (1.0 + trend + noise)
                market_history[symbol].append(price)

                signal = self.strategy.generate_signal(symbol, market_history[symbol])
                if signal.signal == "buy" and cash > 0 and len(positions) < max_positions:
                    amount = min(cash * max_position_fraction, cash * 0.2)
                    if amount > 0:
                        quantity = amount / price
                        positions[symbol] = positions.get(symbol, 0.0) + quantity
                        cash -= amount
                        trade_log.append(BacktestTrade(symbol=symbol, side="buy", quantity=quantity, price=price, cycle=cycle))
                elif signal.signal == "sell" and symbol in positions:
                    quantity = positions.pop(symbol)
                    cash += quantity * price
                    trade_log.append(BacktestTrade(symbol=symbol, side="sell", quantity=quantity, price=price, cycle=cycle))

        final_prices = {symbol: market_history[symbol][-1] for symbol in self.symbols}
        portfolio_value = cash + sum(positions.get(symbol, 0.0) * final_prices[symbol] for symbol in self.symbols)
        return {
            "cycles": cycles,
            "initial_cash": round(self.initial_cash, 2),
            "final_cash": round(cash, 2),
            "final_portfolio_value": round(portfolio_value, 2),
            "return_pct": round(((portfolio_value - self.initial_cash) / self.initial_cash) * 100.0, 2),
            "trade_count": len(trade_log),
            "final_positions": {symbol: round(positions.get(symbol, 0.0), 6) for symbol in self.symbols if positions.get(symbol, 0.0) > 0},
            "final_prices": {symbol: round(final_prices[symbol], 2) for symbol in self.symbols},
            "trades": [
                {"symbol": trade.symbol, "side": trade.side, "quantity": round(trade.quantity, 6), "price": round(trade.price, 2), "cycle": trade.cycle}
                for trade in trade_log[-10:]
            ],
        }

    def _initial_series(self, symbol: str) -> list[float]:
        base = {"BTCUSD": 62000.0, "ETHUSD": 3450.0, "AAPL": 192.0, "NVDA": 930.0}.get(symbol, 100.0)
        series = [base]
        for _ in range(1, 20):
            step = self.rng.uniform(-0.03, 0.03)
            series.append(series[-1] * (1.0 + step))
        return series

#!/usr/bin/env python3
"""Parameter optimization helper for the Jarvis strategy prototype."""

from __future__ import annotations

import math
from dataclasses import dataclass
from random import Random
from typing import Any


@dataclass
class OptimizationResult:
    short_window: int
    long_window: int
    threshold: float
    return_pct: float
    final_value: float
    trade_count: int


class StrategyOptimizer:
    """Simple deterministic search over moving-average strategy settings."""

    def __init__(self, initial_cash: float = 100000.0, symbols: list[str] | None = None) -> None:
        self.initial_cash = float(initial_cash)
        self.symbols = symbols or ["BTCUSD", "ETHUSD", "AAPL", "NVDA"]

    def optimize(self, cycles: int = 60, symbol: str = "BTCUSD") -> dict[str, Any]:
        cycles = max(1, int(cycles))
        price_history = self._build_price_history(cycles)
        results: list[OptimizationResult] = []
        for short_window in (3, 5, 7, 9):
            for long_window in (10, 12, 16, 20):
                if long_window <= short_window:
                    continue
                for threshold in (1.01, 1.02, 1.03, 1.05):
                    result = self._simulate(short_window, long_window, threshold, price_history)
                    results.append(result)

        best = max(results, key=lambda item: item.return_pct)
        ranked = sorted(results, key=lambda item: item.return_pct, reverse=True)[:5]
        return {
            "symbol": symbol,
            "cycles": cycles,
            "best": {
                "short_window": best.short_window,
                "long_window": best.long_window,
                "threshold": best.threshold,
                "return_pct": round(best.return_pct, 2),
                "final_value": round(best.final_value, 2),
                "trade_count": best.trade_count,
            },
            "ranked": [
                {
                    "short_window": item.short_window,
                    "long_window": item.long_window,
                    "threshold": item.threshold,
                    "return_pct": round(item.return_pct, 2),
                    "final_value": round(item.final_value, 2),
                    "trade_count": item.trade_count,
                }
                for item in ranked
            ],
        }

    def _build_price_history(self, cycles: int) -> list[float]:
        rng = Random(11)
        history = [100.0]
        for cycle in range(1, max(1, cycles) + 1):
            trend = math.sin(cycle / 11.0) * 0.02
            noise = rng.uniform(-0.05, 0.05)
            history.append(history[-1] * (1.0 + trend + noise))
        return history

    def _simulate(
        self,
        short_window: int,
        long_window: int,
        threshold: float,
        price_history: list[float],
    ) -> OptimizationResult:
        cash = self.initial_cash
        position = 0.0
        trade_count = 0
        history = [price_history[0]]

        for _, price in enumerate(price_history[1:], start=1):
            history.append(price)
            moving_short = self._moving_average(history, short_window)
            moving_long = self._moving_average(history, long_window)
            ratio = moving_short / moving_long if moving_long else 1.0

            if ratio >= threshold and position == 0:
                quantity = (cash * 0.2) / price
                cash -= quantity * price
                position = quantity
                trade_count += 1
            elif ratio <= 1.0 / threshold and position > 0:
                cash += position * price
                position = 0.0
                trade_count += 1

        final_value = cash + (position * price_history[-1])
        return OptimizationResult(
            short_window=short_window,
            long_window=long_window,
            threshold=threshold,
            return_pct=((final_value - self.initial_cash) / self.initial_cash) * 100.0,
            final_value=final_value,
            trade_count=trade_count,
        )

    def _moving_average(self, values: list[float], window: int) -> float:
        if len(values) < window:
            return sum(values) / max(1, len(values))
        return sum(values[-window:]) / window

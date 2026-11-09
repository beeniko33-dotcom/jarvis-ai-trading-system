#!/usr/bin/env python3
"""Strategy helpers used by the Jarvis trading engine."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass
class StrategySignal:
    symbol: str
    signal: str
    strength: float
    reason: str


class MomentumStrategy:
    """A simple moving-average-based strategy for local demo trading."""

    def __init__(
        self,
        *,
        short_window: int = 5,
        long_window: int = 12,
        buy_threshold: float = 1.02,
        sell_threshold: float = 0.98,
    ) -> None:
        self.short_window = max(2, int(short_window))
        self.long_window = max(self.short_window + 1, int(long_window))
        self.buy_threshold = float(buy_threshold)
        self.sell_threshold = float(sell_threshold)

    def as_config(self) -> dict[str, float | int]:
        return {
            "short_window": self.short_window,
            "long_window": self.long_window,
            "buy_threshold": self.buy_threshold,
            "sell_threshold": self.sell_threshold,
        }

    def update(self, **values: float | int) -> dict[str, float | int]:
        if "short_window" in values:
            self.short_window = max(2, int(values["short_window"]))
        if "long_window" in values:
            self.long_window = max(self.short_window + 1, int(values["long_window"]))
        if "buy_threshold" in values:
            self.buy_threshold = float(values["buy_threshold"])
        if "sell_threshold" in values:
            self.sell_threshold = float(values["sell_threshold"])
        return self.as_config()

    def generate_signal(self, symbol: str, history: list[float]) -> StrategySignal:
        if not history:
            return StrategySignal(symbol=symbol, signal="hold", strength=0.0, reason="No price history")

        short_avg = self._moving_average(history, self.short_window)
        long_avg = self._moving_average(history, self.long_window)
        ratio = short_avg / long_avg if long_avg else 1.0

        if ratio > self.buy_threshold:
            return StrategySignal(
                symbol=symbol,
                signal="buy",
                strength=round((ratio - 1.0) * 100.0, 2),
                reason="Momentum above long-term trend",
            )
        if ratio < self.sell_threshold:
            return StrategySignal(
                symbol=symbol,
                signal="sell",
                strength=round((1.0 - ratio) * 100.0, 2),
                reason="Momentum below long-term trend",
            )
        return StrategySignal(symbol=symbol, signal="hold", strength=0.0, reason="Range-bound pricing")

    def _moving_average(self, values: list[float], window: int) -> float:
        if len(values) < window:
            return sum(values) / max(1, len(values))
        return sum(values[-window:]) / window

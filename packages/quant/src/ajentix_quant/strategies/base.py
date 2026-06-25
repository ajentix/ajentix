"""Strategy contract. Strategies are pure deterministic functions of market state."""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass


@dataclass(frozen=True)
class Signal:
    symbol: str
    enter: bool  # True = hold delta-neutral carry, False = flat
    target_delta: float  # 0.0 for market-neutral
    reason: str


class Strategy(ABC):
    name: str

    @abstractmethod
    def signal(self, *, symbol: str, funding_rate_8h: float) -> Signal:
        """Emit a deterministic signal from current market state."""

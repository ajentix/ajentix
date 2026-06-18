"""Adapter contract.

We abstract only the *commodity plumbing* (connect/auth/fetch/order). Venue-specific
microstructure (funding interval, mechanics) is surfaced as typed data, never flattened,
because that microstructure is itself a source of alpha.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass


@dataclass(frozen=True)
class FundingRate:
    symbol: str
    rate: float  # fractional per interval, e.g. 0.0001 == 0.01%
    interval_hours: float
    timestamp: int  # epoch ms


@dataclass(frozen=True)
class Candle:
    timestamp: int  # epoch ms
    open: float
    high: float
    low: float
    close: float
    volume: float


class VenueAdapter(ABC):
    """Read paths are implemented in Phase 0; order placement arrives in Phase 2."""

    name: str

    @abstractmethod
    def fetch_funding_rate(self, symbol: str) -> FundingRate:
        """Current funding rate for a perpetual symbol."""

    @abstractmethod
    def fetch_ohlcv(self, symbol: str, timeframe: str = "1h", limit: int = 500) -> list[Candle]:
        """Recent OHLCV candles."""

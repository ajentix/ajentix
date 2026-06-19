"""Offline replay adapter: serve a cached ``MarketDataset`` through the ``VenueAdapter`` API.

Deterministic and network-free by construction. A request for a symbol / stream that the
dataset does not contain raises ``KeyError`` (fail closed) — there is NO network fallback.
"""

from __future__ import annotations

from pathlib import Path

from ..adapters.base import (
    Candle,
    FundingRate,
    FundingRateHistoryRequest,
    HistoricalCandle,
    MarketDataset,
    MarketType,
    PriceType,
    StreamKey,
    VenueAdapter,
)
from .cache import load_dataset


class ReplayVenueAdapter(VenueAdapter):
    """A ``VenueAdapter`` backed entirely by a cached, validated ``MarketDataset``."""

    name = "replay"

    def __init__(self, dataset: MarketDataset) -> None:
        self.dataset = dataset

    @classmethod
    def from_cache(cls, cache_root: str | Path, scenario_id: str) -> ReplayVenueAdapter:
        return cls(load_dataset(cache_root, scenario_id))

    def _funding(self, symbol: str) -> tuple[FundingRate, ...]:
        series = self.dataset.funding.get(symbol)
        if series is None:
            raise KeyError(f"no funding series cached for {symbol}")
        return series

    def _stream(self, key: StreamKey) -> tuple[HistoricalCandle, ...]:
        series = self.dataset.ohlcv.get(key)
        if series is None:
            raise KeyError(f"no OHLCV stream cached for {key}")
        return series

    def fetch_funding_rate(self, symbol: str) -> FundingRate:
        return self._funding(symbol)[-1]

    def fetch_ohlcv(self, symbol: str, timeframe: str = "1h", limit: int = 500) -> list[Candle]:
        key = StreamKey(symbol, MarketType.LINEAR_PERP, PriceType.TRADE)
        self._check_timeframe(timeframe)
        series = self._stream(key)
        tail = series[-limit:] if limit > 0 else series
        return [
            Candle(c.timestamp_ms, c.open, c.high, c.low, c.close, c.volume or 0.0) for c in tail
        ]

    def fetch_funding_rate_history(
        self, request: FundingRateHistoryRequest
    ) -> list[FundingRate]:
        return [
            fr
            for fr in self._funding(request.symbol)
            if request.since_ms <= fr.timestamp <= request.until_ms
        ]

    def fetch_ohlcv_history(
        self,
        symbol: str,
        timeframe: str,
        since_ms: int,
        until_ms: int,
        *,
        market_type: MarketType,
        price_type: PriceType,
    ) -> list[HistoricalCandle]:
        self._check_timeframe(timeframe)
        series = self._stream(StreamKey(symbol, market_type, price_type))
        return [c for c in series if since_ms <= c.timestamp_ms <= until_ms]

    def _check_timeframe(self, timeframe: str) -> None:
        if timeframe != self.dataset.timeframe:
            raise ValueError(
                f"replay dataset holds timeframe {self.dataset.timeframe!r}, "
                f"requested {timeframe!r} (no resampling / no network fallback)"
            )

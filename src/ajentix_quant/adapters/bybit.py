"""Bybit adapter (v1 reference) via ccxt — read-only in Phase 0.

`ccxt` is imported lazily so offline backtests and tests need no network/heavy deps.
Install with `pip install -e ".[live]"` to use this adapter.
"""

from __future__ import annotations

from .base import Candle, FundingRate, VenueAdapter


class BybitAdapter(VenueAdapter):
    name = "bybit"

    def __init__(self, api_key: str = "", api_secret: str = "") -> None:
        import ccxt  # lazy: only required for live connectivity

        self._ex = ccxt.bybit(
            {
                "apiKey": api_key,
                "secret": api_secret,
                "enableRateLimit": True,
                # Trade-only keys (NO withdrawal). Default to swap (perps) for funding.
                "options": {"defaultType": "swap"},
            }
        )

    def fetch_funding_rate(self, symbol: str) -> FundingRate:
        fr = self._ex.fetch_funding_rate(symbol)
        return FundingRate(
            symbol=symbol,
            rate=float(fr["fundingRate"]),
            interval_hours=8.0,
            timestamp=int(fr.get("timestamp") or 0),
        )

    def fetch_ohlcv(self, symbol: str, timeframe: str = "1h", limit: int = 500) -> list[Candle]:
        rows = self._ex.fetch_ohlcv(symbol, timeframe=timeframe, limit=limit)
        return [
            Candle(int(r[0]), float(r[1]), float(r[2]), float(r[3]), float(r[4]), float(r[5]))
            for r in rows
        ]

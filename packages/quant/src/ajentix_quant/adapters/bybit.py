"""Bybit adapter (v1 reference) via ccxt — read-only in Phase 0.

`ccxt` is imported lazily so offline backtests and tests need no network/heavy deps.
Install with `pip install -e ".[live]"` to use this adapter.
"""

from __future__ import annotations

import math

from .base import (
    Candle,
    FundingRate,
    FundingRateHistoryRequest,
    HistoricalCandle,
    MarketType,
    PriceType,
    VenueAdapter,
)

_MAX_PAGES = 10_000
_FUNDING_HISTORY_LIMIT = 200
_OHLCV_HISTORY_LIMIT = 1_000


def spot_symbol_from_perp(perp_symbol: str) -> str:
    """Return the spot symbol for a ccxt linear-perp symbol.

    Example: ``BTC/USDT:USDT`` -> ``BTC/USDT``.
    """

    spot_symbol, separator, settle = perp_symbol.partition(":")
    if separator == "" or not spot_symbol or not settle:
        raise ValueError(f"perp symbol must include a ':' settle suffix: {perp_symbol!r}")
    return spot_symbol


def _finite_float(value: object, label: str) -> float:
    try:
        out = float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{label} must be a finite float, got {value!r}") from exc
    if not math.isfinite(out):
        raise ValueError(f"{label} must be finite, got {value!r}")
    return out


def _nonnegative_float(value: object, label: str) -> float:
    out = _finite_float(value, label)
    if out < 0:
        raise ValueError(f"{label} must be >= 0, got {value!r}")
    return out


def _row_timestamp(row: dict, label: str) -> int:
    try:
        return int(row["timestamp"])
    except (KeyError, TypeError, ValueError) as exc:
        raise ValueError(f"{label} row missing integer timestamp: {row!r}") from exc


def _funding_rate(row: dict) -> float:
    value = row.get("fundingRate")
    if value is None:
        info = row.get("info") or {}
        if isinstance(info, dict):
            value = info.get("fundingRate")
    if value is None:
        raise ValueError(f"funding row missing fundingRate: {row!r}")
    return _finite_float(value, "fundingRate")


def _candle_timestamp(row: list | tuple) -> int:
    try:
        return int(row[0])
    except (IndexError, TypeError, ValueError) as exc:
        raise ValueError(f"OHLCV row missing integer timestamp: {row!r}") from exc


def _ohlcv_price_params(price_type: PriceType) -> dict[str, str]:
    if price_type is PriceType.TRADE:
        return {}
    if price_type is PriceType.MARK:
        return {"price": "mark"}
    if price_type is PriceType.INDEX:
        return {"price": "index"}
    raise ValueError(f"unsupported price_type: {price_type!r}")


class BybitAdapter(VenueAdapter):
    name = "bybit"

    def __init__(self, api_key: str = "", api_secret: str = "", *, exchange=None) -> None:
        if exchange is not None:
            self._ex = exchange
            return

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

    def fetch_funding_rate_history(
        self, request: FundingRateHistoryRequest
    ) -> list[FundingRate]:
        if request.since_ms > request.until_ms:
            raise ValueError(
                f"since_ms must be <= until_ms, got {request.since_ms} > {request.until_ms}"
            )
        if request.limit <= 0:
            raise ValueError(f"funding history limit must be > 0, got {request.limit}")

        limit = min(request.limit, _FUNDING_HISTORY_LIMIT)
        cursor = request.since_ms
        by_timestamp: dict[int, FundingRate] = {}

        for _ in range(_MAX_PAGES):
            if cursor > request.until_ms:
                break

            page = self._ex.fetch_funding_rate_history(
                request.symbol,
                since=cursor,
                limit=limit,
                params={"until": request.until_ms, "paginate": True},
            )
            if not page:
                break

            max_timestamp = max(_row_timestamp(row, "funding history") for row in page)
            for row in page:
                timestamp = _row_timestamp(row, "funding history")
                if (
                    request.since_ms <= timestamp <= request.until_ms
                    and timestamp not in by_timestamp
                ):
                    by_timestamp[timestamp] = FundingRate(
                        symbol=request.symbol,
                        rate=_funding_rate(row),
                        interval_hours=8.0,
                        timestamp=timestamp,
                    )

            next_cursor = max_timestamp + 1
            if next_cursor <= cursor:
                raise RuntimeError(
                    "Bybit funding history pagination failed to advance "
                    f"for {request.symbol}: cursor={cursor}, max_timestamp={max_timestamp}"
                )
            cursor = next_cursor
        else:
            raise RuntimeError(
                f"Bybit funding history pagination exceeded {_MAX_PAGES} pages for {request.symbol}"
            )

        return [by_timestamp[timestamp] for timestamp in sorted(by_timestamp)]

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
        if since_ms > until_ms:
            raise ValueError(f"since_ms must be <= until_ms, got {since_ms} > {until_ms}")

        cursor = since_ms
        params = _ohlcv_price_params(price_type)
        by_timestamp: dict[int, HistoricalCandle] = {}

        for _ in range(_MAX_PAGES):
            if cursor > until_ms:
                break

            page = self._ex.fetch_ohlcv(
                symbol,
                timeframe=timeframe,
                since=cursor,
                limit=_OHLCV_HISTORY_LIMIT,
                params=dict(params),
            )
            if not page:
                break

            max_timestamp = max(_candle_timestamp(row) for row in page)
            for row in page:
                timestamp = _candle_timestamp(row)
                if timestamp < since_ms or timestamp > until_ms or timestamp in by_timestamp:
                    continue
                if len(row) < 5:
                    raise ValueError(f"OHLCV row must contain timestamp and OHLC, got {row!r}")
                volume = None
                if price_type is PriceType.TRADE:
                    if len(row) < 6:
                        raise ValueError(f"trade OHLCV row missing volume: {row!r}")
                    volume = _nonnegative_float(row[5], "ohlcv.volume")
                by_timestamp[timestamp] = HistoricalCandle(
                    timestamp_ms=timestamp,
                    symbol=symbol,
                    venue=self.name,
                    market_type=market_type,
                    price_type=price_type,
                    timeframe=timeframe,
                    open=_nonnegative_float(row[1], "ohlcv.open"),
                    high=_nonnegative_float(row[2], "ohlcv.high"),
                    low=_nonnegative_float(row[3], "ohlcv.low"),
                    close=_nonnegative_float(row[4], "ohlcv.close"),
                    volume=volume,
                )

            next_cursor = max_timestamp + 1
            if next_cursor <= cursor:
                raise RuntimeError(
                    "Bybit OHLCV pagination failed to advance "
                    f"for {symbol} {timeframe}: cursor={cursor}, max_timestamp={max_timestamp}"
                )
            cursor = next_cursor
        else:
            raise RuntimeError(f"Bybit OHLCV pagination exceeded {_MAX_PAGES} pages for {symbol}")

        return [by_timestamp[timestamp] for timestamp in sorted(by_timestamp)]

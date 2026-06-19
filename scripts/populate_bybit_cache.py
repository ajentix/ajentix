#!/usr/bin/env python3
"""Populate an aq-cache-v1 scenario from public Bybit market data.

Manual network tool: do not run in CI. Requires the live extra (ccxt) and network access.
"""

from __future__ import annotations

import argparse
import os
import sys
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path

# Allow running from a checkout without installing (src layout).
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from ajentix_quant.adapters.base import (  # noqa: E402
    FundingRateHistoryRequest,
    HistoricalCandle,
    MarketType,
    PriceType,
    SourceQuality,
    StreamKey,
    StreamName,
)
from ajentix_quant.adapters.bybit import BybitAdapter, spot_symbol_from_perp  # noqa: E402
from ajentix_quant.data.cache import write_cache  # noqa: E402

_REQUIRED_STREAMS = (
    StreamName.FUNDING_HISTORY,
    StreamName.SPOT_TRADE_OHLCV,
    StreamName.PERP_TRADE_OHLCV,
    StreamName.PERP_MARK_OHLCV,
)


def _parse_iso_ms(value: str) -> int:
    normalized = value[:-1] + "+00:00" if value.endswith("Z") else value
    dt = datetime.fromisoformat(normalized)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return int(dt.timestamp() * 1000)


def _retag_symbol(rows: list[HistoricalCandle], symbol: str) -> list[HistoricalCandle]:
    """Re-key spot candles under the canonical *scenario* symbol (the perp symbol).

    In aq-cache-v1 ``HistoricalCandle.symbol`` is the scenario/pair key, not the venue-native
    order symbol; the leg is distinguished by ``market_type`` (SPOT vs LINEAR_PERP). The cache
    validates required streams per scenario symbol, so the spot leg (fetched on e.g. BTC/USDT)
    is stored under the perp scenario symbol (BTC/USDT:USDT) with ``market_type=SPOT`` left
    intact. NOTE for Phase 2 live execution: do NOT route spot orders using this scenario
    symbol — derive the native spot symbol (spot_symbol_from_perp) at the order boundary.
    """
    return [replace(row, symbol=symbol) for row in rows]


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Populate an aq-cache-v1 scenario from public Bybit market data. "
        "Manual network tool; never run in CI."
    )
    parser.add_argument("--venue", default=BybitAdapter.name, choices=[BybitAdapter.name])
    parser.add_argument(
        "--symbols", nargs="+", required=True, help="Perp symbols, e.g. BTC/USDT:USDT"
    )
    parser.add_argument("--timeframe", default="1h")
    parser.add_argument("--since", required=True, help="ISO8601 start timestamp")
    parser.add_argument("--until", required=True, help="ISO8601 end timestamp")
    parser.add_argument("--out", required=True, type=Path, help="Cache root directory")
    parser.add_argument("--scenario-id", required=True)
    parser.add_argument("--train-until", help="Optional ISO8601 train/test boundary")
    return parser


def main(argv=None) -> None:
    if os.environ.get("CI"):
        raise SystemExit("populate_bybit_cache is a manual network tool and must not run in CI")
    args = _parser().parse_args(argv)
    since_ms = _parse_iso_ms(args.since)
    until_ms = _parse_iso_ms(args.until)
    if since_ms > until_ms:
        raise SystemExit(f"--since must be <= --until, got {args.since!r} > {args.until!r}")
    train_until_ms = _parse_iso_ms(args.train_until) if args.train_until else None

    adapter = BybitAdapter(api_key="", api_secret="")
    funding = {}
    ohlcv = {}
    row_counts: dict[str, dict[StreamName, int]] = {}

    for perp_symbol in args.symbols:
        spot_symbol = spot_symbol_from_perp(perp_symbol)
        funding_rows = adapter.fetch_funding_rate_history(
            FundingRateHistoryRequest(symbol=perp_symbol, since_ms=since_ms, until_ms=until_ms)
        )
        perp_trade = adapter.fetch_ohlcv_history(
            perp_symbol,
            args.timeframe,
            since_ms,
            until_ms,
            market_type=MarketType.LINEAR_PERP,
            price_type=PriceType.TRADE,
        )
        perp_mark = adapter.fetch_ohlcv_history(
            perp_symbol,
            args.timeframe,
            since_ms,
            until_ms,
            market_type=MarketType.LINEAR_PERP,
            price_type=PriceType.MARK,
        )
        index = adapter.fetch_ohlcv_history(
            perp_symbol,
            args.timeframe,
            since_ms,
            until_ms,
            market_type=MarketType.LINEAR_PERP,
            price_type=PriceType.INDEX,
        )
        spot_trade = _retag_symbol(
            adapter.fetch_ohlcv_history(
                spot_symbol,
                args.timeframe,
                since_ms,
                until_ms,
                market_type=MarketType.SPOT,
                price_type=PriceType.TRADE,
            ),
            perp_symbol,
        )

        funding[perp_symbol] = funding_rows
        ohlcv[StreamKey(perp_symbol, MarketType.SPOT, PriceType.TRADE)] = spot_trade
        ohlcv[StreamKey(perp_symbol, MarketType.LINEAR_PERP, PriceType.TRADE)] = perp_trade
        ohlcv[StreamKey(perp_symbol, MarketType.LINEAR_PERP, PriceType.MARK)] = perp_mark
        ohlcv[StreamKey(perp_symbol, MarketType.LINEAR_PERP, PriceType.INDEX)] = index
        row_counts[perp_symbol] = {
            StreamName.FUNDING_HISTORY: len(funding_rows),
            StreamName.SPOT_TRADE_OHLCV: len(spot_trade),
            StreamName.PERP_TRADE_OHLCV: len(perp_trade),
            StreamName.PERP_MARK_OHLCV: len(perp_mark),
            StreamName.INDEX_OHLCV: len(index),
        }

    for symbol, counts in row_counts.items():
        print(
            symbol,
            " ".join(f"{stream.value}={counts[stream]}" for stream in counts),
        )

    missing_required = [
        f"{symbol}:{stream.value}"
        for symbol, counts in row_counts.items()
        for stream in _REQUIRED_STREAMS
        if counts[stream] == 0
    ]
    if missing_required:
        print("missing required streams: " + ", ".join(missing_required), file=sys.stderr)
        sys.exit(1)

    scenario_dir = write_cache(
        args.out,
        args.scenario_id,
        venue=args.venue,
        timeframe=args.timeframe,
        funding=funding,
        ohlcv=ohlcv,
        source_quality={
            StreamName.FUNDING_HISTORY: SourceQuality.VENUE,
            StreamName.SPOT_TRADE_OHLCV: SourceQuality.VENUE,
            StreamName.PERP_TRADE_OHLCV: SourceQuality.VENUE,
            StreamName.PERP_MARK_OHLCV: SourceQuality.VENUE,
            StreamName.INDEX_OHLCV: SourceQuality.VENUE,
        },
        train_until_ms=train_until_ms,
    )
    print(f"wrote cache: {scenario_dir}")


if __name__ == "__main__":
    main()

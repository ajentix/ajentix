#!/usr/bin/env python3
"""Generate the deterministic Stage-1 structural fixture.

Regenerate the committed fixture from the repository root with:

    . .venv/bin/activate && python scripts/gen_stage1_structural_fixture.py

The generator is offline-only: it synthesizes a single BTC/USDT:USDT aq-cache-v1
scenario and writes manifest.json, funding.csv, and ohlcv.csv via data.cache.write_cache.
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

# Allow running from a checkout without installing (src layout).
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from ajentix_quant.adapters.base import (  # noqa: E402
    FundingRate,
    HistoricalCandle,
    MarketType,
    PriceType,
    SourceQuality,
    StreamKey,
    StreamName,
)
from ajentix_quant.data.cache import write_cache  # noqa: E402

SCENARIO_ID = "structural_v1"
SYMBOL = "BTC/USDT:USDT"
VENUE = "bybit"
TIMEFRAME = "8h"
START_MS = 1_704_067_200_000  # 2024-01-01T00:00:00Z
STEP_MS = 8 * 60 * 60 * 1000
N_INTERVALS = 42
NEGATIVE_WINDOW = (12, 13)
GAP_INDEX = 22
GAP_MULTIPLIER = 1.18
PARAM_FREEZE_HASH = "stage1-structural-v1"


def _funding_rate(index: int, *, include_negative_regime: bool) -> float:
    if include_negative_regime and index in NEGATIVE_WINDOW:
        return -0.00022 if index == NEGATIVE_WINDOW[0] else -0.00018
    return 0.00072 + (0.00003 if index % 4 == 0 else 0.0) + (
        0.00002 if index % 7 == 0 else 0.0
    )


def _trade_candle(
    *,
    timestamp_ms: int,
    market_type: MarketType,
    price_type: PriceType,
    open_: float,
    high: float,
    low: float,
    close: float,
    volume: float | None,
) -> HistoricalCandle:
    return HistoricalCandle(
        timestamp_ms=timestamp_ms,
        symbol=SYMBOL,
        venue=VENUE,
        market_type=market_type,
        price_type=price_type,
        timeframe=TIMEFRAME,
        open=open_,
        high=high,
        low=low,
        close=close,
        volume=volume,
    )


def build_fixture_rows(
    *,
    include_negative_regime: bool = True,
    include_gap: bool = True,
) -> tuple[
    dict[str, tuple[FundingRate, ...]],
    dict[StreamKey, tuple[HistoricalCandle, ...]],
    dict[StreamName, SourceQuality],
]:
    funding: list[FundingRate] = []
    spot: list[HistoricalCandle] = []
    perp_trade: list[HistoricalCandle] = []
    perp_mark: list[HistoricalCandle] = []
    previous_mark_close: float | None = None

    for index in range(N_INTERVALS):
        timestamp_ms = START_MS + index * STEP_MS
        funding.append(
            FundingRate(
                symbol=SYMBOL,
                rate=_funding_rate(index, include_negative_regime=include_negative_regime),
                interval_hours=8.0,
                timestamp=timestamp_ms,
            )
        )

        base = 30_000.0 + index * 18.0 + ((index % 6) - 2) * 9.0
        spot_close = round(base, 2)
        perp_close = round(spot_close * 1.00015, 2)
        mark_close = round(spot_close * 1.00010, 2)

        spot_open = spot[-1].close if spot else spot_close
        perp_open = perp_trade[-1].close if perp_trade else perp_close
        mark_open = perp_mark[-1].close if perp_mark else mark_close

        spot_high = round(max(spot_open, spot_close) * 1.002, 2)
        spot_low = round(min(spot_open, spot_close) * 0.998, 2)
        perp_high = round(max(perp_open, perp_close) * 1.002, 2)
        perp_low = round(min(perp_open, perp_close) * 0.998, 2)
        mark_high = round(max(mark_open, mark_close) * 1.002, 2)
        mark_low = round(min(mark_open, mark_close) * 0.998, 2)
        if include_gap and index == GAP_INDEX:
            mark_high = round((previous_mark_close or mark_close) * GAP_MULTIPLIER, 2)
            perp_high = max(perp_high, mark_high)

        volume_btc = 125.0 + (index % 5) * 3.0
        spot.append(
            _trade_candle(
                timestamp_ms=timestamp_ms,
                market_type=MarketType.SPOT,
                price_type=PriceType.TRADE,
                open_=spot_open,
                high=spot_high,
                low=spot_low,
                close=spot_close,
                volume=volume_btc,
            )
        )
        perp_trade.append(
            _trade_candle(
                timestamp_ms=timestamp_ms,
                market_type=MarketType.LINEAR_PERP,
                price_type=PriceType.TRADE,
                open_=perp_open,
                high=perp_high,
                low=perp_low,
                close=perp_close,
                volume=round(volume_btc * 1.4, 8),
            )
        )
        perp_mark.append(
            _trade_candle(
                timestamp_ms=timestamp_ms,
                market_type=MarketType.LINEAR_PERP,
                price_type=PriceType.MARK,
                open_=mark_open,
                high=mark_high,
                low=mark_low,
                close=mark_close,
                volume=None,
            )
        )
        previous_mark_close = mark_close

    return (
        {SYMBOL: tuple(funding)},
        {
            StreamKey(SYMBOL, MarketType.SPOT, PriceType.TRADE): tuple(spot),
            StreamKey(SYMBOL, MarketType.LINEAR_PERP, PriceType.TRADE): tuple(perp_trade),
            StreamKey(SYMBOL, MarketType.LINEAR_PERP, PriceType.MARK): tuple(perp_mark),
        },
        {
            StreamName.FUNDING_HISTORY: SourceQuality.FIXTURE,
            StreamName.SPOT_TRADE_OHLCV: SourceQuality.FIXTURE,
            StreamName.PERP_TRADE_OHLCV: SourceQuality.FIXTURE,
            StreamName.PERP_MARK_OHLCV: SourceQuality.FIXTURE,
        },
    )


def write_structural_fixture(
    cache_root: str | Path,
    scenario_id: str = SCENARIO_ID,
    *,
    include_negative_regime: bool = True,
    include_gap: bool = True,
) -> Path:
    funding, ohlcv, source_quality = build_fixture_rows(
        include_negative_regime=include_negative_regime,
        include_gap=include_gap,
    )
    return write_cache(
        cache_root,
        scenario_id,
        venue=VENUE,
        timeframe=TIMEFRAME,
        funding=funding,
        ohlcv=ohlcv,
        source_quality=source_quality,
        param_freeze_hash=PARAM_FREEZE_HASH,
    )


def _parser() -> argparse.ArgumentParser:
    repo_root = Path(__file__).resolve().parents[1]
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--cache-root",
        default=str(repo_root / "tests" / "fixtures" / "stage1"),
        help="cache root that will receive <scenario-id>/manifest.json + CSV files",
    )
    parser.add_argument("--scenario-id", default=SCENARIO_ID)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    scenario_dir = write_structural_fixture(args.cache_root, args.scenario_id)
    print(f"wrote fixture: {scenario_dir}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

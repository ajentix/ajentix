#!/usr/bin/env python3
"""Generate the deterministic Stage-1 Edge Verdict demo fixture.

Regenerate from the repository root with:

    . .venv/bin/activate && python scripts/gen_edge_demo_fixture.py

The fixture is intentionally source_quality=FIXTURE. It exercises the Edge Verdict
load/split/run/report wiring, but can never produce a GO.
"""

from __future__ import annotations

import argparse
import json
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

SCENARIO_ID = "edge_demo_v1"
GENERATOR_VERSION = "ajentix-quant/phase1-g008-edge-demo"
SYMBOL = "BTC/USDT:USDT"
VENUE = "bybit"
TIMEFRAME = "8h"
START_MS = 1_704_067_200_000  # 2024-01-01T00:00:00Z
STEP_MS = 8 * 60 * 60 * 1000
N_INTERVALS = 64
TRAIN_INTERVALS = 32
TRAIN_UNTIL_MS = START_MS + (TRAIN_INTERVALS - 1) * STEP_MS


def _funding_rate(index: int) -> float:
    if index < TRAIN_INTERVALS:
        return 0.00072 + (0.00005 if index % 5 == 0 else 0.0)
    if index in {42, 43}:
        return -0.00012
    return 0.00066 + (0.00004 if index % 7 == 0 else 0.0)


def _candle(
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


def build_fixture_rows() -> tuple[
    dict[str, tuple[FundingRate, ...]],
    dict[StreamKey, tuple[HistoricalCandle, ...]],
    dict[StreamName, SourceQuality],
]:
    funding: list[FundingRate] = []
    spot: list[HistoricalCandle] = []
    perp_trade: list[HistoricalCandle] = []
    perp_mark: list[HistoricalCandle] = []

    for index in range(N_INTERVALS):
        timestamp_ms = START_MS + index * STEP_MS
        funding.append(
            FundingRate(
                symbol=SYMBOL,
                rate=_funding_rate(index),
                interval_hours=8.0,
                timestamp=timestamp_ms,
            )
        )

        drift = index * 11.0
        wave = ((index % 8) - 3) * 6.5
        base = 30_000.0 + drift + wave
        spot_close = round(base, 2)
        perp_close = round(spot_close * (1.00012 + (0.00002 if index % 6 == 0 else 0.0)), 2)
        mark_close = round(spot_close * 1.00008, 2)

        spot_open = spot[-1].close if spot else spot_close
        perp_open = perp_trade[-1].close if perp_trade else perp_close
        mark_open = perp_mark[-1].close if perp_mark else mark_close

        spot_high = round(max(spot_open, spot_close) * 1.0015, 2)
        spot_low = round(min(spot_open, spot_close) * 0.9985, 2)
        perp_high = round(max(perp_open, perp_close) * 1.0015, 2)
        perp_low = round(min(perp_open, perp_close) * 0.9985, 2)
        mark_high = round(max(mark_open, mark_close) * 1.0015, 2)
        mark_low = round(min(mark_open, mark_close) * 0.9985, 2)
        volume_btc = 160.0 + (index % 6) * 4.0

        spot.append(
            _candle(
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
            _candle(
                timestamp_ms=timestamp_ms,
                market_type=MarketType.LINEAR_PERP,
                price_type=PriceType.TRADE,
                open_=perp_open,
                high=perp_high,
                low=perp_low,
                close=perp_close,
                volume=round(volume_btc * 1.35, 8),
            )
        )
        perp_mark.append(
            _candle(
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


def write_edge_demo_fixture(cache_root: str | Path, scenario_id: str = SCENARIO_ID) -> Path:
    funding, ohlcv, source_quality = build_fixture_rows()
    scenario_dir = write_cache(
        cache_root,
        scenario_id,
        venue=VENUE,
        timeframe=TIMEFRAME,
        funding=funding,
        ohlcv=ohlcv,
        source_quality=source_quality,
        train_until_ms=TRAIN_UNTIL_MS,
        param_freeze_hash=None,
    )
    manifest_path = scenario_dir / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["generator_version"] = GENERATOR_VERSION
    manifest_path.write_text(
        json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    return scenario_dir


def _parser() -> argparse.ArgumentParser:
    repo_root = Path(__file__).resolve().parents[1]
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--cache-root",
        default=str(repo_root / "tests" / "fixtures" / "edge"),
        help="cache root that will receive <scenario-id>/manifest.json + CSV files",
    )
    parser.add_argument("--scenario-id", default=SCENARIO_ID)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    scenario_dir = write_edge_demo_fixture(args.cache_root, args.scenario_id)
    print(f"wrote fixture: {scenario_dir}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

"""Venue adapters: uniform plumbing, venue-specific microstructure kept first-class."""

from .base import (
    REQUIRED_STREAM_MATRIX,
    Candle,
    FundingRate,
    FundingRateHistoryRequest,
    HistoricalCandle,
    MarketDataset,
    MarketType,
    MissingBehavior,
    PriceType,
    SourceQuality,
    StreamKey,
    StreamName,
    StreamSpec,
    VenueAdapter,
    stream_spec,
)

__all__ = [
    "Candle",
    "FundingRate",
    "FundingRateHistoryRequest",
    "HistoricalCandle",
    "MarketDataset",
    "MarketType",
    "MissingBehavior",
    "PriceType",
    "REQUIRED_STREAM_MATRIX",
    "SourceQuality",
    "StreamKey",
    "StreamName",
    "StreamSpec",
    "VenueAdapter",
    "stream_spec",
]

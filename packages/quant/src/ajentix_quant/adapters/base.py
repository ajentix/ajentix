"""Adapter contract.

We abstract only the *commodity plumbing* (connect/auth/fetch/order). Venue-specific
microstructure (funding interval, mechanics, price type, margin tiers) is surfaced as
typed data, never flattened, because that microstructure is itself a source of alpha.

Phase 1 adds historical read paths (funding-rate history, paginated OHLCV with an
explicit market type + price type) plus typed source-quality semantics that drive
fail-closed validation. Order placement still arrives in Phase 2.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from enum import StrEnum


class MarketType(StrEnum):
    """Which book a candle/stream comes from."""

    SPOT = "spot"
    LINEAR_PERP = "linear_perp"
    INVERSE_PERP = "inverse_perp"


class PriceType(StrEnum):
    """Which price series a perp candle represents."""

    TRADE = "trade"
    MARK = "mark"
    INDEX = "index"


class SourceQuality(StrEnum):
    """Provenance of a data stream; drives fail-closed validation downstream."""

    VENUE = "venue"  # fetched from a public venue endpoint and frozen in cache
    FROZEN_SNAPSHOT = "frozen_snapshot"  # curated from venue docs/risk-limit examples
    FIXTURE = "fixture"  # deterministic generated fixture data
    PROXY = "proxy"  # conservative substitute; can NEVER back a hard safety claim
    ABSENT = "absent"  # missing


# Source qualities that may back a hard safety / liquidation / leverage claim.
HARD_CLAIM_SOURCE_QUALITY: tuple[SourceQuality, ...] = (
    SourceQuality.VENUE,
    SourceQuality.FROZEN_SNAPSHOT,
    SourceQuality.FIXTURE,
)


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


@dataclass(frozen=True)
class HistoricalCandle:
    """A typed OHLCV row tagged with market + price type.

    ``volume`` is nullable: Bybit mark-price and index-price klines carry no volume, so
    slippage must never be driven by mark/index volume (see ``SlippageModel``).
    """

    timestamp_ms: int
    symbol: str
    venue: str
    market_type: MarketType
    price_type: PriceType
    timeframe: str
    open: float
    high: float
    low: float
    close: float
    volume: float | None


@dataclass(frozen=True)
class FundingRateHistoryRequest:
    symbol: str
    since_ms: int
    until_ms: int
    limit: int = 200  # Bybit v5 funding-history page cap


@dataclass(frozen=True)
class StreamKey:
    """Identifies one OHLCV stream within a dataset."""

    symbol: str
    market_type: MarketType
    price_type: PriceType


class StreamName(StrEnum):
    """Logical data streams a Stage-1 scenario can require."""

    FUNDING_HISTORY = "funding_history"
    SPOT_TRADE_OHLCV = "spot_trade_ohlcv"
    PERP_TRADE_OHLCV = "perp_trade_ohlcv"
    PERP_MARK_OHLCV = "perp_mark_ohlcv"
    INDEX_OHLCV = "index_ohlcv"
    INSTRUMENT_METADATA = "instrument_metadata"
    FEES = "fees"
    FUNDING_INTERVAL = "funding_interval"
    TICK_LOT_PRECISION = "tick_lot_precision"
    RISK_LIMITS = "risk_limits"
    MAINTENANCE_TIERS = "maintenance_tiers"


class MissingBehavior(StrEnum):
    FAIL_CLOSED = "fail_closed"
    DOWNGRADE = "downgrade"


@dataclass(frozen=True)
class StreamSpec:
    """One row of the required-stream matrix (plan §4)."""

    name: StreamName
    required_for_structural: bool
    allowed_source_quality: tuple[SourceQuality, ...]
    missing_behavior: MissingBehavior
    hard_claims: tuple[str, ...]  # claims invalidated / downgraded when missing
    notes: str = ""


def _spec(
    name: StreamName,
    *,
    required: bool,
    allowed: tuple[SourceQuality, ...],
    missing: MissingBehavior,
    claims: tuple[str, ...],
    notes: str = "",
) -> StreamSpec:
    return StreamSpec(name, required, allowed, missing, claims, notes)


_VENUE_OR_FIXTURE: tuple[SourceQuality, ...] = (SourceQuality.VENUE, SourceQuality.FIXTURE)
_INDEX_ALLOWED: tuple[SourceQuality, ...] = (
    SourceQuality.VENUE,
    SourceQuality.FROZEN_SNAPSHOT,
    SourceQuality.FIXTURE,
    SourceQuality.PROXY,
)

# Encodes the required-stream matrix. Consumed by the cache validator (fail-closed) and
# the gate / edge-verdict harnesses (claim downgrading). Mark + maintenance tiers are
# fail-closed for any no-liquidation / leverage claim; index missing only downgrades.
REQUIRED_STREAM_MATRIX: tuple[StreamSpec, ...] = (
    _spec(
        StreamName.FUNDING_HISTORY,
        required=True,
        allowed=_VENUE_OR_FIXTURE,
        missing=MissingBehavior.FAIL_CLOSED,
        claims=("funding_sign", "carry"),
    ),
    _spec(
        StreamName.SPOT_TRADE_OHLCV,
        required=True,
        allowed=_VENUE_OR_FIXTURE,
        missing=MissingBehavior.FAIL_CLOSED,
        claims=("replay", "net_delta"),
    ),
    _spec(
        StreamName.PERP_TRADE_OHLCV,
        required=True,
        allowed=_VENUE_OR_FIXTURE,
        missing=MissingBehavior.FAIL_CLOSED,
        claims=("fills", "slippage_volume", "account_equity"),
    ),
    _spec(
        StreamName.PERP_MARK_OHLCV,
        required=True,
        allowed=HARD_CLAIM_SOURCE_QUALITY,
        missing=MissingBehavior.FAIL_CLOSED,
        claims=("no_liquidation", "leverage_cap"),
        notes="proxy mark allowed for demo reports only, never a hard safety claim",
    ),
    _spec(
        StreamName.INDEX_OHLCV,
        required=False,
        allowed=_INDEX_ALLOWED,
        missing=MissingBehavior.DOWNGRADE,
        claims=("basis_quality",),
        notes="missing index downgrades basis/perf claims; spot-close proxy allowed",
    ),
    _spec(
        StreamName.INSTRUMENT_METADATA,
        required=True,
        allowed=HARD_CLAIM_SOURCE_QUALITY,
        missing=MissingBehavior.FAIL_CLOSED,
        claims=("precision", "sizing"),
    ),
    _spec(
        StreamName.FEES,
        required=True,
        allowed=HARD_CLAIM_SOURCE_QUALITY,
        missing=MissingBehavior.FAIL_CLOSED,
        claims=("net_cost",),
    ),
    _spec(
        StreamName.FUNDING_INTERVAL,
        required=True,
        allowed=HARD_CLAIM_SOURCE_QUALITY,
        missing=MissingBehavior.FAIL_CLOSED,
        claims=("settlement_ordering",),
    ),
    _spec(
        StreamName.TICK_LOT_PRECISION,
        required=True,
        allowed=HARD_CLAIM_SOURCE_QUALITY,
        missing=MissingBehavior.FAIL_CLOSED,
        claims=("order_sizing", "delta"),
    ),
    _spec(
        StreamName.RISK_LIMITS,
        required=True,
        allowed=HARD_CLAIM_SOURCE_QUALITY,
        missing=MissingBehavior.FAIL_CLOSED,
        claims=("leverage_cap",),
    ),
    _spec(
        StreamName.MAINTENANCE_TIERS,
        required=True,
        allowed=HARD_CLAIM_SOURCE_QUALITY,
        missing=MissingBehavior.FAIL_CLOSED,
        claims=("no_liquidation", "health_factor", "gap_survival"),
    ),
)


def stream_spec(name: StreamName) -> StreamSpec:
    """Return the matrix row for ``name`` (raises ``KeyError`` if undefined)."""
    for spec in REQUIRED_STREAM_MATRIX:
        if spec.name == name:
            return spec
    raise KeyError(name)


@dataclass(frozen=True)
class MarketDataset:
    """Deterministic in-memory container for one replay scenario.

    Populated by the cache loader (``data.cache`` / ``ReplayVenueAdapter``). Holds funding
    by symbol and OHLCV by ``StreamKey``, plus per-stream ``SourceQuality`` for fail-closed
    checks. Train/test boundary and the param-freeze hash support out-of-sample evaluation.
    """

    venue: str
    timeframe: str
    scenario_id: str
    symbols: tuple[str, ...]
    funding: dict[str, tuple[FundingRate, ...]]
    ohlcv: dict[StreamKey, tuple[HistoricalCandle, ...]]
    source_quality: dict[StreamName, SourceQuality]
    train_until_ms: int | None = None  # rows with ts <= this are TRAIN, the rest TEST
    param_freeze_hash: str | None = None


class VenueAdapter(ABC):
    """Read paths implemented across Phase 0/1; order placement arrives in Phase 2."""

    name: str

    @abstractmethod
    def fetch_funding_rate(self, symbol: str) -> FundingRate:
        """Current funding rate for a perpetual symbol."""

    @abstractmethod
    def fetch_ohlcv(self, symbol: str, timeframe: str = "1h", limit: int = 500) -> list[Candle]:
        """Recent OHLCV candles (trade price)."""

    @abstractmethod
    def fetch_funding_rate_history(
        self, request: FundingRateHistoryRequest
    ) -> list[FundingRate]:
        """Historical funding rates over ``[since_ms, until_ms]``, ascending by timestamp."""

    @abstractmethod
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
        """Historical OHLCV for an explicit market + price type, ascending by timestamp."""

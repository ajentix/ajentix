"""Phase 1 Sub-phase 1a: typed domain contracts + config."""

import inspect

from ajentix_quant.adapters.base import (
    HARD_CLAIM_SOURCE_QUALITY,
    REQUIRED_STREAM_MATRIX,
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
    VenueAdapter,
    stream_spec,
)
from ajentix_quant.config import Settings


def test_market_and_price_type_values():
    assert MarketType.SPOT.value == "spot"
    assert MarketType.LINEAR_PERP.value == "linear_perp"
    assert {p.value for p in PriceType} == {"trade", "mark", "index"}


def test_source_quality_hard_claim_set():
    # proxy and absent can NEVER back a hard safety claim
    assert SourceQuality.PROXY not in HARD_CLAIM_SOURCE_QUALITY
    assert SourceQuality.ABSENT not in HARD_CLAIM_SOURCE_QUALITY
    assert SourceQuality.VENUE in HARD_CLAIM_SOURCE_QUALITY
    assert SourceQuality.FROZEN_SNAPSHOT in HARD_CLAIM_SOURCE_QUALITY
    assert SourceQuality.FIXTURE in HARD_CLAIM_SOURCE_QUALITY


def test_historical_candle_volume_is_nullable():
    # mark/index klines carry no volume
    mark = HistoricalCandle(
        timestamp_ms=1,
        symbol="BTC/USDT:USDT",
        venue="bybit",
        market_type=MarketType.LINEAR_PERP,
        price_type=PriceType.MARK,
        timeframe="1h",
        open=1.0,
        high=1.0,
        low=1.0,
        close=1.0,
        volume=None,
    )
    assert mark.volume is None
    trade = HistoricalCandle(
        timestamp_ms=1,
        symbol="BTC/USDT:USDT",
        venue="bybit",
        market_type=MarketType.LINEAR_PERP,
        price_type=PriceType.TRADE,
        timeframe="1h",
        open=1.0,
        high=2.0,
        low=0.5,
        close=1.5,
        volume=123.0,
    )
    assert trade.volume == 123.0


def test_required_stream_matrix_covers_every_stream_name():
    covered = {spec.name for spec in REQUIRED_STREAM_MATRIX}
    assert covered == set(StreamName)


def test_mark_and_maintenance_tiers_fail_closed():
    for name in (StreamName.PERP_MARK_OHLCV, StreamName.MAINTENANCE_TIERS):
        spec = stream_spec(name)
        assert spec.required_for_structural is True
        assert spec.missing_behavior is MissingBehavior.FAIL_CLOSED
        # proxy can never satisfy these hard safety streams
        assert SourceQuality.PROXY not in spec.allowed_source_quality


def test_index_missing_only_downgrades_and_allows_proxy():
    spec = stream_spec(StreamName.INDEX_OHLCV)
    assert spec.required_for_structural is False
    assert spec.missing_behavior is MissingBehavior.DOWNGRADE
    assert SourceQuality.PROXY in spec.allowed_source_quality


def test_stream_spec_unknown_raises():
    try:
        stream_spec("nonexistent")  # type: ignore[arg-type]
    except KeyError:
        return
    raise AssertionError("stream_spec should raise KeyError for an unknown stream")


def test_market_dataset_construction_and_train_test_boundary():
    fr = FundingRate(symbol="BTC/USDT:USDT", rate=0.0001, interval_hours=8.0, timestamp=10)
    key = StreamKey("BTC/USDT:USDT", MarketType.LINEAR_PERP, PriceType.MARK)
    ds = MarketDataset(
        venue="bybit",
        timeframe="1h",
        scenario_id="fixture_v1",
        symbols=("BTC/USDT:USDT",),
        funding={"BTC/USDT:USDT": (fr,)},
        ohlcv={key: ()},
        source_quality={StreamName.FUNDING_HISTORY: SourceQuality.FIXTURE},
        train_until_ms=100,
        param_freeze_hash="deadbeef",
    )
    assert ds.symbols == ("BTC/USDT:USDT",)
    assert ds.funding["BTC/USDT:USDT"][0].rate == 0.0001
    assert ds.train_until_ms == 100
    assert ds.param_freeze_hash == "deadbeef"


def test_funding_rate_history_request_defaults():
    req = FundingRateHistoryRequest(symbol="ETH/USDT:USDT", since_ms=1, until_ms=2)
    assert req.limit == 200


def test_venue_adapter_requires_history_signatures():
    required = VenueAdapter.__abstractmethods__
    assert "fetch_funding_rate_history" in required
    assert "fetch_ohlcv_history" in required
    # explicit market_type + price_type are keyword-only on the history OHLCV path
    sig = inspect.signature(VenueAdapter.fetch_ohlcv_history)
    assert sig.parameters["market_type"].kind is inspect.Parameter.KEYWORD_ONLY
    assert sig.parameters["price_type"].kind is inspect.Parameter.KEYWORD_ONLY


def test_config_phase1_defaults():
    s = Settings()
    assert s.capital_usd_min <= s.default_capital_usd <= s.capital_usd_max
    assert s.timeframe == "1h"
    assert set(s.symbols) >= {"BTC/USDT:USDT", "ETH/USDT:USDT"}
    assert s.perp_taker_fee_bps > 0
    assert s.slippage_cap_bps >= s.slippage_base_bps
    assert s.gate_scenario_id and s.edge_verdict_scenario_id

def test_bybit_adapter_is_concrete_and_phase0_signature_preserved():
    # importing the class must not require ccxt (lazy import lives in __init__)
    from ajentix_quant.adapters.bybit import BybitAdapter

    # all abstract methods implemented -> concrete (instantiable once ccxt is present)
    assert BybitAdapter.__abstractmethods__ == frozenset()
    # Phase 0 default signature preserved
    sig = inspect.signature(BybitAdapter.fetch_ohlcv)
    assert sig.parameters["timeframe"].default == "1h"
    assert sig.parameters["limit"].default == 500

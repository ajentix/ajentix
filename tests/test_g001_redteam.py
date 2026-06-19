from __future__ import annotations

import dataclasses
import inspect
import os
from collections import Counter

import pytest

from ajentix_quant.adapters.base import (
    HARD_CLAIM_SOURCE_QUALITY,
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
from ajentix_quant.config import Settings


def _historical_candle() -> HistoricalCandle:
    return HistoricalCandle(
        timestamp_ms=1,
        symbol="BTC/USDT:USDT",
        venue="bybit",
        market_type=MarketType.LINEAR_PERP,
        price_type=PriceType.MARK,
        timeframe="1h",
        open=100.0,
        high=101.0,
        low=99.0,
        close=100.5,
        volume=None,
    )


def _market_dataset() -> MarketDataset:
    return MarketDataset(
        venue="bybit",
        timeframe="1h",
        scenario_id="redteam_fixture",
        symbols=("BTC/USDT:USDT",),
        funding={
            "BTC/USDT:USDT": (
                FundingRate(
                    symbol="BTC/USDT:USDT",
                    rate=0.0001,
                    interval_hours=8.0,
                    timestamp=1,
                ),
            )
        },
        ohlcv={
            StreamKey("BTC/USDT:USDT", MarketType.LINEAR_PERP, PriceType.MARK): (
                _historical_candle(),
            )
        },
        source_quality={StreamName.PERP_MARK_OHLCV: SourceQuality.FIXTURE},
        train_until_ms=1,
        param_freeze_hash="redteam",
    )


def test_required_stream_matrix_has_exactly_one_row_per_stream_name() -> None:
    names = [spec.name for spec in REQUIRED_STREAM_MATRIX]
    counts = Counter(names)

    assert all(isinstance(name, StreamName) for name in names)
    assert counts == Counter({name: 1 for name in StreamName})
    assert len(REQUIRED_STREAM_MATRIX) == len(StreamName)


def test_no_stream_spec_allows_absent_source_quality() -> None:
    allowed_without_absent = set(SourceQuality) - {SourceQuality.ABSENT}

    for spec in REQUIRED_STREAM_MATRIX:
        assert spec.allowed_source_quality, spec.name
        assert all(isinstance(quality, SourceQuality) for quality in spec.allowed_source_quality)
        assert set(spec.allowed_source_quality) <= allowed_without_absent, spec.name


def test_proxy_is_confined_to_non_structural_downgrade_streams() -> None:
    proxy_specs = [
        spec
        for spec in REQUIRED_STREAM_MATRIX
        if SourceQuality.PROXY in spec.allowed_source_quality
    ]

    assert [spec.name for spec in proxy_specs] == [StreamName.INDEX_OHLCV]
    for spec in proxy_specs:
        assert spec.required_for_structural is False
        assert spec.missing_behavior is MissingBehavior.DOWNGRADE

    for hard_stream in (StreamName.PERP_MARK_OHLCV, StreamName.MAINTENANCE_TIERS):
        hard_spec = stream_spec(hard_stream)
        assert hard_spec.required_for_structural is True
        assert hard_spec.missing_behavior is MissingBehavior.FAIL_CLOSED
        assert SourceQuality.PROXY not in hard_spec.allowed_source_quality


@pytest.mark.parametrize(
    ("frozen_obj", "field", "replacement"),
    [
        (_historical_candle(), "close", 0.0),
        (_market_dataset(), "scenario_id", "mutated"),
        (
            StreamSpec(
                name=StreamName.FEES,
                required_for_structural=True,
                allowed_source_quality=(SourceQuality.VENUE,),
                missing_behavior=MissingBehavior.FAIL_CLOSED,
                hard_claims=("net_cost",),
            ),
            "notes",
            "mutated",
        ),
    ],
)
def test_domain_dataclasses_are_shallow_frozen(
    frozen_obj: object, field: str, replacement: object
) -> None:
    with pytest.raises(dataclasses.FrozenInstanceError):
        setattr(frozen_obj, field, replacement)


@pytest.mark.parametrize(
    "garbage_stream_name",
    ["", "perp-mark-ohlcv", "spot", SourceQuality.ABSENT, object()],
)
def test_stream_spec_rejects_unknown_garbage_values(garbage_stream_name: object) -> None:
    with pytest.raises(KeyError) as exc_info:
        stream_spec(garbage_stream_name)  # type: ignore[arg-type]

    assert exc_info.value.args == (garbage_stream_name,)


def test_venue_adapter_base_class_remains_abstract() -> None:
    assert {"fetch_funding_rate_history", "fetch_ohlcv_history"} <= VenueAdapter.__abstractmethods__

    with pytest.raises(TypeError, match="abstract"):
        VenueAdapter()


def test_phase0_only_adapter_is_still_abstract_without_history_methods() -> None:
    class Phase0OnlyAdapter(VenueAdapter):
        name = "phase0-only"

        def fetch_funding_rate(self, symbol: str) -> FundingRate:
            return FundingRate(symbol=symbol, rate=0.0, interval_hours=8.0, timestamp=0)

        def fetch_ohlcv(
            self, symbol: str, timeframe: str = "1h", limit: int = 500
        ) -> list[Candle]:
            return [Candle(timestamp=0, open=1.0, high=1.0, low=1.0, close=1.0, volume=0.0)][
                :limit
            ]

    expected_abstract = {"fetch_funding_rate_history", "fetch_ohlcv_history"}
    assert expected_abstract <= Phase0OnlyAdapter.__abstractmethods__
    with pytest.raises(TypeError, match="abstract"):
        Phase0OnlyAdapter()


def test_complete_adapter_instantiates_and_history_price_selectors_are_keyword_only() -> None:
    class CompleteAdapter(VenueAdapter):
        name = "complete"

        def fetch_funding_rate(self, symbol: str) -> FundingRate:
            return FundingRate(symbol=symbol, rate=0.0001, interval_hours=8.0, timestamp=1)

        def fetch_ohlcv(
            self, symbol: str, timeframe: str = "1h", limit: int = 500
        ) -> list[Candle]:
            return [Candle(timestamp=1, open=1.0, high=2.0, low=0.5, close=1.5, volume=10.0)][
                :limit
            ]

        def fetch_funding_rate_history(
            self, request: FundingRateHistoryRequest
        ) -> list[FundingRate]:
            return [
                FundingRate(
                    symbol=request.symbol,
                    rate=0.0001,
                    interval_hours=8.0,
                    timestamp=request.since_ms,
                )
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
            return [
                HistoricalCandle(
                    timestamp_ms=since_ms,
                    symbol=symbol,
                    venue=self.name,
                    market_type=market_type,
                    price_type=price_type,
                    timeframe=timeframe,
                    open=1.0,
                    high=2.0,
                    low=0.5,
                    close=1.5,
                    volume=None if price_type in (PriceType.MARK, PriceType.INDEX) else 10.0,
                )
            ]

    base_signature = inspect.signature(VenueAdapter.fetch_ohlcv_history)
    assert base_signature.parameters["market_type"].kind is inspect.Parameter.KEYWORD_ONLY
    assert base_signature.parameters["price_type"].kind is inspect.Parameter.KEYWORD_ONLY

    adapter = CompleteAdapter()
    assert CompleteAdapter.__abstractmethods__ == frozenset()

    with pytest.raises(TypeError):
        adapter.fetch_ohlcv_history(
            "BTC/USDT:USDT",
            "1h",
            1,
            2,
            MarketType.LINEAR_PERP,  # type: ignore[misc]
            PriceType.MARK,
        )

    rows = adapter.fetch_ohlcv_history(
        "BTC/USDT:USDT",
        "1h",
        1,
        2,
        market_type=MarketType.LINEAR_PERP,
        price_type=PriceType.MARK,
    )
    assert rows[0].market_type is MarketType.LINEAR_PERP
    assert rows[0].price_type is PriceType.MARK
    assert rows[0].volume is None


def test_settings_defaults_respect_boundary_invariants(monkeypatch: pytest.MonkeyPatch) -> None:
    for key in tuple(os.environ):
        if key.upper().startswith("AQ_"):
            monkeypatch.delenv(key, raising=False)

    settings = Settings(_env_file=None)

    assert settings.capital_usd_min <= settings.default_capital_usd <= settings.capital_usd_max
    assert settings.capital_usd_min > 0
    assert settings.capital_usd_max > 0

    fee_fields = ("perp_taker_fee_bps", "perp_maker_fee_bps", "spot_taker_fee_bps")
    for field in fee_fields:
        assert getattr(settings, field) > 0, field

    assert settings.slippage_cap_bps >= settings.slippage_base_bps >= 0
    assert settings.gate_scenario_id.strip()
    assert settings.edge_verdict_scenario_id.strip()


def test_hard_claim_source_quality_never_contains_proxy_or_absent() -> None:
    assert SourceQuality.PROXY not in HARD_CLAIM_SOURCE_QUALITY
    assert SourceQuality.ABSENT not in HARD_CLAIM_SOURCE_QUALITY
    assert all(isinstance(quality, SourceQuality) for quality in HARD_CLAIM_SOURCE_QUALITY)


def test_str_enum_members_compare_equal_to_wire_strings() -> None:
    assert isinstance(MarketType.SPOT, str)
    assert MarketType.SPOT == "spot"
    assert PriceType.MARK == "mark"
    assert SourceQuality.VENUE == "venue"
    assert StreamName.INDEX_OHLCV == "index_ohlcv"
    assert MissingBehavior.FAIL_CLOSED == "fail_closed"

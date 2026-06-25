from __future__ import annotations

import pytest

from ajentix_quant.options import (
    OptionChainSnapshot,
    OptionLeg,
    OptionType,
    SourceQuality,
)
from ajentix_quant.options.usd_projection import (
    EVAL_FEE_CURRENCY,
    EVAL_PREMIUM_CURRENCY,
    USD_PROJECTION_SOURCE,
    eth_usd_rate,
    project_leg_to_usd,
    project_snapshot_to_usd,
)

SNAPSHOT_MS = 1_700_000_000_000
EXPIRY_30D_MS = SNAPSHOT_MS + 30 * 86_400_000
RATE = 3000.0


def _leg(
    name: str,
    option_type: OptionType,
    strike: float,
    *,
    bid: float,
    ask: float,
) -> OptionLeg:
    return OptionLeg(
        instrument_name=name,
        underlying="ETH",
        contract_multiplier=1.0,
        option_type=option_type,
        side="short",
        strike=strike,
        expiry_ms=EXPIRY_30D_MS,
        settlement_style="european",
        settlement_index="deribit_eth_index",
        premium_currency="ETH",
        fee_currency="ETH",
        collateral_currency="USDC_or_ETH",
        usd_conversion_source="deribit_eth_index",
        quote_ts_ms=SNAPSHOT_MS,
        quote_age_s=2.0,
        bid_price=bid,
        bid_amount=5.0,
        bid_iv=0.55,
        ask_price=ask,
        ask_amount=5.0,
        ask_iv=0.56,
        mark_price=(bid + ask) / 2.0,
        greek_provenance_key="vendor_cached_hashed_preferred_else_local",
        min_tick=1e-06,
        min_lot=1.0,
        source_quality=SourceQuality.FIXTURE,
    )


def _snapshot(
    *,
    index_price: float | None = RATE,
    conversion_inputs: dict | None = None,
) -> OptionChainSnapshot:
    legs = (
        _leg("ETH-30D-2800-P", OptionType.PUT, 2800.0, bid=0.005, ask=0.006),
        _leg("ETH-30D-2900-P", OptionType.PUT, 2900.0, bid=0.018, ask=0.020),
    )
    return OptionChainSnapshot(
        underlying="ETH",
        exchange="deribit",
        snapshot_ts_ms=SNAPSHOT_MS,
        source_ts_ms=SNAPSHOT_MS,
        source_id="fixture",
        scenario_id="deribit_history_eth_vrp_free_v1",
        settlement_index_price=index_price,
        index_price=index_price,
        usd_conversion_inputs=conversion_inputs if conversion_inputs is not None else {},
        legs=legs,
        source_quality_map={"chain": SourceQuality.FIXTURE},
        schema_version="aq-options-cache-v1",
        manifest_sha256="f" * 64,
    )


def test_eth_usd_rate_prefers_conversion_input_then_index() -> None:
    assert eth_usd_rate(_snapshot(conversion_inputs={"ETH_USD": 2640.55})) == 2640.55
    assert eth_usd_rate(_snapshot(index_price=3100.0)) == 3100.0
    assert eth_usd_rate(_snapshot(index_price=None)) is None


def test_project_leg_scales_premia_and_relabels_currency() -> None:
    leg = _leg("ETH-30D-2800-P", OptionType.PUT, 2800.0, bid=0.005, ask=0.006)
    usd = project_leg_to_usd(leg, rate=RATE)

    assert usd.bid_price == pytest.approx(0.005 * RATE)
    assert usd.ask_price == pytest.approx(0.006 * RATE)
    assert usd.mark_price == pytest.approx(((0.005 + 0.006) / 2.0) * RATE)
    assert usd.min_tick == pytest.approx(1e-06 * RATE)
    assert usd.premium_currency == EVAL_PREMIUM_CURRENCY
    assert usd.fee_currency == EVAL_FEE_CURRENCY
    assert usd.usd_conversion_source == USD_PROJECTION_SOURCE
    # Non-premium fields are untouched so delta selection is identical to the ETH run.
    assert usd.strike == leg.strike
    assert usd.bid_iv == leg.bid_iv
    assert usd.ask_iv == leg.ask_iv
    assert usd.expiry_ms == leg.expiry_ms
    assert usd.min_lot == 1.0  # normalized for per-contract measurement economics
    assert usd.contract_multiplier == leg.contract_multiplier


def test_project_leg_rejects_nonpositive_rate() -> None:
    leg = _leg("ETH-30D-2800-P", OptionType.PUT, 2800.0, bid=0.005, ask=0.006)
    for bad in (0.0, -1.0, float("inf"), float("nan")):
        with pytest.raises(ValueError):
            project_leg_to_usd(leg, rate=bad)


def test_project_snapshot_marks_inputs_and_projects_every_leg() -> None:
    snap = _snapshot(conversion_inputs={"ETH_USD": RATE})
    projected = project_snapshot_to_usd(snap)

    assert projected is not None
    assert all(leg.premium_currency == EVAL_PREMIUM_CURRENCY for leg in projected.legs)
    assert projected.usd_conversion_inputs["usd_projection"] == USD_PROJECTION_SOURCE
    assert projected.usd_conversion_inputs["usd_projection_rate"] == RATE
    # Original snapshot is unmutated.
    assert all(leg.premium_currency == "ETH" for leg in snap.legs)


def test_project_snapshot_fails_closed_without_rate() -> None:
    assert project_snapshot_to_usd(_snapshot(index_price=None)) is None


def test_credit_to_width_becomes_dimensionally_consistent() -> None:
    """ETH credit / USD width is ~rate too small; USD credit / USD width is realistic."""
    snap = _snapshot(conversion_inputs={"ETH_USD": RATE})
    short_eth = snap.legs[1]  # 2900 put, bid 0.018 ETH
    long_eth = snap.legs[0]  # 2800 put, ask 0.006 ETH
    width = abs(short_eth.strike - long_eth.strike)  # 100 USD

    eth_ratio = (short_eth.bid_price - long_eth.ask_price) / width
    projected = project_snapshot_to_usd(snap)
    assert projected is not None
    short_usd = projected.legs[1]
    long_usd = projected.legs[0]
    usd_ratio = (short_usd.bid_price - long_usd.ask_price) / width

    assert usd_ratio == pytest.approx(eth_ratio * RATE)
    # ETH ratio is far below any sane entry bar; USD ratio is in a plausible 0.1-0.5 range.
    assert eth_ratio < 1e-3
    assert 0.1 <= usd_ratio <= 0.5

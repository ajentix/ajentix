from __future__ import annotations

import math

import pytest

from ajentix_quant.options import OptionLeg, OptionType, Side, SourceQuality
from ajentix_quant.options.valuation import (
    DAY_COUNT,
    LOCAL_GREEKS_ROLE,
    TIMESTAMP_CONVENTION,
    black_scholes_value_greeks,
    diagnostic_value_greeks_from_leg,
    nearest_by_abs_then_value,
    year_fraction_act_365,
)

SNAPSHOT_MS = 1_700_000_000_000
EXPIRY_MS = SNAPSHOT_MS + 365 * 86_400_000


def _leg() -> OptionLeg:
    return OptionLeg(
        instrument_name="ETH-20241227-3000-C",
        underlying="ETH",
        contract_multiplier=1.0,
        option_type=OptionType.CALL,
        side=Side.LONG,
        strike=3000.0,
        expiry_ms=EXPIRY_MS,
        settlement_style="european",
        settlement_index="deribit_eth_index",
        premium_currency="ETH",
        fee_currency="ETH",
        collateral_currency="USDC_or_ETH",
        usd_conversion_source="deribit_eth_index",
        quote_ts_ms=SNAPSHOT_MS,
        quote_age_s=1.0,
        bid_price=120.0,
        bid_amount=3.0,
        bid_iv=0.50,
        ask_price=125.0,
        ask_amount=3.0,
        ask_iv=0.52,
        mark_price=122.0,
        greek_provenance_key="vendor_cached_hashed_preferred_else_local",
        min_tick=0.05,
        min_lot=1.0,
        source_quality=SourceQuality.VENUE,
    )


def test_black_scholes_greeks_are_finite_with_frozen_provenance():
    greeks = black_scholes_value_greeks(
        option_type=OptionType.CALL,
        spot=3000.0,
        strike=3100.0,
        time_to_expiry_years=30.0 / 365.0,
        volatility=0.65,
    )

    for value in (greeks.value, greeks.delta, greeks.gamma, greeks.vega, greeks.theta):
        assert math.isfinite(value)
    assert greeks.day_count == DAY_COUNT == "act/365"
    assert greeks.timestamp_convention == TIMESTAMP_CONVENTION == "utc_snapshot"
    assert greeks.risk_free_rate == 0.0
    assert greeks.dividend_yield == 0.0
    assert greeks.role == LOCAL_GREEKS_ROLE == "diagnostic_only"


def test_value_is_monotonic_in_volatility_and_time():
    low_vol = black_scholes_value_greeks(
        option_type="call",
        spot=3000.0,
        strike=3000.0,
        time_to_expiry_years=30.0 / 365.0,
        volatility=0.35,
    )
    high_vol = black_scholes_value_greeks(
        option_type="call",
        spot=3000.0,
        strike=3000.0,
        time_to_expiry_years=30.0 / 365.0,
        volatility=0.70,
    )
    longer = black_scholes_value_greeks(
        option_type="call",
        spot=3000.0,
        strike=3000.0,
        time_to_expiry_years=45.0 / 365.0,
        volatility=0.35,
    )

    assert high_vol.value > low_vol.value
    assert longer.value > low_vol.value
    assert high_vol.vega > 0.0


def test_put_call_parity_holds_with_zero_rate_and_dividend():
    call = black_scholes_value_greeks(
        option_type="call",
        spot=3000.0,
        strike=2900.0,
        time_to_expiry_years=45.0 / 365.0,
        volatility=0.60,
    )
    put = black_scholes_value_greeks(
        option_type="put",
        spot=3000.0,
        strike=2900.0,
        time_to_expiry_years=45.0 / 365.0,
        volatility=0.60,
    )

    assert call.value - put.value == pytest.approx(100.0, abs=1e-9)


def test_act_365_day_count_and_tie_breakers_are_deterministic():
    assert year_fraction_act_365(snapshot_ts_ms=SNAPSHOT_MS, expiry_ms=EXPIRY_MS) == 1.0
    assert nearest_by_abs_then_value([45, 21, 39], 30.0) == 21
    assert nearest_by_abs_then_value([2900.0, 3100.0], 3000.0) == 2900.0


def test_local_greeks_are_diagnostic_not_fill_prices():
    greeks = diagnostic_value_greeks_from_leg(
        _leg(),
        snapshot_ts_ms=SNAPSHOT_MS,
        underlying_price=3000.0,
    )

    assert greeks.role == "diagnostic_only"
    assert not hasattr(greeks, "fill_price")
    assert not hasattr(greeks, "bid_ask_fill")

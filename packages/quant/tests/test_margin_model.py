import math

import pytest

from ajentix_quant.risk.margin import (
    VenueMarginModel,
    bybit_btc_eth_instruments,
    bybit_btc_eth_risk_limits,
)


def btc_model() -> VenueMarginModel:
    instruments = bybit_btc_eth_instruments()
    limits = bybit_btc_eth_risk_limits()
    return VenueMarginModel(instruments["BTC/USDT:USDT"], limits["BTC/USDT:USDT"])


def test_tier_for_boundaries_and_maintenance_margin():
    limits = bybit_btc_eth_risk_limits()["BTC/USDT:USDT"]

    assert limits.tier_for(999_999.99).maintenance_margin_rate == pytest.approx(0.005)
    assert limits.tier_for(1_000_000.0).maintenance_margin_rate == pytest.approx(0.010)
    assert limits.tier_for(2_000_000.0).maintenance_margin_rate == pytest.approx(0.020)
    assert limits.maintenance_margin(1_500_000.0) == pytest.approx(10_000.0)
    assert limits.maintenance_margin(2_000_000.0) == pytest.approx(15_000.0)

    with pytest.raises(ValueError):
        limits.tier_for(math.inf)


def test_health_factor_safe_vs_breach():
    model = btc_model()

    safe = model.health_factor(entry=100.0, mark=100.0, qty=1.0, wallet_equity=10.0)
    breach = model.health_factor(entry=100.0, mark=120.0, qty=1.0, wallet_equity=10.0)

    assert safe > 1.5
    assert breach < 1.0


def test_liquidation_mark_and_distance_pct_for_short():
    model = btc_model()

    liquidation = model.liquidation_mark(entry=100.0, qty=1.0, wallet_equity=10.0)
    assert liquidation == pytest.approx(110.0 / 1.005)
    assert model.liquidation_distance_pct(
        entry=100.0,
        mark=100.0,
        qty=1.0,
        wallet_equity=10.0,
    ) == pytest.approx((liquidation - 100.0) / 100.0)
    assert model.liquidation_distance_pct(
        entry=100.0,
        mark=120.0,
        qty=1.0,
        wallet_equity=10.0,
    ) == 0.0


def test_survives_gap_at_15_pct_but_not_20_pct():
    model = btc_model()

    assert model.survives_gap(
        entry=100.0,
        mark=100.0,
        qty=1.0,
        wallet_equity=20.0,
        gap_pct=0.15,
        health_factor_floor=1.5,
    )
    assert not model.survives_gap(
        entry=100.0,
        mark=100.0,
        qty=1.0,
        wallet_equity=20.0,
        gap_pct=0.20,
        health_factor_floor=1.5,
    )


def test_gap_survival_leverage_cap_is_hard_capped_and_survives_documented_gap():
    model = btc_model()
    reserve_pct = 0.25
    max_gap_pct = 0.20
    health_factor_floor = 1.5

    cap = model.gap_survival_leverage_cap(
        max_gap_pct=max_gap_pct,
        health_factor_floor=health_factor_floor,
        reserve_pct=reserve_pct,
    )

    assert 1.0 <= cap <= 5.0
    total_equity = 1_000.0
    entry = 100.0
    qty = (cap * total_equity) / entry
    wallet_equity = (1.0 - reserve_pct) * total_equity
    assert model.survives_gap(
        entry=entry,
        mark=entry,
        qty=qty,
        wallet_equity=wallet_equity,
        gap_pct=max_gap_pct,
        health_factor_floor=health_factor_floor,
    )


def test_round_qty_and_meets_min_notional():
    instrument = bybit_btc_eth_instruments()["BTC/USDT:USDT"]

    assert instrument.round_qty(0.1239) == pytest.approx(0.123)
    assert instrument.round_qty(-1.0) == 0.0
    assert instrument.meets_min_notional(5.0)
    assert not instrument.meets_min_notional(4.999)


def test_gap_cap_returns_below_one_when_no_leverage_survives():
    # extreme gap (100%) at the HF floor -> no leverage >= 1x is safe; cap must NOT be
    # floored up to 1.0 (that would falsely imply a 1x position survives)
    model = btc_model()
    cap = model.gap_survival_leverage_cap(
        max_gap_pct=1.0, health_factor_floor=1.5, reserve_pct=0.25
    )
    assert cap < 1.0
    # the no-entry invariant: a 1x position does NOT survive the 100% gap at the HF floor
    equity = 1_000.0
    assert not model.survives_gap(
        entry=1.0, mark=1.0, qty=1.0 * equity, wallet_equity=(1.0 - 0.25) * equity,
        gap_pct=1.0, health_factor_floor=1.5,
    )


def test_gap_cap_is_tier_aware_and_conservative_when_equity_crosses_tiers():
    model = btc_model()
    cap_lowest = model.gap_survival_leverage_cap(
        max_gap_pct=0.20, health_factor_floor=1.5, reserve_pct=0.25
    )
    # Two equities: 300k (entry notional already tier 2) and 250k (entry notional in tier 1
    # but the SHOCKED notional crosses into tier 2 -> the entry-notional bug would over-cap).
    for equity in (300_000.0, 250_000.0):
        cap_aware = model.gap_survival_leverage_cap(
            max_gap_pct=0.20, health_factor_floor=1.5, reserve_pct=0.25, equity=equity
        )
        assert cap_aware < cap_lowest  # tier-aware cap is tighter (higher-tier MMR)
        # a position sized at the tier-aware cap survives the 20% gap at the REAL (shocked) tier
        assert model.survives_gap(
            entry=1.0, mark=1.0, qty=cap_aware * equity, wallet_equity=0.75 * equity,
            gap_pct=0.20, health_factor_floor=1.5,
        )


def test_gap_cap_rejects_full_reserve():
    model = btc_model()
    with pytest.raises(ValueError, match="reserve_pct must be less than 1"):
        model.gap_survival_leverage_cap(
            max_gap_pct=0.20, health_factor_floor=1.5, reserve_pct=1.0
        )


def test_negative_wallet_yields_sub_one_health_and_no_raise():
    # underwater account (cash + spot_value < 0 -> negative margin wallet) must produce a
    # health factor < 1 (liquidation) rather than raising, so the engine liquidates instead
    # of crashing.
    model = btc_model()
    hf = model.health_factor(entry=100.0, mark=100.0, qty=1.0, wallet_equity=-5.0)
    assert hf < 1.0
    # liquidation_mark must not raise on a negative wallet
    liq = model.liquidation_mark(entry=100.0, qty=1.0, wallet_equity=-5.0)
    assert liq == liq  # finite or inf, never an exception

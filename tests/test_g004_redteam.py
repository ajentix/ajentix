import math

import pytest

from ajentix_quant.risk.engine import NullADLProvider, RiskEngine, RiskParams
from ajentix_quant.risk.margin import (
    MaintenanceTier,
    RiskLimits,
    VenueMarginModel,
    bybit_btc_eth_instruments,
    bybit_btc_eth_risk_limits,
)


def _model(symbol: str = "BTC/USDT:USDT") -> VenueMarginModel:
    instruments = bybit_btc_eth_instruments()
    limits = bybit_btc_eth_risk_limits()
    return VenueMarginModel(instruments[symbol], limits[symbol])


def _first_tier_mmr(model: VenueMarginModel) -> float:
    return model.limits.tiers[0].maintenance_margin_rate


def _gap_cap_position(
    *, leverage: float, reserve_pct: float, total_equity: float = 1_000.0, entry: float = 100.0
) -> tuple[float, float, float]:
    qty = leverage * total_equity / entry
    wallet_equity = (1.0 - reserve_pct) * total_equity
    return entry, qty, wallet_equity


def _clean_deleverage_inputs() -> dict[str, float | int | None]:
    return {
        "realized_vol_annual": 0.5,
        "funding_rate_8h": 0.0001,
        "health_factor": 2.0,
        "hours_negative": 0.0,
        "drawdown_pct": 0.0,
        "adl_rank": None,
        "net_delta_frac": 0.0,
    }


def test_short_health_factor_strictly_decreases_as_mark_rises() -> None:
    model = _model()
    marks = [80.0, 90.0, 100.0, 105.0, 110.0, 125.0, 150.0]

    health_factors = [
        model.health_factor(entry=100.0, mark=mark, qty=3.0, wallet_equity=500.0)
        for mark in marks
    ]

    assert all(
        earlier > later for earlier, later in zip(health_factors, health_factors[1:], strict=False)
    )


@pytest.mark.parametrize(
    ("symbol", "reserve_pct", "gap_pct", "mmr"),
    [
        ("BTC/USDT:USDT", 0.25, 0.20, 0.005),
        ("BTC/USDT:USDT", 0.10, 0.25, 0.005),
        ("ETH/USDT:USDT", 0.35, 0.15, 0.006),
        ("ETH/USDT:USDT", 0.10, 0.30, 0.006),
    ],
)
def test_gap_survival_cap_is_consistent_with_survives_gap_and_tight(
    symbol: str, reserve_pct: float, gap_pct: float, mmr: float
) -> None:
    model = _model(symbol)
    health_factor_floor = 1.5
    assert _first_tier_mmr(model) == pytest.approx(mmr)

    cap = model.gap_survival_leverage_cap(
        max_gap_pct=gap_pct,
        health_factor_floor=health_factor_floor,
        reserve_pct=reserve_pct,
        mmr=mmr,
    )
    assert 1.0 < cap < 5.0

    entry, qty, wallet_equity = _gap_cap_position(
        leverage=cap,
        reserve_pct=reserve_pct,
    )
    shocked_mark = entry * (1.0 + gap_pct)
    health_at_cap = model.health_factor(
        entry=entry,
        mark=shocked_mark,
        qty=qty,
        wallet_equity=wallet_equity,
    )

    assert model.survives_gap(
        entry=entry,
        mark=entry,
        qty=qty,
        wallet_equity=wallet_equity,
        gap_pct=gap_pct,
        health_factor_floor=health_factor_floor,
    )
    assert health_at_cap == pytest.approx(health_factor_floor, abs=1e-9)

    too_high = cap + 1e-4
    _, too_high_qty, _ = _gap_cap_position(
        leverage=too_high,
        reserve_pct=reserve_pct,
    )
    health_above_cap = model.health_factor(
        entry=entry,
        mark=shocked_mark,
        qty=too_high_qty,
        wallet_equity=wallet_equity,
    )

    assert health_above_cap < health_factor_floor
    assert not model.survives_gap(
        entry=entry,
        mark=entry,
        qty=too_high_qty,
        wallet_equity=wallet_equity,
        gap_pct=gap_pct,
        health_factor_floor=health_factor_floor,
    )


def test_gap_survival_cap_is_bounded_and_monotonic() -> None:
    model = _model()
    health_factor_floor = 1.5

    gap_caps = [
        model.gap_survival_leverage_cap(
            max_gap_pct=gap_pct,
            health_factor_floor=health_factor_floor,
            reserve_pct=0.25,
            mmr=0.005,
        )
        for gap_pct in [0.05, 0.10, 0.20, 0.30, 0.50]
    ]
    mmr_caps = [
        model.gap_survival_leverage_cap(
            max_gap_pct=0.20,
            health_factor_floor=health_factor_floor,
            reserve_pct=0.25,
            mmr=mmr,
        )
        for mmr in [0.001, 0.005, 0.010, 0.020, 0.050]
    ]
    reserve_caps = [
        model.gap_survival_leverage_cap(
            max_gap_pct=0.20,
            health_factor_floor=health_factor_floor,
            reserve_pct=reserve_pct,
            mmr=0.005,
        )
        for reserve_pct in [0.0, 0.10, 0.25, 0.40, 0.70]
    ]

    for cap in [*gap_caps, *mmr_caps, *reserve_caps]:
        assert 1.0 <= cap <= 5.0
    assert all(earlier >= later for earlier, later in zip(gap_caps, gap_caps[1:], strict=False))
    assert all(earlier >= later for earlier, later in zip(mmr_caps, mmr_caps[1:], strict=False))
    assert all(
        earlier >= later for earlier, later in zip(reserve_caps, reserve_caps[1:], strict=False)
    )


def test_liquidation_mark_crosses_health_factor_one_for_safe_short() -> None:
    model = _model()
    entry = 100.0
    mark = 100.0
    qty = 1.0
    wallet_equity = 10.0

    liquidation_mark = model.liquidation_mark(
        entry=entry,
        qty=qty,
        wallet_equity=wallet_equity,
    )

    assert liquidation_mark > mark
    assert model.health_factor(
        entry=entry,
        mark=liquidation_mark,
        qty=qty,
        wallet_equity=wallet_equity,
    ) == pytest.approx(1.0, abs=1e-12)
    assert (
        model.health_factor(
            entry=entry,
            mark=liquidation_mark * 1.0001,
            qty=qty,
            wallet_equity=wallet_equity,
        )
        < 1.0
    )


def test_liquidation_distance_is_non_negative_and_zero_once_past_liquidation() -> None:
    model = _model()
    entry = 100.0
    qty = 1.0
    wallet_equity = 10.0
    liquidation_mark = model.liquidation_mark(
        entry=entry,
        qty=qty,
        wallet_equity=wallet_equity,
    )

    safe_distance = model.liquidation_distance_pct(
        entry=entry,
        mark=entry,
        qty=qty,
        wallet_equity=wallet_equity,
    )
    past_distance = model.liquidation_distance_pct(
        entry=entry,
        mark=liquidation_mark * 1.001,
        qty=qty,
        wallet_equity=wallet_equity,
    )

    assert safe_distance >= 0.0
    assert past_distance == 0.0


def test_margin_model_rejects_non_finite_and_nonsensical_negative_inputs() -> None:
    model = _model()
    invalid_calls = [
        lambda: model.health_factor(
            entry=math.nan, mark=100.0, qty=1.0, wallet_equity=10.0
        ),
        lambda: model.health_factor(
            entry=100.0, mark=math.inf, qty=1.0, wallet_equity=10.0
        ),
        lambda: model.short_unrealized_pnl(entry=100.0, mark=100.0, qty=-1.0),
        # NOTE: negative wallet_equity is intentionally VALID (underwater account -> HF<1
        # -> liquidation); it must NOT raise. See test_negative_wallet_yields_sub_one_health.
        lambda: model.survives_gap(
            entry=100.0,
            mark=100.0,
            qty=1.0,
            wallet_equity=10.0,
            gap_pct=-0.01,
            health_factor_floor=1.5,
        ),
    ]

    for invalid_call in invalid_calls:
        with pytest.raises(ValueError):
            invalid_call()


def test_tier_for_uses_half_open_boundaries_and_rejects_out_of_range() -> None:
    limits = bybit_btc_eth_risk_limits()["BTC/USDT:USDT"]

    for lower_tier, upper_tier in zip(limits.tiers, limits.tiers[1:], strict=False):
        boundary = lower_tier.notional_cap
        assert limits.tier_for(math.nextafter(boundary, 0.0)) is lower_tier
        assert limits.tier_for(boundary) is upper_tier

    with pytest.raises(ValueError):
        limits.tier_for(-0.01)

    finite_limits = RiskLimits(
        symbol=limits.symbol,
        source_quality=limits.source_quality,
        tiers=(
            MaintenanceTier(
                notional_floor=0.0,
                notional_cap=100.0,
                maintenance_margin_rate=0.005,
                maintenance_amount=0.0,
                max_leverage=10.0,
            ),
            MaintenanceTier(
                notional_floor=100.0,
                notional_cap=200.0,
                maintenance_margin_rate=0.010,
                maintenance_amount=0.5,
                max_leverage=5.0,
            ),
        ),
    )

    assert finite_limits.tier_for(100.0) is finite_limits.tiers[1]
    with pytest.raises(ValueError):
        finite_limits.tier_for(200.0)
    with pytest.raises(ValueError):
        finite_limits.tier_for(200.01)


def test_round_qty_floors_to_step_and_min_notional_boundary() -> None:
    instruments = bybit_btc_eth_instruments()
    btc = instruments["BTC/USDT:USDT"]
    eth = instruments["ETH/USDT:USDT"]

    assert btc.round_qty(0.001999999) == pytest.approx(0.001)
    assert btc.round_qty(0.000999999) == 0.0
    assert eth.round_qty(1.239) == pytest.approx(1.23)
    assert eth.round_qty(1.2) == pytest.approx(1.2)
    assert btc.meets_min_notional(btc.min_notional)
    assert not btc.meets_min_notional(math.nextafter(btc.min_notional, 0.0))
    assert eth.meets_min_notional(eth.min_notional)
    assert not eth.meets_min_notional(math.nextafter(eth.min_notional, 0.0))

    with pytest.raises(ValueError):
        btc.round_qty(math.nan)
    with pytest.raises(ValueError):
        btc.meets_min_notional(-0.01)


@pytest.mark.parametrize("symbol", ["BTC/USDT:USDT", "ETH/USDT:USDT"])
def test_dynamic_leverage_capped_never_exceeds_gap_cap_or_hard_cap(symbol: str) -> None:
    model = _model(symbol)
    engine = RiskEngine(
        RiskParams(
            base_leverage=1_000.0,
            max_leverage=1_000.0,
            reserve_pct=0.25,
            gap_stress_pct=0.20,
            health_factor_floor=1.5,
        )
    )

    uncapped_by_margin = engine.dynamic_leverage_capped(
        realized_vol_annual=1e-12,
        funding_rate_8h=1.0,
    )
    gap_cap = engine.gap_survival_leverage_cap(model)
    capped = engine.dynamic_leverage_capped(
        realized_vol_annual=1e-12,
        funding_rate_8h=1.0,
        margin_model=model,
    )

    assert uncapped_by_margin == 5.0
    assert 1.0 <= gap_cap <= 5.0
    assert capped <= 5.0
    assert capped <= gap_cap
    assert capped == pytest.approx(min(5.0, gap_cap))


def test_deleverage_reasons_fire_individually_and_adl_none_is_absent_safe() -> None:
    engine = RiskEngine()
    clean = _clean_deleverage_inputs()

    assert NullADLProvider().adl_rank("BTC/USDT:USDT") is None
    assert engine.deleverage_reasons(**clean) == ()
    assert engine.deleverage_reasons(**(clean | {"adl_rank": None})) == ()
    assert engine.deleverage_reasons(**(clean | {"adl_rank": 2})) == ()

    individual_cases = [
        ({"realized_vol_annual": 1.0}, ("vol_spike",)),
        ({"funding_rate_8h": 0.000049}, ("funding_compression",)),
        ({"funding_rate_8h": -0.000001}, ("funding_reversal_imminent",)),
        ({"health_factor": math.nextafter(1.5, 0.0)}, ("health_factor",)),
        ({"drawdown_pct": 0.05}, ("drawdown_kill",)),
        ({"adl_rank": 3}, ("adl_rank",)),
        ({"net_delta_frac": math.nextafter(0.02, 1.0)}, ("net_delta",)),
        ({"net_delta_frac": math.nextafter(-0.02, -1.0)}, ("net_delta",)),
    ]

    for overrides, expected in individual_cases:
        assert engine.deleverage_reasons(**(clean | overrides)) == expected

    non_triggering_boundaries = [
        {"realized_vol_annual": math.nextafter(1.0, 0.0)},
        {"funding_rate_8h": 0.00005},
        {"health_factor": 1.5},
        {"drawdown_pct": math.nextafter(0.05, 0.0)},
        {"net_delta_frac": 0.02},
        {"net_delta_frac": -0.02},
    ]
    for overrides in non_triggering_boundaries:
        assert engine.deleverage_reasons(**(clean | overrides)) == ()

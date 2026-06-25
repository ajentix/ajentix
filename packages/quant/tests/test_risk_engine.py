from ajentix_quant.risk.engine import NullADLProvider, RiskEngine, RiskParams
from ajentix_quant.risk.margin import (
    VenueMarginModel,
    bybit_btc_eth_instruments,
    bybit_btc_eth_risk_limits,
)


def test_dynamic_leverage_capped():
    eng = RiskEngine(RiskParams(base_leverage=2.0, max_leverage=5.0))
    # very low vol + strong funding should hit the cap, never exceed it
    lev = eng.dynamic_leverage(realized_vol_annual=0.01, funding_rate_8h=0.001)
    assert 1.0 <= lev <= 5.0
    assert lev == 5.0


def test_dynamic_leverage_floor():
    eng = RiskEngine()
    # extreme vol scales leverage down toward the floor of 1.0
    lev = eng.dynamic_leverage(realized_vol_annual=100.0, funding_rate_8h=0.0)
    assert lev == 1.0


def test_liquidation_distance_floor():
    eng = RiskEngine(RiskParams(min_liq_distance_pct=0.15))
    assert eng.liquidation_distance_ok(leverage=5.0) is True  # 1/5 = 0.20 >= 0.15
    assert eng.liquidation_distance_ok(leverage=10.0) is False  # 0.10 < 0.15


def test_funding_reversal_exit():
    eng = RiskEngine(RiskParams(funding_reversal_exit_hours=24))
    assert eng.should_exit_funding_reversal(hours_negative=24.0) is True
    assert eng.should_exit_funding_reversal(hours_negative=8.0) is False


def test_kill_switch_and_reserve():
    eng = RiskEngine(RiskParams(max_drawdown_pct=0.05, reserve_pct=0.25))
    assert eng.kill_switch(drawdown_pct=0.06) is True
    assert eng.kill_switch(drawdown_pct=0.01) is False
    assert abs(eng.deployable_fraction() - 0.75) < 1e-9


def _btc_margin_model() -> VenueMarginModel:
    instruments = bybit_btc_eth_instruments()
    limits = bybit_btc_eth_risk_limits()
    return VenueMarginModel(instruments["BTC/USDT:USDT"], limits["BTC/USDT:USDT"])


def test_dynamic_leverage_capped_respects_gap_survival_cap():
    eng = RiskEngine(RiskParams(base_leverage=2.0, max_leverage=5.0))
    model = _btc_margin_model()

    gap_cap = eng.gap_survival_leverage_cap(model)
    lev = eng.dynamic_leverage_capped(
        realized_vol_annual=0.01,
        funding_rate_8h=0.001,
        margin_model=model,
    )

    assert lev <= gap_cap
    assert lev <= 5.0
    assert lev == gap_cap


def test_dynamic_leverage_never_returns_more_than_5x():
    eng = RiskEngine(RiskParams(base_leverage=100.0, max_leverage=100.0))

    assert eng.dynamic_leverage(realized_vol_annual=0.01, funding_rate_8h=0.001) == 5.0
    assert eng.dynamic_leverage_capped(
        realized_vol_annual=0.01,
        funding_rate_8h=0.001,
    ) == 5.0


def test_deleverage_reasons_fire_in_deterministic_order_and_absent_adl_is_safe():
    eng = RiskEngine()
    clean = dict(
        realized_vol_annual=0.5,
        funding_rate_8h=0.0001,
        health_factor=2.0,
        hours_negative=0.0,
        drawdown_pct=0.0,
    )

    assert NullADLProvider().adl_rank("BTC/USDT:USDT") is None
    assert eng.deleverage_reasons(**clean, adl_rank=None, net_delta_frac=0.0) == ()
    assert eng.should_deleverage(**clean, adl_rank=None, net_delta_frac=0.0) is False
    assert eng.deleverage_reasons(
        realized_vol_annual=1.0,
        funding_rate_8h=0.00001,
        health_factor=1.49,
        hours_negative=0.0,
        drawdown_pct=0.05,
        adl_rank=3,
        net_delta_frac=0.03,
    ) == (
        "vol_spike",
        "funding_compression",
        "health_factor",
        "drawdown_kill",
        "adl_rank",
        "net_delta",
    )
    assert eng.should_deleverage(
        realized_vol_annual=1.0,
        funding_rate_8h=0.00001,
        health_factor=1.49,
        hours_negative=0.0,
        drawdown_pct=0.05,
        adl_rank=3,
        net_delta_frac=0.03,
    ) is True


def test_deleverage_reasons_funding_reversal_imminent_is_separate_from_compression():
    eng = RiskEngine()

    assert eng.deleverage_reasons(
        realized_vol_annual=0.5,
        funding_rate_8h=-0.00001,
        health_factor=2.0,
        hours_negative=0.0,
        drawdown_pct=0.0,
        adl_rank=None,
        net_delta_frac=0.0,
    ) == ("funding_reversal_imminent",)


def _btc_model() -> VenueMarginModel:
    return VenueMarginModel(
        bybit_btc_eth_instruments()["BTC/USDT:USDT"],
        bybit_btc_eth_risk_limits()["BTC/USDT:USDT"],
    )


def test_dynamic_leverage_capped_returns_zero_when_no_leverage_survives_gap():
    # gap_stress_pct=1.0 (100%) at HF floor 1.5 -> no leverage >= 1x is safe -> no entry (0.0)
    eng = RiskEngine(RiskParams(gap_stress_pct=1.0, health_factor_floor=1.5, reserve_pct=0.25))
    lev = eng.dynamic_leverage_capped(
        realized_vol_annual=0.01, funding_rate_8h=0.001, margin_model=_btc_model()
    )
    assert lev == 0.0


def test_dynamic_leverage_capped_respects_gap_cap_for_feasible_gap():
    eng = RiskEngine(RiskParams(gap_stress_pct=0.20, health_factor_floor=1.5, reserve_pct=0.25))
    model = _btc_model()
    cap = eng.gap_survival_leverage_cap(model)
    lev = eng.dynamic_leverage_capped(
        realized_vol_annual=0.01, funding_rate_8h=0.001, margin_model=model
    )
    assert 1.0 <= lev <= cap <= 5.0

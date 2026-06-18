from ajentix_quant.risk.engine import RiskEngine, RiskParams


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

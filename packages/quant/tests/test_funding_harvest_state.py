import math

import pytest

from ajentix_quant.data.sample import sample_market_dataset
from ajentix_quant.strategies.funding_harvest import FundingHarvest
from ajentix_quant.strategies.sizing import SmallCapitalSizingPolicy
from ajentix_quant.strategies.state import MarketState, SignalAction


def _state(**overrides: object) -> MarketState:
    data: dict[str, object] = {
        "symbol": "BTC/USDT:USDT",
        "funding_rate": 0.0002,
        "interval_hours": 8.0,
        "spot_close": 100.0,
        "perp_mark_close": 100.1,
        "index_close": 100.0,
        "basis_bps": 10.0,
        "realized_vol_annual": 0.5,
        "expected_cost_bps": 2.0,
        "equity_usd": 1_000.0,
        "net_delta_frac": 0.0,
        "in_position": False,
        "current_leverage": 0.0,
        "gap_survival_leverage_cap": 2.0,
        "health_factor": 2.0,
        "risk_deleverage": False,
    }
    data.update(overrides)
    return MarketState(**data)  # type: ignore[arg-type]


def test_enter_when_capture_beats_cost_and_gap_cap_is_safe() -> None:
    signal = FundingHarvest().decide(_state())

    assert signal.action is SignalAction.ENTER
    assert signal.target_notional_usd == pytest.approx(500.0)
    assert signal.target_leverage == pytest.approx(2.0)
    assert signal.target_net_delta == 0.0
    assert "expected carry" in signal.reason


def test_flat_when_funding_below_threshold() -> None:
    signal = FundingHarvest().decide(_state(funding_rate=0.00005))

    assert signal.action is SignalAction.FLAT
    assert signal.target_notional_usd == 0.0
    assert signal.target_net_delta == 0.0
    assert "funding below threshold" in signal.reason


def test_flat_when_gap_cap_has_no_safe_leverage() -> None:
    signal = FundingHarvest().decide(_state(gap_survival_leverage_cap=0.99))

    assert signal.action is SignalAction.FLAT
    assert signal.target_net_delta == 0.0
    assert "no safe leverage" in signal.reason


def test_flat_when_expected_carry_does_not_clear_cost_plus_margin() -> None:
    state = _state(
        funding_rate=0.0001,
        gap_survival_leverage_cap=1.0,
        expected_cost_bps=2.0,
    )
    signal = FundingHarvest().decide(state)

    assert signal.action is SignalAction.FLAT
    assert signal.target_net_delta == 0.0
    assert "expected carry below cost+margin" in signal.reason


def test_flat_when_min_notional_is_infeasible_at_tiny_equity() -> None:
    state = _state(
        funding_rate=0.0003,
        equity_usd=10.0,
        gap_survival_leverage_cap=1.0,
    )
    sizing = SmallCapitalSizingPolicy(min_notional_usd=5.0)
    signal = FundingHarvest().decide(state, sizing=sizing)

    assert signal.action is SignalAction.FLAT
    assert signal.target_notional_usd == 0.0
    assert signal.target_net_delta == 0.0
    assert "min-notional infeasible" in signal.reason


def test_exit_on_risk_deleverage() -> None:
    signal = FundingHarvest().decide(_state(in_position=True, risk_deleverage=True))

    assert signal.action is SignalAction.EXIT
    assert signal.target_net_delta == 0.0
    assert "risk deleverage/kill/liq-buffer" in signal.reason


def test_exit_on_negative_funding() -> None:
    signal = FundingHarvest().decide(_state(in_position=True, funding_rate=-0.00001))

    assert signal.action is SignalAction.EXIT
    assert signal.target_net_delta == 0.0
    assert "funding reversal/negative" in signal.reason


def test_exit_on_funding_compression() -> None:
    signal = FundingHarvest().decide(_state(in_position=True, funding_rate=0.00004))

    assert signal.action is SignalAction.EXIT
    assert signal.target_net_delta == 0.0
    assert "funding compression" in signal.reason


def test_exit_on_net_delta_drift() -> None:
    signal = FundingHarvest().decide(_state(in_position=True, net_delta_frac=0.0201))

    assert signal.action is SignalAction.EXIT
    assert signal.target_net_delta == 0.0
    assert "net-delta drift" in signal.reason


def test_exit_on_basis_dislocation() -> None:
    signal = FundingHarvest().decide(_state(in_position=True, basis_bps=-50.1))

    assert signal.action is SignalAction.EXIT
    assert signal.target_net_delta == 0.0
    assert "basis dislocation" in signal.reason


def test_hold_when_position_remains_inside_risk_bands() -> None:
    state = _state(in_position=True, current_leverage=2.0)
    signal = FundingHarvest().decide(state)

    assert signal.action is SignalAction.HOLD
    assert signal.target_notional_usd == pytest.approx(500.0)
    assert signal.target_leverage == pytest.approx(2.0)
    assert signal.target_net_delta == 0.0
    assert "carry remains valid" in signal.reason


def test_target_net_delta_is_zero_for_all_actions() -> None:
    strategy = FundingHarvest()
    states = [
        _state(),
        _state(in_position=True, current_leverage=2.0),
        _state(in_position=True, funding_rate=-0.00001),
        _state(funding_rate=0.00001),
    ]

    assert {strategy.decide(state).action for state in states} == {
        SignalAction.ENTER,
        SignalAction.HOLD,
        SignalAction.EXIT,
        SignalAction.FLAT,
    }
    for state in states:
        assert strategy.decide(state).target_net_delta == 0.0


@pytest.mark.parametrize(
    "overrides",
    [
        {"funding_rate": math.nan},
        {"spot_close": math.inf},
        {"index_close": math.inf},
        {"interval_hours": -8.0},
        {"realized_vol_annual": -0.01},
        {"expected_cost_bps": -0.01},
        {"equity_usd": -1.0},
        {"current_leverage": -0.01},
        {"gap_survival_leverage_cap": -0.01},
    ],
)
def test_market_state_rejects_nan_inf_and_negative_inputs(
    overrides: dict[str, float],
) -> None:
    with pytest.raises(ValueError):
        _state(**overrides)


def test_decide_revalidates_state_and_rejects_bad_decision_inputs() -> None:
    strategy = FundingHarvest()

    with pytest.raises(ValueError):
        strategy.decide(_state(), hold_intervals=0)
    with pytest.raises(ValueError):
        strategy.decide(_state(), safety_margin_bps=math.inf)


def test_sample_market_dataset_is_deterministic_and_exercises_actions() -> None:
    first = sample_market_dataset()
    second = sample_market_dataset()

    assert first == second
    assert 20 <= len(first) <= 40

    actions = {FundingHarvest().decide(state).action for state in first}
    assert actions == {
        SignalAction.ENTER,
        SignalAction.HOLD,
        SignalAction.EXIT,
        SignalAction.FLAT,
    }

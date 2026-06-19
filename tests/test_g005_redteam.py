import ast
import inspect
import math
import random
import textwrap

import pytest

import ajentix_quant.strategies.funding_harvest as funding_harvest_module
from ajentix_quant.data.sample import sample_market_dataset
from ajentix_quant.strategies.funding_harvest import FundingHarvest
from ajentix_quant.strategies.sizing import SmallCapitalSizingPolicy
from ajentix_quant.strategies.state import MarketState, SignalAction

SEED = 20260618


def _state(**overrides: object) -> MarketState:
    data: dict[str, object] = {
        "symbol": "BTC/USDT:USDT",
        "funding_rate": 0.00025,
        "interval_hours": 8.0,
        "spot_close": 100.0,
        "perp_mark_close": 100.1,
        "index_close": 100.0,
        "basis_bps": 5.0,
        "realized_vol_annual": 0.45,
        "expected_cost_bps": 1.0,
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


def test_target_net_delta_is_zero_across_seeded_adversarial_sweep() -> None:
    rng = random.Random(SEED)
    strategy = FundingHarvest()
    actions: set[SignalAction] = set()

    for _ in range(300):
        in_position = rng.choice([False, True])
        gap_cap = rng.choice([0.0, 0.25, 0.999, 1.0, 1.5, 3.0, 5.0, 100.0])
        state = _state(
            funding_rate=rng.uniform(-0.00025, 0.00075),
            expected_cost_bps=rng.uniform(0.0, 12.0),
            gap_survival_leverage_cap=gap_cap,
            equity_usd=rng.choice([0.0, 1.0, 10.0, 25.0, 100.0, 1_000.0, 2_500.0]),
            in_position=in_position,
            current_leverage=rng.uniform(0.0, 5.0) if in_position else 0.0,
            basis_bps=rng.uniform(-120.0, 120.0),
            net_delta_frac=rng.uniform(-0.08, 0.08),
            risk_deleverage=rng.random() < 0.2,
            realized_vol_annual=rng.uniform(0.0, 2.5),
            spot_close=rng.uniform(50.0, 125.0),
            perp_mark_close=rng.uniform(50.0, 125.0),
        )

        signal = strategy.decide(state)
        actions.add(signal.action)
        assert signal.target_net_delta == 0.0

    assert actions == {
        SignalAction.ENTER,
        SignalAction.HOLD,
        SignalAction.EXIT,
        SignalAction.FLAT,
    }


def test_carry_vs_cost_boundary_requires_strictly_positive_edge() -> None:
    strategy = FundingHarvest(min_funding_rate_8h=0.0001)
    safety_margin_bps = 1.0
    hold_intervals = 1
    equality_funding = 0.0002
    expected_cost_bps = equality_funding * hold_intervals * 1.0 * 1e4 - safety_margin_bps

    equality_state = _state(
        funding_rate=equality_funding,
        expected_cost_bps=expected_cost_bps,
        gap_survival_leverage_cap=1.0,
    )
    above_state = _state(
        funding_rate=equality_funding + 1e-10,
        expected_cost_bps=expected_cost_bps,
        gap_survival_leverage_cap=1.0,
    )
    below_state = _state(
        funding_rate=equality_funding - 1e-10,
        expected_cost_bps=expected_cost_bps,
        gap_survival_leverage_cap=1.0,
    )

    assert strategy.decide(
        equality_state,
        hold_intervals=hold_intervals,
        safety_margin_bps=safety_margin_bps,
    ).action is SignalAction.FLAT
    assert strategy.decide(
        above_state,
        hold_intervals=hold_intervals,
        safety_margin_bps=safety_margin_bps,
    ).action is SignalAction.ENTER
    assert strategy.decide(
        below_state,
        hold_intervals=hold_intervals,
        safety_margin_bps=safety_margin_bps,
    ).action is SignalAction.FLAT


@pytest.mark.parametrize(
    ("name", "state", "reason_fragment"),
    [
        (
            "funding-threshold",
            _state(funding_rate=FundingHarvest().min_funding_rate_8h - 1e-12),
            "funding below threshold",
        ),
        (
            "gap-cap-below-one",
            _state(funding_rate=0.0005, gap_survival_leverage_cap=0.999),
            "no safe leverage",
        ),
        (
            "min-notional-infeasible",
            _state(funding_rate=0.001, expected_cost_bps=0.0, equity_usd=1.0),
            "min-notional infeasible",
        ),
    ],
)
def test_no_entry_gates_block_before_entry(
    name: str,
    state: MarketState,
    reason_fragment: str,
) -> None:
    signal = FundingHarvest().decide(state, sizing=SmallCapitalSizingPolicy())

    assert name
    assert signal.action is SignalAction.FLAT
    assert signal.target_notional_usd == 0.0
    assert signal.target_leverage == 0.0
    assert signal.target_net_delta == 0.0
    assert reason_fragment in signal.reason


@pytest.mark.parametrize("gap_cap", [1.0, 1.25, 2.75, 3.0, 100.0])
def test_enter_leverage_is_bounded_by_gap_base_and_absolute_caps(gap_cap: float) -> None:
    state = _state(
        funding_rate=0.001,
        expected_cost_bps=0.0,
        gap_survival_leverage_cap=gap_cap,
        equity_usd=1_000.0,
    )

    signal = FundingHarvest().decide(state)

    assert signal.action is SignalAction.ENTER
    assert 1.0 <= signal.target_leverage <= min(gap_cap, 3.0)
    assert signal.target_leverage <= 5.0
    assert signal.target_leverage == pytest.approx(min(gap_cap, 3.0))
    assert signal.target_net_delta == 0.0


def test_exit_priority_returns_risk_reason_when_multiple_exit_conditions_trigger() -> None:
    signal = FundingHarvest().decide(
        _state(
            in_position=True,
            risk_deleverage=True,
            funding_rate=-0.0002,
            net_delta_frac=0.25,
            basis_bps=200.0,
            current_leverage=2.0,
        )
    )

    assert signal.action is SignalAction.EXIT
    assert signal.target_net_delta == 0.0
    assert signal.reason.startswith("risk deleverage/kill/liq-buffer")


@pytest.mark.parametrize(
    ("state", "reason_fragment"),
    [
        (
            _state(in_position=True, risk_deleverage=True, current_leverage=2.0),
            "risk deleverage/kill/liq-buffer",
        ),
        (
            _state(in_position=True, funding_rate=-0.000001, current_leverage=2.0),
            "funding reversal/negative",
        ),
        (
            _state(in_position=True, funding_rate=0.000049, current_leverage=2.0),
            "funding compression",
        ),
        (
            _state(in_position=True, net_delta_frac=0.020001, current_leverage=2.0),
            "net-delta drift",
        ),
        (
            _state(in_position=True, basis_bps=50.001, current_leverage=2.0),
            "basis dislocation",
        ),
    ],
)
def test_each_exit_condition_fires_when_isolated(
    state: MarketState,
    reason_fragment: str,
) -> None:
    signal = FundingHarvest().decide(state)

    assert signal.action is SignalAction.EXIT
    assert signal.target_notional_usd == 0.0
    assert signal.target_leverage == 0.0
    assert signal.target_net_delta == 0.0
    assert reason_fragment in signal.reason


def test_hold_when_position_remains_healthy_and_profitable() -> None:
    signal = FundingHarvest().decide(
        _state(
            in_position=True,
            funding_rate=0.00022,
            current_leverage=2.5,
            net_delta_frac=0.019,
            basis_bps=-12.0,
            risk_deleverage=False,
        )
    )

    assert signal.action is SignalAction.HOLD
    assert signal.target_notional_usd == pytest.approx(625.0)
    assert signal.target_leverage == pytest.approx(2.5)
    assert signal.target_net_delta == 0.0
    assert "carry remains valid" in signal.reason


def test_decide_is_deterministic_for_identical_market_state_and_parameters() -> None:
    strategy = FundingHarvest()
    state = _state(
        funding_rate=0.000333,
        expected_cost_bps=0.75,
        gap_survival_leverage_cap=2.4,
        basis_bps=17.0,
        equity_usd=750.0,
    )

    first = strategy.decide(state, hold_intervals=2, safety_margin_bps=0.5)
    second = strategy.decide(state, hold_intervals=2, safety_margin_bps=0.5)

    assert first == second


@pytest.mark.parametrize(
    "overrides",
    [
        pytest.param({"funding_rate": math.nan}, id="nan-funding"),
        pytest.param({"expected_cost_bps": math.inf}, id="inf-cost"),
        pytest.param({"equity_usd": -0.01}, id="negative-equity"),
        pytest.param({"realized_vol_annual": -1e-12}, id="negative-vol"),
        pytest.param({"interval_hours": 0.0}, id="zero-interval"),
        pytest.param({"interval_hours": -8.0}, id="negative-interval"),
        pytest.param({"spot_close": 0.0}, id="zero-spot"),
        pytest.param({"spot_close": -1.0}, id="negative-spot"),
        pytest.param({"perp_mark_close": 0.0}, id="zero-perp-mark"),
        pytest.param({"perp_mark_close": -1.0}, id="negative-perp-mark"),
    ],
)
def test_market_state_rejects_non_finite_negative_and_non_positive_inputs(
    overrides: dict[str, float],
) -> None:
    with pytest.raises(ValueError):
        _state(**overrides)


def test_sample_market_dataset_is_deterministic_and_covers_all_actions() -> None:
    first = sample_market_dataset()
    second = sample_market_dataset()

    assert first == second
    actions = [FundingHarvest().decide(state).action for state in first]
    assert set(actions) == {
        SignalAction.ENTER,
        SignalAction.HOLD,
        SignalAction.EXIT,
        SignalAction.FLAT,
    }


def test_funding_harvest_source_has_no_network_imports_and_no_decide_io(monkeypatch) -> None:
    module_source = inspect.getsource(funding_harvest_module)
    module_tree = ast.parse(module_source)
    forbidden_import_roots = {"ccxt", "requests", "socket"}
    import_roots: set[str] = set()
    called_names: set[str] = set()

    for node in ast.walk(module_tree):
        if isinstance(node, ast.Import):
            import_roots.update(alias.name.split(".", 1)[0] for alias in node.names)
        if isinstance(node, ast.ImportFrom) and node.module is not None:
            import_roots.add(node.module.split(".", 1)[0])
        if isinstance(node, ast.Call):
            if isinstance(node.func, ast.Name):
                called_names.add(node.func.id)
            elif isinstance(node.func, ast.Attribute):
                called_names.add(node.func.attr)

    decide_tree = ast.parse(textwrap.dedent(inspect.getsource(FundingHarvest.decide)))
    decide_call_names: set[str] = set()
    for node in ast.walk(decide_tree):
        if isinstance(node, ast.Call):
            if isinstance(node.func, ast.Name):
                decide_call_names.add(node.func.id)
            elif isinstance(node.func, ast.Attribute):
                decide_call_names.add(node.func.attr)

    forbidden_io_call_names = {
        "connect",
        "delete",
        "get",
        "open",
        "patch",
        "post",
        "put",
        "read_bytes",
        "read_text",
        "recv",
        "request",
        "send",
        "sendall",
        "urlopen",
        "write_bytes",
        "write_text",
    }

    assert forbidden_import_roots.isdisjoint(import_roots)
    assert forbidden_io_call_names.isdisjoint(called_names)
    assert forbidden_io_call_names.isdisjoint(decide_call_names)

    def fail_open(*args: object, **kwargs: object) -> None:
        raise AssertionError("FundingHarvest.decide attempted disk I/O")

    monkeypatch.setattr("builtins.open", fail_open)
    signal = FundingHarvest().decide(_state(funding_rate=0.0005))
    assert signal.action is SignalAction.ENTER

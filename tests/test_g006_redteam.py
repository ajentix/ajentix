from __future__ import annotations

from collections import Counter
from types import SimpleNamespace

import pytest

from ajentix_quant.backtest.events import EventKind, LedgerEvent
from ajentix_quant.risk.engine import RiskEngine, RiskParams
from ajentix_quant.strategies.sizing import SmallCapitalSizingPolicy
from ajentix_quant.strategies.state import CarrySignal, MarketState, SignalAction
from test_backtest_two_leg import STEP, _dataset, _runner, _settings

INITIAL_EQUITY = 1000.0


class _FixedLeverageHoldStrategy:
    """Test double that enters once and keeps holding so cash effects are isolated."""

    def __init__(self, leverage: float = 1.0) -> None:
        self.leverage = leverage

    def decide(
        self,
        state: MarketState,
        *,
        sizing: SmallCapitalSizingPolicy | None = None,
        hold_intervals: int = 3,
        safety_margin_bps: float = 1.0,
    ) -> CarrySignal:
        _ = hold_intervals, safety_margin_bps
        if state.in_position:
            return CarrySignal(
                symbol=state.symbol,
                action=SignalAction.HOLD,
                target_notional_usd=0.0,
                target_leverage=state.current_leverage,
                target_net_delta=0.0,
                reason="test hold regardless of funding sign",
            )
        policy = sizing or SmallCapitalSizingPolicy()
        return CarrySignal(
            symbol=state.symbol,
            action=SignalAction.ENTER,
            target_notional_usd=policy.size(
                equity_usd=state.equity_usd,
                leverage=self.leverage,
            ),
            target_leverage=self.leverage,
            target_net_delta=0.0,
            reason="test fixed-leverage entry",
        )


def _hold_runner(
    *,
    leverage: float = 1.0,
    settings: SimpleNamespace | None = None,
    risk: RiskEngine | None = None,
    sizing: SmallCapitalSizingPolicy | None = None,
):
    return _runner(
        settings=settings,
        risk=risk
        or RiskEngine(
            RiskParams(
                funding_reversal_imminent_8h=-1.0,
                max_drawdown_pct=10.0,
            )
        ),
        sizing=sizing,
        strategy=_FixedLeverageHoldStrategy(leverage),
    )


def _event_counts(events: tuple[LedgerEvent, ...]) -> dict[EventKind, int]:
    counts = Counter(event.kind for event in events)
    return {kind: counts[kind] for kind in EventKind}


def _counter_fields(result) -> tuple[int, int, int, int, int, int]:
    return (
        result.n_entries,
        result.n_exits,
        result.n_forced_exits,
        result.n_rebalances,
        result.n_liquidations,
        result.n_deleverages,
    )


def _event_signature(events: tuple[LedgerEvent, ...]) -> tuple[tuple[object, ...], ...]:
    return tuple(
        (
            event.timestamp_ms,
            event.kind,
            event.reason,
            event.amount,
            event.notional,
            event.leverage,
            event.net_delta,
            event.equity_before,
            event.equity_after,
            event.spot_price,
            event.perp_mark,
        )
        for event in events
    )


def test_funding_sign_cash_effects_accrue_both_directions_over_multiple_periods() -> None:
    positive = _hold_runner().run_market_dataset(
        _dataset(funding_rates=[0.001, 0.001, 0.001]),
    )
    negative = _hold_runner().run_market_dataset(
        _dataset(funding_rates=[-0.001, -0.001, -0.001]),
    )

    assert positive.n_entries == 1
    assert positive.n_exits == 0
    assert positive.funding_received > 0.0
    assert positive.funding_paid == 0.0
    assert positive.final_equity == pytest.approx(
        INITIAL_EQUITY + positive.funding_received,
        abs=1e-8,
    )
    assert positive.final_equity > positive.equity_curve[1]

    assert negative.n_entries == 1
    assert negative.n_exits == 0
    assert negative.funding_paid > 0.0
    assert negative.funding_received == 0.0
    assert negative.final_equity == pytest.approx(
        INITIAL_EQUITY - negative.funding_paid,
        abs=1e-8,
    )
    assert negative.final_equity < negative.equity_curve[1]


def test_price_legs_cancel_identical_path_and_basis_widening_has_correct_sign() -> None:
    settings = _settings(max_net_delta_frac=1.0)
    identical_path = _hold_runner(settings=settings).run_market_dataset(
        _dataset(
            funding_rates=[0.0, 0.0],
            spot_closes=[100.0, 120.0],
            perp_closes=[100.0, 120.0],
        )
    )
    basis_widening = _hold_runner(settings=settings).run_market_dataset(
        _dataset(
            funding_rates=[0.0, 0.0],
            spot_closes=[100.0, 100.0],
            perp_closes=[100.0, 110.0],
        )
    )

    assert identical_path.funding_received == 0.0
    assert identical_path.funding_paid == 0.0
    assert identical_path.total_fees == 0.0
    assert identical_path.total_slippage == 0.0
    assert identical_path.final_equity == pytest.approx(INITIAL_EQUITY, abs=1e-8)

    assert basis_widening.final_equity < INITIAL_EQUITY
    assert basis_widening.final_equity == pytest.approx(975.0, abs=1e-8)
    assert INITIAL_EQUITY - basis_widening.final_equity < 30.0
    assert basis_widening.liquidated is False


def test_run_market_dataset_is_bit_deterministic_for_same_and_rebuilt_dataset() -> None:
    def build_dataset():
        return _dataset(
            funding_rates=[0.005, 0.001, 0.002, 0.0015],
            spot_closes=[100.0, 101.0, 101.0, 102.0],
            perp_closes=[100.0, 101.2, 100.8, 102.1],
        )

    dataset = build_dataset()
    same_a = _hold_runner().run_market_dataset(dataset)
    same_b = _hold_runner().run_market_dataset(dataset)

    assert same_a.final_equity == same_b.final_equity
    assert tuple(same_a.equity_curve) == tuple(same_b.equity_curve)
    assert _counter_fields(same_a) == _counter_fields(same_b)
    assert _event_counts(same_a.events) == _event_counts(same_b.events)
    assert _event_signature(same_a.events) == _event_signature(same_b.events)

    rebuilt_dataset_a = build_dataset()
    rebuilt_dataset_b = build_dataset()
    rebuilt_a = _hold_runner().run_market_dataset(rebuilt_dataset_a)
    rebuilt_b = _hold_runner().run_market_dataset(rebuilt_dataset_b)

    assert rebuilt_dataset_a == rebuilt_dataset_b
    assert rebuilt_a.final_equity == rebuilt_b.final_equity == same_a.final_equity
    assert (
        tuple(rebuilt_a.equity_curve)
        == tuple(rebuilt_b.equity_curve)
        == tuple(same_a.equity_curve)
    )
    assert _counter_fields(rebuilt_a) == _counter_fields(rebuilt_b) == _counter_fields(same_a)
    assert (
        _event_counts(rebuilt_a.events)
        == _event_counts(rebuilt_b.events)
        == _event_counts(same_a.events)
    )
    assert (
        _event_signature(rebuilt_a.events)
        == _event_signature(rebuilt_b.events)
        == _event_signature(same_a.events)
    )


def test_intraperiod_adverse_mark_high_liquidates_over_cap_but_gap_cap_survives() -> None:
    aggressive_sizing = SmallCapitalSizingPolicy(max_position_pct=1.0, reserve_pct=0.0)
    favorable_close_with_adverse_high = _dataset(
        funding_rates=[0.005, 0.005, 0.005],
        spot_closes=[100.0, 100.0, 100.0],
        perp_closes=[100.0, 90.0, 100.0],
        mark_highs=[100.0, 140.0, 100.0],
    )
    over_cap = _hold_runner(
        leverage=3.0,
        risk=RiskEngine(
            RiskParams(
                gap_stress_pct=0.0,
                reserve_pct=0.0,
                funding_reversal_imminent_8h=-1.0,
                max_drawdown_pct=10.0,
            )
        ),
        sizing=aggressive_sizing,
    ).run_market_dataset(
        favorable_close_with_adverse_high,
        realized_vol_annual=[0.2, 0.2, 0.2],
    )
    capped = _runner(
        risk=RiskEngine(RiskParams(gap_stress_pct=0.40, reserve_pct=0.0)),
        sizing=aggressive_sizing,
    ).run_market_dataset(
        favorable_close_with_adverse_high,
        realized_vol_annual=[0.2, 0.2, 0.2],
    )

    liquidation_events = [event for event in over_cap.events if event.kind is EventKind.LIQUIDATION]
    assert over_cap.liquidated is True, _event_signature(over_cap.events)
    assert over_cap.n_liquidations >= 1
    assert liquidation_events
    assert float(liquidation_events[0].perp_mark or 0.0) == 140.0
    assert over_cap.final_equity < INITIAL_EQUITY
    assert not any(
        event.kind is EventKind.ENTRY and event.timestamp_ms > liquidation_events[0].timestamp_ms
        for event in over_cap.events
    )

    assert capped.liquidated is False
    assert capped.n_liquidations == 0
    assert capped.max_leverage_used < over_cap.max_leverage_used


def test_last_period_funding_mutation_does_not_change_earlier_events_or_curve() -> None:
    base_dataset = _dataset(
        funding_rates=[0.005, 0.005, 0.005, 0.005],
        spot_closes=[100.0, 100.0, 101.0, 101.0],
        perp_closes=[100.0, 100.0, 101.0, 101.0],
    )
    mutated_dataset = _dataset(
        funding_rates=[0.005, 0.005, 0.005, 0.50],
        spot_closes=[100.0, 100.0, 101.0, 101.0],
        perp_closes=[100.0, 100.0, 101.0, 101.0],
    )

    base = _hold_runner().run_market_dataset(base_dataset)
    mutated = _hold_runner().run_market_dataset(mutated_dataset)

    earlier_base_events = tuple(event for event in base.events if event.timestamp_ms < 3 * STEP)
    earlier_mutated_events = tuple(
        event for event in mutated.events if event.timestamp_ms < 3 * STEP
    )
    assert _event_signature(earlier_base_events) == _event_signature(earlier_mutated_events)
    assert tuple(base.equity_curve[:-1]) == tuple(mutated.equity_curve[:-1])
    assert base.final_equity != mutated.final_equity


def test_costs_monotonically_reduce_equity_against_zero_cost_run() -> None:
    dataset = _dataset(funding_rates=[0.001, 0.001], volume=10_000.0)
    zero_cost = _hold_runner(settings=_settings()).run_market_dataset(dataset)
    with_costs = _hold_runner(
        settings=_settings(
            spot_taker_fee_bps=10.0,
            perp_taker_fee_bps=5.5,
            slippage_base_bps=2.0,
            slippage_impact_bps_per_pct_volume=5.0,
            slippage_cap_bps=50.0,
        )
    ).run_market_dataset(dataset, slippage_stress_multiplier=3.0)

    assert with_costs.total_fees > zero_cost.total_fees == 0.0
    assert with_costs.total_slippage > zero_cost.total_slippage == 0.0
    assert with_costs.final_equity < zero_cost.final_equity


def test_zero_trade_volume_fails_closed_before_slippage_can_use_non_trade_volume() -> None:
    with pytest.raises(ValueError, match="trade volume must be finite and positive"):
        _hold_runner().run_market_dataset(_dataset(funding_rates=[0.005], volume=0.0))

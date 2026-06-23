"""Hand-calculated tests for free-data VRP cost-budget gates."""

from __future__ import annotations

from dataclasses import FrozenInstanceError

from ajentix_quant.backtest.vrp_free_cost_budget import (
    VrpFreeCostBudgetStatus,
    evaluate_vrp_free_cost_budget,
    frozen_min_credit_to_width_floor,
    iv_fraction_to_vol_points,
    round_trip_leg_crossing_usd,
    round_trip_structure_spread_usd,
    spread_price_eth,
)


def test_clearly_passing_structure_clears_p75_p50_and_net_credit_gates() -> None:
    result = evaluate_vrp_free_cost_budget(
        gross_credit_usd=85.0,
        width_usd=200.0,
        p50_spread_usd=8.0,
        p75_spread_usd=20.0,
        sample_count=30,
        distinct_months=6,
    )

    assert result.status is VrpFreeCostBudgetStatus.PASS
    assert result.budget_pass is True
    assert result.sample_coverage_pass is True
    assert result.min_credit_to_width == 0.15
    assert result.required_min_credit_usd == 30.0
    assert result.max_absorbable_round_trip_spread_usd == 55.0
    assert result.p75_safety_spread_usd == 25.0
    assert result.p50_margin_spread_usd == 12.0
    assert result.net_credit_after_p75_safety_usd == 60.0
    assert result.net_credit_to_width_after_p75_safety == 0.3
    assert result.p75_safety_pass is True
    assert result.p50_margin_pass is True
    assert result.net_credit_to_width_after_p75_safety_pass is True
    assert result.fail_reasons == ()
    assert result.authorizing is False
    assert result.capital_go_allowed is False
    assert result.non_authorizing_reason == "free_vrp_cost_budget_component_only"


def test_expensive_spread_fails_budget_without_authorization() -> None:
    result = evaluate_vrp_free_cost_budget(
        gross_credit_usd=60.0,
        width_usd=200.0,
        p50_spread_usd=22.0,
        p75_spread_usd=40.0,
        sample_count=30,
        distinct_months=6,
    )

    assert result.status is VrpFreeCostBudgetStatus.FAIL_BUDGET
    assert result.budget_pass is False
    assert result.required_min_credit_usd == 30.0
    assert result.max_absorbable_round_trip_spread_usd == 30.0
    assert result.p75_safety_spread_usd == 50.0
    assert result.p50_margin_spread_usd == 33.0
    assert result.net_credit_after_p75_safety_usd == 10.0
    assert result.net_credit_to_width_after_p75_safety == 0.05
    assert result.p75_safety_pass is False
    assert result.p50_margin_pass is False
    assert result.net_credit_to_width_after_p75_safety_pass is False
    assert result.fail_reasons == (
        "absorbable_spread_below_p75_safety_spread",
        "absorbable_spread_below_p50_margin_spread",
        "net_credit_to_width_after_p75_safety_below_grid_floor",
    )
    assert result.capital_go_allowed is False


def test_exact_boundary_counts_as_pass() -> None:
    result = evaluate_vrp_free_cost_budget(
        gross_credit_usd=80.0,
        width_usd=200.0,
        p50_spread_usd=20.0,
        p75_spread_usd=40.0,
        sample_count=30,
        distinct_months=6,
    )

    assert result.status is VrpFreeCostBudgetStatus.PASS
    assert result.budget_pass is True
    assert result.required_min_credit_usd == 30.0
    assert result.max_absorbable_round_trip_spread_usd == 50.0
    assert result.p75_safety_spread_usd == 50.0
    assert result.p50_margin_spread_usd == 30.0
    assert result.net_credit_after_p75_safety_usd == 30.0
    assert result.net_credit_to_width_after_p75_safety == 0.15
    assert result.p75_safety_pass is True
    assert result.p50_margin_pass is True
    assert result.net_credit_to_width_after_p75_safety_pass is True


def test_unit_conversions_eth_spread_usd_and_vol_points() -> None:
    leg_a = round_trip_leg_crossing_usd(
        bid_price=0.125,
        ask_price=0.1875,
        index_price_usd=1600.0,
        contract_multiplier=1.0,
        quantity=2.0,
    )
    leg_b = round_trip_leg_crossing_usd(
        bid_price=0.03125,
        ask_price=0.046875,
        index_price_usd=1600.0,
        contract_multiplier=1.0,
        quantity=2.0,
    )
    result = evaluate_vrp_free_cost_budget(
        gross_credit_usd=85.0,
        width_usd=200.0,
        p50_spread_usd=8.0,
        p75_spread_usd=20.0,
        sample_count=30,
        distinct_months=6,
        p50_spread_iv_fraction=0.03125,
        p75_spread_iv_fraction=0.0625,
    )

    assert spread_price_eth(bid_price=0.125, ask_price=0.1875) == 0.0625
    assert spread_price_eth(bid_price=0.25, ask_price=0.125) == 0.0
    assert leg_a == 200.0
    assert leg_b == 50.0
    assert round_trip_structure_spread_usd((leg_a, leg_b)) == 250.0
    assert iv_fraction_to_vol_points(0.03125) == 3.125
    assert result.p50_spread_vol_points == 3.125
    assert result.p75_spread_vol_points == 6.25
    assert result.p50_margin_spread_vol_points == 4.6875
    assert result.p75_safety_spread_vol_points == 7.8125


def test_fail_closed_insufficient_samples_or_months_are_inconclusive() -> None:
    insufficient_samples = evaluate_vrp_free_cost_budget(
        gross_credit_usd=85.0,
        width_usd=200.0,
        p50_spread_usd=8.0,
        p75_spread_usd=20.0,
        sample_count=29,
        distinct_months=6,
    )
    insufficient_months = evaluate_vrp_free_cost_budget(
        gross_credit_usd=85.0,
        width_usd=200.0,
        p50_spread_usd=8.0,
        p75_spread_usd=20.0,
        sample_count=30,
        distinct_months=5,
    )

    assert insufficient_samples.status is VrpFreeCostBudgetStatus.INCONCLUSIVE
    assert insufficient_samples.budget_pass is False
    assert insufficient_samples.sample_coverage_pass is False
    assert insufficient_samples.p75_safety_pass is True
    assert insufficient_samples.p50_margin_pass is True
    assert insufficient_samples.net_credit_to_width_after_p75_safety_pass is True
    assert insufficient_samples.fail_reasons == (
        "sample_count_below_min_samples_per_bin",
    )
    assert insufficient_months.status is VrpFreeCostBudgetStatus.INCONCLUSIVE
    assert insufficient_months.budget_pass is False
    assert insufficient_months.sample_coverage_pass is False
    assert insufficient_months.fail_reasons == (
        "distinct_months_below_min_distinct_months_per_bin",
    )


def test_explicit_grid_floor_must_come_from_frozen_structure_grid() -> None:
    result = evaluate_vrp_free_cost_budget(
        gross_credit_usd=90.0,
        width_usd=200.0,
        p50_spread_usd=8.0,
        p75_spread_usd=20.0,
        sample_count=30,
        distinct_months=6,
        min_credit_to_width=0.20,
    )

    assert frozen_min_credit_to_width_floor() == 0.15
    assert result.min_credit_to_width == 0.20
    assert result.required_min_credit_usd == 40.0
    assert result.max_absorbable_round_trip_spread_usd == 50.0
    assert result.status is VrpFreeCostBudgetStatus.PASS

    try:
        evaluate_vrp_free_cost_budget(
            gross_credit_usd=85.0,
            width_usd=200.0,
            p50_spread_usd=8.0,
            p75_spread_usd=20.0,
            sample_count=30,
            distinct_months=6,
            min_credit_to_width=0.17,
        )
    except ValueError as exc:
        assert "frozen grid values" in str(exc)
    else:  # pragma: no cover - protects the hand-rolled raises check
        raise AssertionError("non-frozen min_credit_to_width should fail closed")


def test_result_is_immutable() -> None:
    result = evaluate_vrp_free_cost_budget(
        gross_credit_usd=85.0,
        width_usd=200.0,
        p50_spread_usd=8.0,
        p75_spread_usd=20.0,
        sample_count=30,
        distinct_months=6,
    )

    try:
        result.status = VrpFreeCostBudgetStatus.FAIL_BUDGET
    except FrozenInstanceError:
        pass
    else:  # pragma: no cover - protects the hand-rolled raises check
        raise AssertionError("cost-budget result must be immutable")

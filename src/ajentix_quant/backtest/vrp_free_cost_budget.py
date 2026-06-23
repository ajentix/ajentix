"""Numeric cost-budget gates for free-data-native VRP research.

This module is deliberately pure and non-authorizing. It consumes already-resolved,
fold-causal calibrated spread quantiles for a matching bin and never performs bin lookup,
I/O, network access, order routing, or capital authorization.
"""

from __future__ import annotations

import math
from collections.abc import Iterable
from dataclasses import dataclass
from enum import StrEnum

from ajentix_quant.research.vrp_free_preregistration import (
    PLAN_COST_BUDGET_BAR,
    PLAN_STRUCTURE_GRID,
    PLAN_UNIT_CONVERSIONS,
)


class VrpFreeCostBudgetStatus(StrEnum):
    """Status for the free-data cost-budget component verdict."""

    PASS = "PASS"
    FAIL_BUDGET = "FAIL_BUDGET"
    INCONCLUSIVE = "INCONCLUSIVE"


@dataclass(frozen=True, kw_only=True)
class VrpFreeCostBudgetResult:
    """Immutable audit record for one free-data VRP cost-budget calculation."""

    status: VrpFreeCostBudgetStatus
    budget_pass: bool
    sample_coverage_pass: bool
    p75_safety_pass: bool
    p50_margin_pass: bool
    net_credit_to_width_after_p75_safety_pass: bool
    gross_credit_usd: float
    width_usd: float
    gross_credit_to_width: float
    min_credit_to_width: float
    required_min_credit_usd: float
    max_absorbable_round_trip_spread_usd: float
    p50_spread_usd: float
    p75_spread_usd: float
    p50_margin_multiplier: float
    p75_safety_multiplier: float
    p50_margin_spread_usd: float
    p75_safety_spread_usd: float
    net_credit_after_p50_margin_usd: float
    net_credit_after_p75_safety_usd: float
    net_credit_to_width_after_p75_safety: float
    sample_count: int
    distinct_months: int
    min_samples_per_bin: int
    min_distinct_months_per_bin: int
    p50_spread_iv_fraction: float | None
    p75_spread_iv_fraction: float | None
    p50_spread_vol_points: float | None
    p75_spread_vol_points: float | None
    p50_margin_spread_vol_points: float | None
    p75_safety_spread_vol_points: float | None
    fail_reasons: tuple[str, ...]
    authorizing: bool
    capital_go_allowed: bool
    non_authorizing_reason: str


def evaluate_vrp_free_cost_budget(
    *,
    gross_credit_usd: float,
    width_usd: float,
    p50_spread_usd: float,
    p75_spread_usd: float,
    sample_count: int,
    distinct_months: int,
    min_credit_to_width: float | None = None,
    p50_spread_iv_fraction: float | None = None,
    p75_spread_iv_fraction: float | None = None,
) -> VrpFreeCostBudgetResult:
    """Return the frozen free-VRP cost-budget verdict component.

    ``min_credit_to_width`` defaults to the lowest frozen grid floor in
    ``PLAN_STRUCTURE_GRID["min_credit_to_width"]``. Passing it explicitly is allowed
    only for values present in that frozen grid. Sparse calibration bins are
    fail-closed as ``INCONCLUSIVE`` and can never pass.
    """

    gross_credit_usd = _require_non_negative("gross_credit_usd", gross_credit_usd)
    width_usd = _require_positive("width_usd", width_usd)
    p50_spread_usd = _require_non_negative("p50_spread_usd", p50_spread_usd)
    p75_spread_usd = _require_non_negative("p75_spread_usd", p75_spread_usd)
    if p75_spread_usd < p50_spread_usd:
        raise ValueError("p75_spread_usd must be greater than or equal to p50_spread_usd")

    sample_count = _require_int("sample_count", sample_count)
    distinct_months = _require_int("distinct_months", distinct_months)
    min_credit_to_width = _frozen_min_credit_to_width(min_credit_to_width)

    p50_spread_vol_points = _optional_vol_points(
        "p50_spread_iv_fraction", p50_spread_iv_fraction
    )
    p75_spread_vol_points = _optional_vol_points(
        "p75_spread_iv_fraction", p75_spread_iv_fraction
    )
    if (
        p50_spread_iv_fraction is not None
        and p75_spread_iv_fraction is not None
        and p75_spread_iv_fraction < p50_spread_iv_fraction
    ):
        raise ValueError(
            "p75_spread_iv_fraction must be greater than or equal to "
            "p50_spread_iv_fraction"
        )

    p75_safety_multiplier = float(PLAN_COST_BUDGET_BAR["spread_safety_multiplier"])
    p50_margin_multiplier = float(PLAN_COST_BUDGET_BAR["median_spread_margin_multiplier"])
    min_samples_per_bin = _require_int(
        "PLAN_COST_BUDGET_BAR.min_samples_per_bin",
        PLAN_COST_BUDGET_BAR["min_samples_per_bin"],
    )
    min_distinct_months_per_bin = _require_int(
        "PLAN_COST_BUDGET_BAR.min_distinct_months_per_bin",
        PLAN_COST_BUDGET_BAR["min_distinct_months_per_bin"],
    )
    if not bool(PLAN_COST_BUDGET_BAR["require_net_credit_to_width_after_p75_safety"]):
        raise ValueError("frozen cost-budget bar must require p75-safety net credit floor")

    gross_credit_to_width = gross_credit_usd / width_usd
    required_min_credit_usd = width_usd * min_credit_to_width
    max_absorbable_round_trip_spread_usd = gross_credit_usd - required_min_credit_usd
    p75_safety_spread_usd = p75_spread_usd * p75_safety_multiplier
    p50_margin_spread_usd = p50_spread_usd * p50_margin_multiplier
    net_credit_after_p75_safety_usd = gross_credit_usd - p75_safety_spread_usd
    net_credit_after_p50_margin_usd = gross_credit_usd - p50_margin_spread_usd
    net_credit_to_width_after_p75_safety = net_credit_after_p75_safety_usd / width_usd

    sample_coverage_pass = (
        sample_count >= min_samples_per_bin
        and distinct_months >= min_distinct_months_per_bin
    )
    p75_safety_pass = max_absorbable_round_trip_spread_usd >= p75_safety_spread_usd
    p50_margin_pass = max_absorbable_round_trip_spread_usd >= p50_margin_spread_usd
    net_credit_to_width_after_p75_safety_pass = (
        net_credit_to_width_after_p75_safety >= min_credit_to_width
    )
    budget_pass = (
        sample_coverage_pass
        and p75_safety_pass
        and p50_margin_pass
        and net_credit_to_width_after_p75_safety_pass
    )

    fail_reasons = _fail_reasons(
        sample_count=sample_count,
        distinct_months=distinct_months,
        min_samples_per_bin=min_samples_per_bin,
        min_distinct_months_per_bin=min_distinct_months_per_bin,
        p75_safety_pass=p75_safety_pass,
        p50_margin_pass=p50_margin_pass,
        net_credit_to_width_after_p75_safety_pass=(
            net_credit_to_width_after_p75_safety_pass
        ),
    )
    if not sample_coverage_pass:
        status = VrpFreeCostBudgetStatus.INCONCLUSIVE
    elif budget_pass:
        status = VrpFreeCostBudgetStatus.PASS
    else:
        status = VrpFreeCostBudgetStatus.FAIL_BUDGET

    return VrpFreeCostBudgetResult(
        status=status,
        budget_pass=budget_pass,
        sample_coverage_pass=sample_coverage_pass,
        p75_safety_pass=p75_safety_pass,
        p50_margin_pass=p50_margin_pass,
        net_credit_to_width_after_p75_safety_pass=(
            net_credit_to_width_after_p75_safety_pass
        ),
        gross_credit_usd=float(gross_credit_usd),
        width_usd=float(width_usd),
        gross_credit_to_width=float(gross_credit_to_width),
        min_credit_to_width=float(min_credit_to_width),
        required_min_credit_usd=float(required_min_credit_usd),
        max_absorbable_round_trip_spread_usd=float(
            max_absorbable_round_trip_spread_usd
        ),
        p50_spread_usd=float(p50_spread_usd),
        p75_spread_usd=float(p75_spread_usd),
        p50_margin_multiplier=float(p50_margin_multiplier),
        p75_safety_multiplier=float(p75_safety_multiplier),
        p50_margin_spread_usd=float(p50_margin_spread_usd),
        p75_safety_spread_usd=float(p75_safety_spread_usd),
        net_credit_after_p50_margin_usd=float(net_credit_after_p50_margin_usd),
        net_credit_after_p75_safety_usd=float(net_credit_after_p75_safety_usd),
        net_credit_to_width_after_p75_safety=float(
            net_credit_to_width_after_p75_safety
        ),
        sample_count=sample_count,
        distinct_months=distinct_months,
        min_samples_per_bin=min_samples_per_bin,
        min_distinct_months_per_bin=min_distinct_months_per_bin,
        p50_spread_iv_fraction=p50_spread_iv_fraction,
        p75_spread_iv_fraction=p75_spread_iv_fraction,
        p50_spread_vol_points=p50_spread_vol_points,
        p75_spread_vol_points=p75_spread_vol_points,
        p50_margin_spread_vol_points=(
            None
            if p50_spread_vol_points is None
            else float(p50_spread_vol_points * p50_margin_multiplier)
        ),
        p75_safety_spread_vol_points=(
            None
            if p75_spread_vol_points is None
            else float(p75_spread_vol_points * p75_safety_multiplier)
        ),
        fail_reasons=fail_reasons,
        authorizing=False,
        capital_go_allowed=False,
        non_authorizing_reason="free_vrp_cost_budget_component_only",
    )


def spread_price_eth(*, bid_price: float, ask_price: float) -> float:
    """Return ``max(ask_price - bid_price, 0)`` in ETH option-price units."""

    _require_unit_formula("spread_price_eth")
    bid_price = _require_non_negative("bid_price", bid_price)
    ask_price = _require_non_negative("ask_price", ask_price)
    return float(max(ask_price - bid_price, 0.0))


def round_trip_leg_crossing_usd(
    *,
    bid_price: float,
    ask_price: float,
    index_price_usd: float,
    contract_multiplier: float,
    quantity: float,
) -> float:
    """Convert one leg's ETH bid/ask spread into frozen USD crossing units."""

    _require_unit_formula("round_trip_leg_crossing_usd")
    spread_eth = spread_price_eth(bid_price=bid_price, ask_price=ask_price)
    index_price_usd = _require_positive("index_price_usd", index_price_usd)
    contract_multiplier = _require_positive("contract_multiplier", contract_multiplier)
    quantity = _require_positive("quantity", quantity)
    return float(spread_eth * index_price_usd * contract_multiplier * quantity)


def round_trip_structure_spread_usd(leg_crossing_usd: Iterable[float]) -> float:
    """Sum leg USD crossing costs into frozen structure round-trip spread units."""

    _require_unit_formula("round_trip_structure_spread_usd")
    values = tuple(
        _require_non_negative("leg_crossing_usd", value) for value in leg_crossing_usd
    )
    if not values:
        raise ValueError("leg_crossing_usd must be non-empty")
    return float(sum(values))


def iv_fraction_to_vol_points(iv_fraction: float) -> float:
    """Convert an IV fraction to volatility points using the frozen plan formula."""

    _require_unit_formula("vol_points")
    iv_fraction = _require_non_negative("iv_fraction", iv_fraction)
    return float(iv_fraction * 100.0)


def frozen_min_credit_to_width_floor() -> float:
    """Return the lowest frozen ``PLAN_STRUCTURE_GRID`` credit-to-width floor."""

    return min(_frozen_min_credit_to_width_values())


def _fail_reasons(
    *,
    sample_count: int,
    distinct_months: int,
    min_samples_per_bin: int,
    min_distinct_months_per_bin: int,
    p75_safety_pass: bool,
    p50_margin_pass: bool,
    net_credit_to_width_after_p75_safety_pass: bool,
) -> tuple[str, ...]:
    reasons: list[str] = []
    if sample_count < min_samples_per_bin:
        reasons.append("sample_count_below_min_samples_per_bin")
    if distinct_months < min_distinct_months_per_bin:
        reasons.append("distinct_months_below_min_distinct_months_per_bin")
    if not p75_safety_pass:
        reasons.append("absorbable_spread_below_p75_safety_spread")
    if not p50_margin_pass:
        reasons.append("absorbable_spread_below_p50_margin_spread")
    if not net_credit_to_width_after_p75_safety_pass:
        reasons.append("net_credit_to_width_after_p75_safety_below_grid_floor")
    return tuple(reasons)


def _optional_vol_points(name: str, value: float | None) -> float | None:
    if value is None:
        return None
    return iv_fraction_to_vol_points(_require_non_negative(name, value))


def _frozen_min_credit_to_width(value: float | None) -> float:
    frozen_values = _frozen_min_credit_to_width_values()
    if value is None:
        return min(frozen_values)
    value = _require_positive("min_credit_to_width", value)
    is_frozen_value = any(
        math.isclose(value, allowed, rel_tol=0.0, abs_tol=1e-12)
        for allowed in frozen_values
    )
    if not is_frozen_value:
        allowed = ", ".join(str(allowed) for allowed in frozen_values)
        raise ValueError(
            f"min_credit_to_width must be one of frozen grid values: {allowed}"
        )
    return value


def _frozen_min_credit_to_width_values() -> tuple[float, ...]:
    raw_values = PLAN_STRUCTURE_GRID["min_credit_to_width"]
    if not isinstance(raw_values, (list, tuple)) or not raw_values:
        raise ValueError("PLAN_STRUCTURE_GRID.min_credit_to_width must be non-empty")
    return tuple(
        _require_positive("PLAN_STRUCTURE_GRID.min_credit_to_width", value)
        for value in raw_values
    )


def _require_unit_formula(key: str) -> None:
    if key not in PLAN_UNIT_CONVERSIONS:
        raise ValueError(f"PLAN_UNIT_CONVERSIONS missing {key}")


def _require_int(name: str, value: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError(f"{name} must be an integer")
    if value < 0:
        raise ValueError(f"{name} must be non-negative")
    return value


def _require_finite(name: str, value: float) -> float:
    value = float(value)
    if not math.isfinite(value):
        raise ValueError(f"{name} must be finite")
    return value


def _require_non_negative(name: str, value: float) -> float:
    value = _require_finite(name, value)
    if value < 0.0:
        raise ValueError(f"{name} must be non-negative")
    return value


def _require_positive(name: str, value: float) -> float:
    value = _require_finite(name, value)
    if value <= 0.0:
        raise ValueError(f"{name} must be positive")
    return value


__all__ = [
    "VrpFreeCostBudgetResult",
    "VrpFreeCostBudgetStatus",
    "evaluate_vrp_free_cost_budget",
    "frozen_min_credit_to_width_floor",
    "iv_fraction_to_vol_points",
    "round_trip_leg_crossing_usd",
    "round_trip_structure_spread_usd",
    "spread_price_eth",
]

"""Defined-risk option sizing and account-cap checks."""

from __future__ import annotations

import math
from dataclasses import dataclass

from ajentix_quant.backtest.option_costs import evaluate_structure_costs
from ajentix_quant.config import Settings
from ajentix_quant.options.types import DefinedRiskStructure, OptionCostBreakdown
from ajentix_quant.research.vrp_preregistration import PLAN_RISK_LIMITS

_EPSILON = 1e-12


@dataclass(frozen=True, kw_only=True)
class DefinedRiskMarginLimits:
    """Frozen Phase-1 risk caps for one account."""

    reserve_pct: float = float(PLAN_RISK_LIMITS["reserve_pct"])
    per_structure_max_loss_pct: float = float(
        PLAN_RISK_LIMITS["per_structure_max_loss_pct"]
    )
    aggregate_max_defined_risk_pct: float = float(
        PLAN_RISK_LIMITS["aggregate_max_defined_risk_pct"]
    )

    def __post_init__(self) -> None:
        _require_fraction("reserve_pct", self.reserve_pct)
        _require_fraction("per_structure_max_loss_pct", self.per_structure_max_loss_pct)
        _require_fraction(
            "aggregate_max_defined_risk_pct",
            self.aggregate_max_defined_risk_pct,
        )


@dataclass(frozen=True, kw_only=True)
class DefinedRiskMarginResult:
    """Sizing decision for a capped option structure."""

    structure_id: str
    accepted: bool
    reason: str
    requested_quantity: int
    minimum_lot_quantity: int
    max_authorized_quantity: int
    structure_max_loss_usd: float
    per_unit_max_loss_usd: float
    equity_usd: float
    reserve_usd: float
    deployable_equity_usd: float
    per_structure_cap_usd: float
    aggregate_cap_usd: float
    aggregate_used_usd: float
    aggregate_remaining_usd: float
    cost_breakdown: OptionCostBreakdown


def evaluate_defined_risk_margin(
    structure: DefinedRiskStructure,
    *,
    equity_usd: float,
    aggregate_open_max_loss_usd: float = 0.0,
    limits: DefinedRiskMarginLimits | None = None,
    settings: Settings | None = None,
    taker_fee_bps: float | None = None,
    usd_conversion_rate: float = 1.0,
) -> DefinedRiskMarginResult:
    """Evaluate max-loss, min-ticket, reserve, per-structure, and aggregate caps.

    Max loss and executable quantity come from ``evaluate_structure_costs``; this module
    only applies account-level constraints to that single cost-path output.
    """

    if not isinstance(structure, DefinedRiskStructure):
        raise ValueError("structure must be a DefinedRiskStructure")
    equity_usd = _require_positive("equity_usd", equity_usd)
    aggregate_open_max_loss_usd = _require_non_negative(
        "aggregate_open_max_loss_usd",
        aggregate_open_max_loss_usd,
    )
    settings = settings or Settings()
    limits = limits or DefinedRiskMarginLimits(reserve_pct=float(settings.reserve_pct))
    cost_breakdown = evaluate_structure_costs(
        structure,
        taker_fee_bps=taker_fee_bps,
        usd_conversion_rate=usd_conversion_rate,
        settings=settings,
    )

    effective_quantity = float(cost_breakdown.min_tick_lot_rounding["effective_quantity"])
    structure_loss = cost_breakdown.max_loss_usd
    per_unit_loss = structure_loss / effective_quantity
    minimum_lot_quantity = _minimum_lot_quantity(structure)
    minimum_lot_loss = per_unit_loss * minimum_lot_quantity

    reserve_usd = equity_usd * limits.reserve_pct
    deployable_equity_usd = equity_usd - reserve_usd
    per_structure_cap_usd = equity_usd * limits.per_structure_max_loss_pct
    aggregate_cap_usd = equity_usd * limits.aggregate_max_defined_risk_pct
    aggregate_remaining_usd = max(0.0, aggregate_cap_usd - aggregate_open_max_loss_usd)
    deployable_remaining_usd = max(0.0, deployable_equity_usd - aggregate_open_max_loss_usd)
    effective_cap_usd = min(
        per_structure_cap_usd,
        aggregate_remaining_usd,
        deployable_remaining_usd,
    )
    max_authorized_quantity = _round_down_to_lot(
        effective_cap_usd / per_unit_loss,
        minimum_lot_quantity,
    )

    accepted = True
    reason = "accepted"
    if (
        minimum_lot_loss > per_structure_cap_usd + _EPSILON
        or minimum_lot_loss > deployable_equity_usd + _EPSILON
    ):
        accepted = False
        reason = "min_lot_width_exceeds_caps"
    elif structure_loss > aggregate_remaining_usd + _EPSILON:
        accepted = False
        reason = "aggregate_cap"
    elif structure_loss > deployable_remaining_usd + _EPSILON:
        accepted = False
        reason = "reserve_cap"
    elif structure.quantity < minimum_lot_quantity:
        accepted = False
        reason = "below_min_lot"
    elif structure.quantity > max_authorized_quantity:
        accepted = False
        reason = "quantity_exceeds_cap"

    return DefinedRiskMarginResult(
        structure_id=structure.structure_id,
        accepted=accepted,
        reason=reason,
        requested_quantity=structure.quantity,
        minimum_lot_quantity=minimum_lot_quantity,
        max_authorized_quantity=max_authorized_quantity,
        structure_max_loss_usd=float(structure_loss),
        per_unit_max_loss_usd=float(per_unit_loss),
        equity_usd=float(equity_usd),
        reserve_usd=float(reserve_usd),
        deployable_equity_usd=float(deployable_equity_usd),
        per_structure_cap_usd=float(per_structure_cap_usd),
        aggregate_cap_usd=float(aggregate_cap_usd),
        aggregate_used_usd=float(aggregate_open_max_loss_usd),
        aggregate_remaining_usd=float(aggregate_remaining_usd),
        cost_breakdown=cost_breakdown,
    )


def assert_defined_risk_margin(
    structure: DefinedRiskStructure,
    *,
    equity_usd: float,
    aggregate_open_max_loss_usd: float = 0.0,
    limits: DefinedRiskMarginLimits | None = None,
    settings: Settings | None = None,
    taker_fee_bps: float | None = None,
    usd_conversion_rate: float = 1.0,
) -> DefinedRiskMarginResult:
    """Return margin result or raise ``ValueError`` with the fail-closed reason."""

    result = evaluate_defined_risk_margin(
        structure,
        equity_usd=equity_usd,
        aggregate_open_max_loss_usd=aggregate_open_max_loss_usd,
        limits=limits,
        settings=settings,
        taker_fee_bps=taker_fee_bps,
        usd_conversion_rate=usd_conversion_rate,
    )
    if not result.accepted:
        raise ValueError(result.reason)
    return result


def _minimum_lot_quantity(structure: DefinedRiskStructure) -> int:
    min_lot = max(float(leg.min_lot) for leg in structure.legs)
    _require_positive("min_lot", min_lot)
    return max(1, int(math.ceil(min_lot - _EPSILON)))


def _round_down_to_lot(quantity: float, lot: int) -> int:
    quantity = _require_non_negative("quantity", quantity)
    if lot <= 0:
        raise ValueError("lot must be positive")
    return max(0, int(math.floor(quantity / lot + _EPSILON) * lot))


def _require_fraction(name: str, value: float) -> float:
    value = _require_non_negative(name, value)
    if value >= 1.0:
        raise ValueError(f"{name} must be less than 1")
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
    "DefinedRiskMarginLimits",
    "DefinedRiskMarginResult",
    "assert_defined_risk_margin",
    "evaluate_defined_risk_margin",
]

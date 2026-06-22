"""Single authorizing cost path for defined-risk option structures.

Every sizing, breakeven, engine, stress, and final-verdict path for VRP option
economics must consume ``evaluate_structure_costs`` rather than reimplementing bid/ask,
fee, rounding, currency, or max-loss calculations.
"""

from __future__ import annotations

import hashlib
import json
import math
from typing import Any

from ajentix_quant.config import Settings
from ajentix_quant.options.types import (
    DefinedRiskStructure,
    OptionCostBreakdown,
    OptionLeg,
    Side,
    SourceQuality,
)

_BPS_DENOMINATOR = 10_000.0
_DEFAULT_OPTION_TAKER_FEE_BPS = 5.5
_DEFAULT_SAFETY_MARGIN_BPS = 1.0
_EPSILON = 1e-12
_NON_AUTHORIZING_COST_MODES = {
    "fixture",
    "maker",
    "mark",
    "marks",
    "marks_only",
    "proxy",
    "sample",
}


def evaluate_structure_costs(
    structure: DefinedRiskStructure,
    *,
    taker_fee_bps: float | None = None,
    settlement_fee_bps: float | None = None,
    safety_margin_bps: float = _DEFAULT_SAFETY_MARGIN_BPS,
    usd_conversion_rate: float = 1.0,
    settings: Settings | None = None,
    cost_mode: str = "taker",
    non_authorizing_reason: str | None = None,
) -> OptionCostBreakdown:
    """Return the authorizing ``OptionCostBreakdown`` for one defined-risk spread.

    The calculation pessimistically crosses bid/ask on every leg at entry and at the
    modeled exit, charges taker fees unless explicitly switched to a non-authorizing
    mode, records adverse min-tick/lot rounding, applies deterministic USD conversion,
    charges a settlement reserve, and hashes all assumptions.
    """

    if not isinstance(structure, DefinedRiskStructure):
        raise ValueError("structure must be a DefinedRiskStructure")
    settings = settings or Settings()
    default_taker_fee_bps = _setting(
        settings,
        "option_taker_fee_bps",
        _setting(settings, "perp_taker_fee_bps", _DEFAULT_OPTION_TAKER_FEE_BPS),
    )
    fee_bps = _require_non_negative(
        "taker_fee_bps",
        default_taker_fee_bps if taker_fee_bps is None else taker_fee_bps,
    )
    settlement_bps = fee_bps if settlement_fee_bps is None else settlement_fee_bps
    settlement_bps = _require_non_negative("settlement_fee_bps", settlement_bps)
    safety_margin_bps = _require_non_negative("safety_margin_bps", safety_margin_bps)
    usd_conversion_rate = _require_positive("usd_conversion_rate", usd_conversion_rate)
    cost_mode = _require_non_empty("cost_mode", cost_mode)

    quantity = _rounded_quantity(structure)
    entry = _side_costs(structure.legs, quantity=quantity, rate=usd_conversion_rate, exit=False)
    exit_ = _side_costs(structure.legs, quantity=quantity, rate=usd_conversion_rate, exit=True)
    net_credit_usd = entry["net_credit_usd"]
    max_loss_usd = max_loss_from_width_credit_usd(
        width=structure.width,
        net_credit=net_credit_usd / (structure.legs[0].contract_multiplier * quantity),
        contract_multiplier=structure.legs[0].contract_multiplier,
        quantity=quantity,
        usd_conversion_rate=usd_conversion_rate,
    )

    entry_fee_usd = _fee_usd(entry["notional_usd"], fee_bps)
    exit_fee_usd = _fee_usd(exit_["notional_usd"], fee_bps)
    expiry_settlement_cost = _fee_usd(max_loss_usd, settlement_bps)
    width_notional_usd = (
        structure.width * structure.legs[0].contract_multiplier * quantity * usd_conversion_rate
    )
    safety_margin = _fee_usd(width_notional_usd, safety_margin_bps)

    per_leg_crossing_cost = {
        name: float(
            entry["crossing_by_leg"].get(name, 0.0)
            + exit_["crossing_by_leg"].get(name, 0.0)
        )
        for name in sorted({leg.instrument_name for leg in structure.legs})
    }
    min_tick_lot_rounding = {
        "requested_quantity": float(structure.quantity),
        "effective_quantity": float(quantity),
        "quantity_delta": float(quantity - structure.quantity),
        **entry["rounding"],
        **{f"exit:{key}": value for key, value in exit_["rounding"].items()},
    }
    exit_net_cashflow_usd = float(exit_["net_credit_usd"])
    exit_close_debit_usd = float(-exit_net_cashflow_usd)
    fees = {
        "entry": float(entry_fee_usd),
        "exit": float(exit_fee_usd),
        "expiry_settlement": float(expiry_settlement_cost),
        "total": float(entry_fee_usd + exit_fee_usd + expiry_settlement_cost),
        "taker_fee_bps": float(fee_bps),
        "settlement_fee_bps": float(settlement_bps),
        "exit_close_debit_usd": exit_close_debit_usd,
        "exit_net_cashflow_usd": exit_net_cashflow_usd,
    }
    entry_cost = float(entry["crossing_usd"] + entry_fee_usd)
    exit_cost = float(exit_["crossing_usd"] + exit_fee_usd)
    total_cost_usd = float(entry_cost + exit_cost + expiry_settlement_cost + safety_margin)
    reason = _non_authorizing_reason(
        structure,
        cost_mode=cost_mode,
        explicit_reason=non_authorizing_reason,
    )
    assumptions = {
        "cost_mode": cost_mode,
        "entry": entry["fills"],
        "exit": exit_["fills"],
        "fees": fees,
        "max_loss_usd": max_loss_usd,
        "safety_margin_bps": safety_margin_bps,
        "structure_id": structure.structure_id,
        "usd_conversion_rate": usd_conversion_rate,
    }
    return OptionCostBreakdown(
        structure_id=structure.structure_id,
        per_leg_crossing_cost=per_leg_crossing_cost,
        fees=fees,
        min_tick_lot_rounding=min_tick_lot_rounding,
        usd_conversion={
            "rate": float(usd_conversion_rate),
            "source": structure.usd_conversion_source,
            "premium_currency": structure.premium_currency,
            "fee_currency": structure.fee_currency,
            "collateral_currency": structure.collateral_currency,
        },
        entry_cost=entry_cost,
        exit_cost=exit_cost,
        expiry_settlement_cost=float(expiry_settlement_cost),
        safety_margin=float(safety_margin),
        total_cost_usd=total_cost_usd,
        net_credit_usd=float(net_credit_usd),
        max_loss_usd=float(max_loss_usd),
        assumptions_hash=_hash_assumptions(assumptions),
        authorizing=reason is None,
        non_authorizing_reason=reason,
    )


def evaluate_structure_exit_costs(
    structure: DefinedRiskStructure,
    *,
    taker_fee_bps: float | None = None,
    settlement_fee_bps: float | None = None,
    safety_margin_bps: float = _DEFAULT_SAFETY_MARGIN_BPS,
    usd_conversion_rate: float = 1.0,
    settings: Settings | None = None,
    cost_mode: str = "taker",
    non_authorizing_reason: str | None = None,
) -> OptionCostBreakdown:
    """Return authoritative close/exit economics for a quoted spread snapshot.

    ``fees["exit_close_debit_usd"]`` is derived from the same exit fills, tick
    rounding, lot sizing, fee assumptions, and USD conversion as the authorizing cost
    path. Engine realized exit PnL must consume that breakdown rather than recompute
    close fills independently.
    """

    return evaluate_structure_costs(
        structure,
        taker_fee_bps=taker_fee_bps,
        settlement_fee_bps=settlement_fee_bps,
        safety_margin_bps=safety_margin_bps,
        usd_conversion_rate=usd_conversion_rate,
        settings=settings,
        cost_mode=cost_mode,
        non_authorizing_reason=non_authorizing_reason,
    )


def close_debit_usd_from_cost_breakdown(breakdown: OptionCostBreakdown) -> float:
    """Extract the executable close debit recorded by ``evaluate_structure_exit_costs``."""

    if not isinstance(breakdown, OptionCostBreakdown):
        raise ValueError("breakdown must be an OptionCostBreakdown")
    if "exit_close_debit_usd" not in breakdown.fees:
        raise ValueError("breakdown is missing exit_close_debit_usd")
    return _require_finite(
        "exit_close_debit_usd",
        float(breakdown.fees["exit_close_debit_usd"]),
    )


def max_loss_from_width_credit_usd(
    *,
    width: float,
    net_credit: float,
    contract_multiplier: float,
    quantity: float,
    usd_conversion_rate: float = 1.0,
) -> float:
    """Return capped spread max loss from width and executable credit.

    This helper lives beside the authorizing cost path so strategy and margin code do not
    carry a parallel max-loss formula.
    """

    width = _require_positive("width", width)
    net_credit = _require_positive("net_credit", net_credit)
    contract_multiplier = _require_positive("contract_multiplier", contract_multiplier)
    quantity = _require_positive("quantity", quantity)
    usd_conversion_rate = _require_positive("usd_conversion_rate", usd_conversion_rate)
    if net_credit >= width * usd_conversion_rate + _EPSILON:
        raise ValueError("net_credit must be less than width")
    return float((width * usd_conversion_rate - net_credit) * contract_multiplier * quantity)


def _side_costs(
    legs: tuple[OptionLeg, ...],
    *,
    quantity: float,
    rate: float,
    exit: bool,
) -> dict[str, Any]:
    crossing_by_leg: dict[str, float] = {}
    fills: dict[str, float] = {}
    rounding: dict[str, float] = {}
    notional_usd = 0.0
    crossing_usd = 0.0
    net_credit_usd = 0.0

    for leg in legs:
        action = _action_for_leg(leg, exit=exit)
        raw_price = leg.ask_price if action == "buy" else leg.bid_price
        fill_price = _round_price(raw_price, leg.min_tick, action=action)
        midpoint = (leg.bid_price + leg.ask_price) / 2.0
        leg_notional = fill_price * leg.contract_multiplier * quantity * rate
        leg_crossing = _crossing_cost(
            midpoint=midpoint,
            fill_price=fill_price,
            action=action,
            multiplier=leg.contract_multiplier,
            quantity=quantity,
            rate=rate,
        )
        crossing_by_leg[leg.instrument_name] = float(leg_crossing)
        fills[leg.instrument_name] = float(fill_price)
        rounding[f"{leg.instrument_name}:price_delta"] = float(fill_price - raw_price)
        notional_usd += leg_notional
        crossing_usd += leg_crossing
        net_credit_usd += (1.0 if action == "sell" else -1.0) * leg_notional

    return {
        "crossing_by_leg": crossing_by_leg,
        "crossing_usd": float(crossing_usd),
        "fills": fills,
        "net_credit_usd": float(net_credit_usd),
        "notional_usd": float(notional_usd),
        "rounding": rounding,
    }


def _action_for_leg(leg: OptionLeg, *, exit: bool) -> str:
    if not exit:
        return "sell" if leg.side is Side.SHORT else "buy"
    return "buy" if leg.side is Side.SHORT else "sell"


def _rounded_quantity(structure: DefinedRiskStructure) -> float:
    min_lot = max(float(leg.min_lot) for leg in structure.legs)
    if min_lot <= 0.0:
        raise ValueError("min_lot must be positive")
    qty = float(structure.quantity)
    return float(round(math.ceil((qty - _EPSILON) / min_lot) * min_lot, 12))


def _round_price(price: float, tick: float, *, action: str) -> float:
    price = _require_non_negative("price", price)
    tick = _require_positive("tick", tick)
    if action == "buy":
        rounded = math.ceil((price - _EPSILON) / tick) * tick
    elif action == "sell":
        rounded = math.floor((price + _EPSILON) / tick) * tick
    else:
        raise ValueError("action must be buy or sell")
    return float(round(max(0.0, rounded), 12))


def _crossing_cost(
    *,
    midpoint: float,
    fill_price: float,
    action: str,
    multiplier: float,
    quantity: float,
    rate: float,
) -> float:
    midpoint = _require_non_negative("midpoint", midpoint)
    fill_price = _require_non_negative("fill_price", fill_price)
    multiplier = _require_positive("multiplier", multiplier)
    quantity = _require_positive("quantity", quantity)
    rate = _require_positive("rate", rate)
    if action == "buy":
        per_contract = max(0.0, fill_price - midpoint)
    elif action == "sell":
        per_contract = max(0.0, midpoint - fill_price)
    else:
        raise ValueError("action must be buy or sell")
    return float(per_contract * multiplier * quantity * rate)


def _fee_usd(notional_usd: float, fee_bps: float) -> float:
    return float(_require_non_negative("notional_usd", notional_usd) * fee_bps / _BPS_DENOMINATOR)


def _non_authorizing_reason(
    structure: DefinedRiskStructure,
    *,
    cost_mode: str,
    explicit_reason: str | None,
) -> str | None:
    if explicit_reason is not None:
        return _canonical_reason(explicit_reason)
    mode = cost_mode.lower()
    if mode in _NON_AUTHORIZING_COST_MODES:
        return _canonical_reason(mode)
    if mode != "taker":
        return _canonical_reason(mode)
    for leg in structure.legs:
        if leg.source_quality is SourceQuality.FIXTURE:
            return "fixture"
        if leg.source_quality is SourceQuality.PROXY:
            return "proxy"
        if leg.source_quality is SourceQuality.ABSENT:
            return "absent"
        if leg.source_quality is SourceQuality.FROZEN_SNAPSHOT:
            return "frozen_snapshot"
    return None


def _canonical_reason(reason: str) -> str:
    reason = _require_non_empty("non_authorizing_reason", reason).lower()
    if reason in {"mark", "marks"}:
        return "marks_only"
    return reason


def _hash_assumptions(assumptions: dict[str, Any]) -> str:
    canonical = json.dumps(assumptions, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _setting(settings: Settings, name: str, default: float) -> float:
    return float(getattr(settings, name, default))


def _require_non_empty(name: str, value: str) -> str:
    if not isinstance(value, str) or not value:
        raise ValueError(f"{name} must be a non-empty string")
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
    "close_debit_usd_from_cost_breakdown",
    "evaluate_structure_costs",
    "evaluate_structure_exit_costs",
    "max_loss_from_width_credit_usd",
]

"""Non-authorizing USD-consistent VRP defined-risk construction (evaluation only).

This is a deliberate sibling of the frozen ``vrp_defined_risk`` strategy. The frozen strategy is
immutable and is NOT modified here. Its entry gate compares an ETH-denominated credit against a
USD width with no conversion, so on the real reconstructed (ETH-premium) cache it rejects every
structure and the official walk-forward selects zero structures in all folds. To *measure* the
skew edge that bug left unmeasured, this module runs the identical search space, leg selection,
delta logic, and ``credit / width`` gate on USD-projected snapshots (see
``ajentix_quant.options.usd_projection``), where the gate is finally dimensionally consistent
(USD credit vs USD width).

The only intentional differences from the frozen strategy are:

  1. it requires the eval USD settlement profile (``premium_currency == "USD"`` etc.) instead of
     the frozen ETH profile, so it accepts USD-projected legs and rejects ETH-labelled ones; and
  2. because its inputs are USD-projected, the same ``credit / width`` gate now compares like
     units.

Leg selection (eligibility, nearest expiry, delta-target short leg, width-target long leg) reuses
the frozen helpers verbatim so the measurement is faithful. Nothing here authorizes capital: the
source quality (reconstructed chains + effective-spread cost proxy) precludes a capital GO. Every
emitted artifact is labelled non-authorizing.
"""

from __future__ import annotations

import enum
from dataclasses import dataclass, replace
from typing import Any

from ajentix_quant.backtest.option_costs import max_loss_from_width_credit_usd
from ajentix_quant.options.types import (
    DefinedRiskStructure,
    OptionChainSnapshot,
    OptionLeg,
    Side,
    StructureType,
)
from ajentix_quant.options.usd_projection import (
    EVAL_FEE_CURRENCY,
    EVAL_PREMIUM_CURRENCY,
    USD_PROJECTION_SOURCE,
)
from ajentix_quant.research.vrp_preregistration import PLAN_SETTLEMENT, PLAN_STRUCTURE_GRID
from ajentix_quant.strategies.vrp_defined_risk import (
    _AUTHORIZING_GREEK_SELECTION_SOURCE,
    _DEFAULT_MAX_QUOTE_AGE_S,
    _DIAGNOSTIC_GREEK_SELECTION_SOURCE,
    _MS_PER_DAY,
    _leg_is_eligible,
    _nearest_expiry,
    _option_type_for_structure,
    _select_long_leg,
    _select_short_leg,
    _snapshot_spot,
    _snapshot_vendor_delta_map,
)

# Eval settlement profile: identical to the frozen plan except the premium/fee currency and the
# conversion source, which are USD because the inputs are USD-projected. Kept as a separate
# constant so the eval never silently inherits a future change to the frozen plan.
EVAL_SETTLEMENT: dict[str, Any] = {
    **PLAN_SETTLEMENT,
    "premium_currency": EVAL_PREMIUM_CURRENCY,
    "fee_currency": EVAL_FEE_CURRENCY,
    "usd_conversion_source": USD_PROJECTION_SOURCE,
}

EVAL_NON_AUTHORIZING_LABEL = "usd_consistent_eval_non_authorizing"

__all__ = [
    "EVAL_NON_AUTHORIZING_LABEL",
    "EVAL_SETTLEMENT",
    "VrpDefinedRiskUsdEvalStrategy",
    "VrpExitAction",
    "construct_usd_eval_structures",
]


class VrpExitAction(enum.StrEnum):
    TAKE_PROFIT = "take_profit"
    STOP_LOSS = "stop_loss"
    HOLD_TO_SETTLEMENT = "hold_to_settlement"


@dataclass(frozen=True, kw_only=True)
class VrpDefinedRiskUsdEvalStrategy:
    """USD-consistent, non-authorizing sibling of the frozen VRP defined-risk strategy."""

    max_quote_age_s: float = _DEFAULT_MAX_QUOTE_AGE_S
    grid: dict[str, Any] | None = None
    allow_diagnostic_greek_selection: bool = True

    def construct_structures(
        self,
        snapshot: OptionChainSnapshot,
    ) -> tuple[DefinedRiskStructure, ...]:
        """Return deterministic eligible USD-settled structures from one USD-projected snapshot."""

        if not isinstance(snapshot, OptionChainSnapshot):
            raise ValueError("snapshot must be an OptionChainSnapshot")
        if snapshot.underlying != "ETH":
            return ()
        spot = _snapshot_spot(snapshot)
        if spot is None:
            return ()
        eligible_legs = tuple(
            leg for leg in snapshot.legs if _leg_is_eligible(leg, self.max_quote_age_s)
        )
        if not eligible_legs:
            return ()

        grid = self.grid or PLAN_STRUCTURE_GRID
        deltas = tuple(float(value) for value in grid["short_leg_abs_delta"])
        widths = tuple(float(value) for value in grid["width_usd"])
        min_credit_filters = tuple(float(value) for value in grid["min_credit_to_width"])
        dte_targets = tuple(int(value) for value in grid["dte_targets"])
        structure_types = tuple(StructureType(value) for value in grid["structure_types"])
        delta_by_instrument = _snapshot_vendor_delta_map(snapshot)
        if not delta_by_instrument and not self.allow_diagnostic_greek_selection:
            return ()
        greek_selection_source = (
            _DIAGNOSTIC_GREEK_SELECTION_SOURCE
            if self.allow_diagnostic_greek_selection
            else _AUTHORIZING_GREEK_SELECTION_SOURCE
        )

        out: list[DefinedRiskStructure] = []
        seen: set[str] = set()
        for structure_type in structure_types:
            option_type = _option_type_for_structure(structure_type)
            side_legs = tuple(leg for leg in eligible_legs if leg.option_type is option_type)
            if not side_legs:
                continue
            for dte_target in dte_targets:
                expiry_ms = _nearest_expiry(side_legs, snapshot.snapshot_ts_ms, dte_target)
                if expiry_ms is None:
                    continue
                expiry_legs = tuple(leg for leg in side_legs if leg.expiry_ms == expiry_ms)
                for delta_target in deltas:
                    short_base = _select_short_leg(
                        expiry_legs,
                        structure_type=structure_type,
                        delta_target=delta_target,
                        spot=spot,
                        snapshot_ts_ms=snapshot.snapshot_ts_ms,
                        delta_by_instrument=delta_by_instrument,
                        allow_diagnostic_greek_selection=self.allow_diagnostic_greek_selection,
                    )
                    if short_base is None:
                        continue
                    for width_target in widths:
                        long_base = _select_long_leg(
                            expiry_legs,
                            structure_type=structure_type,
                            short_leg=short_base,
                            width_target=width_target,
                        )
                        if long_base is None:
                            continue
                        for min_credit_to_width in min_credit_filters:
                            structure = _build_structure_usd(
                                snapshot,
                                structure_type=structure_type,
                                short_leg=replace(short_base, side=Side.SHORT),
                                long_leg=replace(long_base, side=Side.LONG),
                                dte_target=dte_target,
                                delta_target=delta_target,
                                width_target=width_target,
                                min_credit_to_width=min_credit_to_width,
                                greek_selection_source=greek_selection_source,
                            )
                            if structure is None:
                                continue
                            if structure.structure_id in seen:
                                continue
                            seen.add(structure.structure_id)
                            out.append(structure)
        return tuple(out)


def construct_usd_eval_structures(
    snapshot: OptionChainSnapshot,
    *,
    max_quote_age_s: float = _DEFAULT_MAX_QUOTE_AGE_S,
) -> tuple[DefinedRiskStructure, ...]:
    """Convenience wrapper for the USD-consistent non-authorizing eval strategy."""

    return VrpDefinedRiskUsdEvalStrategy(max_quote_age_s=max_quote_age_s).construct_structures(
        snapshot
    )


def _settlement_matches_eval_plan(short_leg: OptionLeg, long_leg: OptionLeg) -> bool:
    return all(
        (
            short_leg.settlement_style == EVAL_SETTLEMENT["style"],
            long_leg.settlement_style == EVAL_SETTLEMENT["style"],
            short_leg.settlement_index == EVAL_SETTLEMENT["settlement_index"],
            long_leg.settlement_index == EVAL_SETTLEMENT["settlement_index"],
            short_leg.premium_currency == EVAL_SETTLEMENT["premium_currency"],
            long_leg.premium_currency == EVAL_SETTLEMENT["premium_currency"],
            short_leg.fee_currency == EVAL_SETTLEMENT["fee_currency"],
            long_leg.fee_currency == EVAL_SETTLEMENT["fee_currency"],
            short_leg.collateral_currency == EVAL_SETTLEMENT["collateral_currency"],
            long_leg.collateral_currency == EVAL_SETTLEMENT["collateral_currency"],
            short_leg.usd_conversion_source == EVAL_SETTLEMENT["usd_conversion_source"],
            long_leg.usd_conversion_source == EVAL_SETTLEMENT["usd_conversion_source"],
            short_leg.contract_multiplier == float(EVAL_SETTLEMENT["contract_multiplier"]),
            long_leg.contract_multiplier == float(EVAL_SETTLEMENT["contract_multiplier"]),
        )
    )


def _build_structure_usd(
    snapshot: OptionChainSnapshot,
    *,
    structure_type: StructureType,
    short_leg: OptionLeg,
    long_leg: OptionLeg,
    dte_target: int,
    delta_target: float,
    width_target: float,
    min_credit_to_width: float,
    greek_selection_source: str,
) -> DefinedRiskStructure | None:
    width = abs(short_leg.strike - long_leg.strike)
    if width <= 0.0:
        return None
    net_credit = short_leg.bid_price - long_leg.ask_price
    if net_credit <= 0.0:
        return None
    if net_credit / width < min_credit_to_width:
        return None
    if not _settlement_matches_eval_plan(short_leg, long_leg):
        return None
    multiplier = short_leg.contract_multiplier
    quantity = 1
    max_loss = max_loss_from_width_credit_usd(
        width=width,
        net_credit=net_credit,
        contract_multiplier=multiplier,
        quantity=quantity,
    )
    dte_days = int(round((short_leg.expiry_ms - snapshot.snapshot_ts_ms) / _MS_PER_DAY))
    return DefinedRiskStructure(
        structure_type=structure_type,
        legs=(short_leg, long_leg),
        quantity=quantity,
        entry_snapshot_id=(
            f"{snapshot.scenario_id}:{snapshot.snapshot_ts_ms}:{snapshot.manifest_sha256}"
        ),
        expiry_ms=short_leg.expiry_ms,
        dte_days=dte_days,
        settlement_style=short_leg.settlement_style,
        settlement_index=short_leg.settlement_index,
        premium_currency=short_leg.premium_currency,
        fee_currency=short_leg.fee_currency,
        collateral_currency=short_leg.collateral_currency,
        usd_conversion_source=short_leg.usd_conversion_source,
        net_credit=float(net_credit),
        width=float(width),
        fees=0.0,
        max_loss_usd=float(max_loss),
        max_gain_usd=float(net_credit * multiplier * quantity),
        entry_quote_ts_ms=max(short_leg.quote_ts_ms, long_leg.quote_ts_ms),
        max_quote_age_s=max(short_leg.quote_age_s, long_leg.quote_age_s),
        frozen_param_key=(
            f"{PLAN_STRUCTURE_GRID['search_space_version']}|{structure_type.value}|"
            f"dte={dte_target}|delta={delta_target:.2f}|width={width_target:.0f}|"
            f"min_credit={min_credit_to_width:.2f}|"
            f"greeks={greek_selection_source}|rolls=false"
        ),
    )

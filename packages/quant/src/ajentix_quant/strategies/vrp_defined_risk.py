"""Deterministic ETH defined-risk VRP credit-spread construction."""

from __future__ import annotations

import enum
import math
from dataclasses import dataclass, replace
from typing import Any

from ajentix_quant.backtest.option_costs import max_loss_from_width_credit_usd
from ajentix_quant.options.types import (
    DefinedRiskStructure,
    OptionChainSnapshot,
    OptionLeg,
    OptionType,
    Side,
    SourceQuality,
    StructureType,
)
from ajentix_quant.options.valuation import diagnostic_value_greeks_from_leg
from ajentix_quant.research.vrp_preregistration import PLAN_SETTLEMENT, PLAN_STRUCTURE_GRID

_AUTHORIZING_GREEK_SELECTION_SOURCE = "vendor_delta_by_instrument_authorizing"
_DIAGNOSTIC_GREEK_SELECTION_SOURCE = (
    "local_black_scholes_diagnostic_only_non_authorizing"
)

_MS_PER_DAY = 86_400_000
_DEFAULT_MAX_QUOTE_AGE_S = 60.0
_ALLOWED_SOURCE_QUALITY = (SourceQuality.VENUE, SourceQuality.FIXTURE)


class VrpExitAction(enum.StrEnum):
    TAKE_PROFIT = "take_profit"
    STOP_LOSS = "stop_loss"
    HOLD_TO_SETTLEMENT = "hold_to_settlement"


@dataclass(frozen=True, kw_only=True)
class VrpDefinedRiskStrategy:
    """Pure strategy that constructs capped ETH put/call credit spreads."""

    max_quote_age_s: float = _DEFAULT_MAX_QUOTE_AGE_S
    grid: dict[str, Any] | None = None
    allow_diagnostic_greek_selection: bool = False

    def construct_structures(
        self,
        snapshot: OptionChainSnapshot,
    ) -> tuple[DefinedRiskStructure, ...]:
        """Return deterministic eligible structures from one normalized chain snapshot."""

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
                        allow_diagnostic_greek_selection=(
                            self.allow_diagnostic_greek_selection
                        ),
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
                            structure = _build_structure(
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

    def exit_action(
        self,
        *,
        entry_credit_usd: float,
        close_debit_usd: float | None,
        bid_ask_available: bool,
    ) -> VrpExitAction:
        """Frozen exit rule: 50% credit capture, 2x stop, otherwise settlement."""

        entry_credit_usd = _require_positive("entry_credit_usd", entry_credit_usd)
        if not bid_ask_available or close_debit_usd is None:
            return VrpExitAction.HOLD_TO_SETTLEMENT
        close_debit_usd = _require_non_negative("close_debit_usd", close_debit_usd)
        exit_rule = (self.grid or PLAN_STRUCTURE_GRID)["exit_rule"]
        if close_debit_usd <= entry_credit_usd * (1.0 - float(exit_rule["profit_take_frac"])):
            return VrpExitAction.TAKE_PROFIT
        if close_debit_usd >= entry_credit_usd * float(exit_rule["stop_loss_credit_mult"]):
            return VrpExitAction.STOP_LOSS
        return VrpExitAction.HOLD_TO_SETTLEMENT


def construct_vrp_defined_risk_structures(
    snapshot: OptionChainSnapshot,
    *,
    max_quote_age_s: float = _DEFAULT_MAX_QUOTE_AGE_S,
) -> tuple[DefinedRiskStructure, ...]:
    """Convenience wrapper for the frozen VRP defined-risk strategy."""

    return VrpDefinedRiskStrategy(max_quote_age_s=max_quote_age_s).construct_structures(snapshot)


def _build_structure(
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
    if not _settlement_matches_plan(short_leg, long_leg):
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
            f"{snapshot.scenario_id}:{snapshot.snapshot_ts_ms}:"
            f"{snapshot.manifest_sha256}"
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


def _select_short_leg(
    legs: tuple[OptionLeg, ...],
    *,
    structure_type: StructureType,
    delta_target: float,
    spot: float,
    snapshot_ts_ms: int,
    delta_by_instrument: dict[str, float],
    allow_diagnostic_greek_selection: bool,
) -> OptionLeg | None:
    candidates: list[tuple[float, float, float, str, OptionLeg]] = []
    for leg in legs:
        if structure_type is StructureType.PUT_CREDIT_SPREAD and leg.strike >= spot:
            continue
        if structure_type is StructureType.CALL_CREDIT_SPREAD and leg.strike <= spot:
            continue
        delta = _leg_delta(
            leg,
            spot=spot,
            snapshot_ts_ms=snapshot_ts_ms,
            delta_by_instrument=delta_by_instrument,
            allow_diagnostic_greek_selection=allow_diagnostic_greek_selection,
        )
        if delta is None:
            continue
        candidates.append(
            (
                abs(abs(delta) - delta_target),
                abs(leg.strike - spot),
                leg.strike,
                leg.instrument_name,
                leg,
            )
        )
    if not candidates:
        return None
    return min(candidates)[4]


def _select_long_leg(
    legs: tuple[OptionLeg, ...],
    *,
    structure_type: StructureType,
    short_leg: OptionLeg,
    width_target: float,
) -> OptionLeg | None:
    target = (
        short_leg.strike - width_target
        if structure_type is StructureType.PUT_CREDIT_SPREAD
        else short_leg.strike + width_target
    )
    candidates: list[tuple[float, float, str, OptionLeg]] = []
    for leg in legs:
        if leg.instrument_name == short_leg.instrument_name:
            continue
        if structure_type is StructureType.PUT_CREDIT_SPREAD:
            if leg.strike >= short_leg.strike:
                continue
            tie = -leg.strike
        else:
            if leg.strike <= short_leg.strike:
                continue
            tie = leg.strike
        candidates.append((abs(leg.strike - target), tie, leg.instrument_name, leg))
    if not candidates:
        return None
    return min(candidates)[3]


def _leg_delta(
    leg: OptionLeg,
    *,
    spot: float,
    snapshot_ts_ms: int,
    delta_by_instrument: dict[str, float],
    allow_diagnostic_greek_selection: bool,
) -> float | None:
    if leg.instrument_name in delta_by_instrument:
        return delta_by_instrument[leg.instrument_name]
    if not allow_diagnostic_greek_selection:
        return None
    try:
        return diagnostic_value_greeks_from_leg(
            leg,
            snapshot_ts_ms=snapshot_ts_ms,
            underlying_price=spot,
        ).delta
    except ValueError:
        return None


def _snapshot_vendor_delta_map(snapshot: OptionChainSnapshot) -> dict[str, float]:
    raw = snapshot.usd_conversion_inputs.get("vendor_delta_by_instrument")
    if isinstance(raw, dict):
        return {str(name): float(delta) for name, delta in raw.items()}
    return {}


def _nearest_expiry(
    legs: tuple[OptionLeg, ...],
    snapshot_ts_ms: int,
    dte_target: int,
) -> int | None:
    expiries = sorted({leg.expiry_ms for leg in legs if leg.expiry_ms > snapshot_ts_ms})
    if not expiries:
        return None
    target = snapshot_ts_ms + dte_target * _MS_PER_DAY
    return min(expiries, key=lambda expiry: (abs(expiry - target), expiry))


def _option_type_for_structure(structure_type: StructureType) -> OptionType:
    if structure_type is StructureType.PUT_CREDIT_SPREAD:
        return OptionType.PUT
    if structure_type is StructureType.CALL_CREDIT_SPREAD:
        return OptionType.CALL
    raise ValueError(f"unsupported structure_type: {structure_type}")


def _snapshot_spot(snapshot: OptionChainSnapshot) -> float | None:
    spot = snapshot.index_price or snapshot.settlement_index_price
    if spot is None or not math.isfinite(spot) or spot <= 0.0:
        return None
    return float(spot)


def _leg_is_eligible(leg: OptionLeg, max_quote_age_s: float) -> bool:
    if leg.source_quality not in _ALLOWED_SOURCE_QUALITY:
        return False
    if leg.quote_age_s > max_quote_age_s:
        return False
    if leg.bid_amount <= 0.0 or leg.ask_amount <= 0.0:
        return False
    if leg.bid_price <= 0.0 or leg.ask_price <= 0.0:
        return False
    if leg.bid_price > leg.ask_price:
        return False
    return True


def _settlement_matches_plan(short_leg: OptionLeg, long_leg: OptionLeg) -> bool:
    return all(
        (
            short_leg.settlement_style == PLAN_SETTLEMENT["style"],
            long_leg.settlement_style == PLAN_SETTLEMENT["style"],
            short_leg.settlement_index == PLAN_SETTLEMENT["settlement_index"],
            long_leg.settlement_index == PLAN_SETTLEMENT["settlement_index"],
            short_leg.premium_currency == PLAN_SETTLEMENT["premium_currency"],
            long_leg.premium_currency == PLAN_SETTLEMENT["premium_currency"],
            short_leg.fee_currency == PLAN_SETTLEMENT["fee_currency"],
            long_leg.fee_currency == PLAN_SETTLEMENT["fee_currency"],
            short_leg.collateral_currency == PLAN_SETTLEMENT["collateral_currency"],
            long_leg.collateral_currency == PLAN_SETTLEMENT["collateral_currency"],
            short_leg.usd_conversion_source == PLAN_SETTLEMENT["usd_conversion_source"],
            long_leg.usd_conversion_source == PLAN_SETTLEMENT["usd_conversion_source"],
            math.isclose(
                short_leg.contract_multiplier,
                float(PLAN_SETTLEMENT["contract_multiplier"]),
                rel_tol=1e-12,
            ),
            math.isclose(
                long_leg.contract_multiplier,
                float(PLAN_SETTLEMENT["contract_multiplier"]),
                rel_tol=1e-12,
            ),
        )
    )


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
    "VrpDefinedRiskStrategy",
    "VrpExitAction",
    "construct_vrp_defined_risk_structures",
]

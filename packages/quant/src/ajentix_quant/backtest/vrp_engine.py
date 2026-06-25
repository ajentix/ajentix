"""Deterministic defined-risk VRP option replay engine.

The engine is deliberately small: callers supply frozen structures and no-network
``OptionChainSnapshot`` values.  Entry cost/max-loss facts are always sourced from
``evaluate_structure_costs``; bid/ask snapshots are used only to close or settle the
already-defined capped spread, with every realized/stress PnL bounded by that cost-path
max loss.
"""

from __future__ import annotations

import math
from collections.abc import Sequence
from dataclasses import dataclass, replace
from typing import Any

from ajentix_quant.backtest.metrics import max_drawdown
from ajentix_quant.backtest.option_costs import (
    close_debit_usd_from_cost_breakdown,
    evaluate_structure_costs,
    evaluate_structure_exit_costs,
)
from ajentix_quant.options.types import (
    DefinedRiskStructure,
    OptionChainSnapshot,
    OptionCostBreakdown,
    OptionLeg,
    OptionType,
    Side,
)

VRP_ENGINE_SCHEMA_VERSION = "vrp-engine-v1"
_EPSILON = 1e-9


@dataclass(frozen=True, kw_only=True)
class VrpBacktestStep:
    """One deterministic entry followed by exit, expiry, and optional stress marks."""

    entry_timestamp_ms: int
    structure: DefinedRiskStructure
    entry_snapshot: OptionChainSnapshot
    exit_timestamp_ms: int | None = None
    exit_snapshot: OptionChainSnapshot | None = None
    settlement_price: float | None = None
    stress_settlement_prices: tuple[float, ...] = ()
    cost_mode: str = "taker"
    taker_fee_bps: float | None = None
    usd_conversion_rate: float = 1.0


@dataclass(frozen=True, kw_only=True)
class VrpLedgerEvent:
    """Auditable ledger row for entry, exit/expiry, and stress checks."""

    event_type: str
    timestamp_ms: int
    structure_id: str
    reason: str
    pnl_usd: float
    equity_before_usd: float
    equity_after_usd: float
    max_loss_usd: float
    cost_assumptions_hash: str
    invariant_ok: bool
    stress: bool = False

    def as_dict(self) -> dict[str, Any]:
        return {
            "event_type": self.event_type,
            "timestamp_ms": self.timestamp_ms,
            "structure_id": self.structure_id,
            "reason": self.reason,
            "pnl_usd": self.pnl_usd,
            "equity_before_usd": self.equity_before_usd,
            "equity_after_usd": self.equity_after_usd,
            "max_loss_usd": self.max_loss_usd,
            "cost_assumptions_hash": self.cost_assumptions_hash,
            "invariant_ok": self.invariant_ok,
            "stress": self.stress,
        }


@dataclass(frozen=True, kw_only=True)
class VrpBacktestResult:
    """Pure replay result with max-loss evidence."""

    schema_version: str
    initial_equity_usd: float
    final_equity_usd: float
    realized_pnl_usd: float
    n_entries: int
    n_exits: int
    n_expiries: int
    n_stress_events: int
    max_drawdown: float
    max_drawdown_including_stress: float
    max_loss_invariant_ok: bool
    events: tuple[VrpLedgerEvent, ...]
    cost_breakdowns: tuple[OptionCostBreakdown, ...]

    def as_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "initial_equity_usd": self.initial_equity_usd,
            "final_equity_usd": self.final_equity_usd,
            "realized_pnl_usd": self.realized_pnl_usd,
            "n_entries": self.n_entries,
            "n_exits": self.n_exits,
            "n_expiries": self.n_expiries,
            "n_stress_events": self.n_stress_events,
            "max_drawdown": self.max_drawdown,
            "max_drawdown_including_stress": self.max_drawdown_including_stress,
            "max_loss_invariant_ok": self.max_loss_invariant_ok,
            "events": [event.as_dict() for event in self.events],
            "cost_assumption_hashes": [
                breakdown.assumptions_hash for breakdown in self.cost_breakdowns
            ],
        }


def run_vrp_backtest(
    steps: Sequence[VrpBacktestStep],
    *,
    initial_equity_usd: float = 1_000.0,
) -> VrpBacktestResult:
    """Replay supplied structures deterministically in timestamp order."""

    equity = _require_positive("initial_equity_usd", initial_equity_usd)
    curve = [equity]
    stress_curve = [equity]
    events: list[VrpLedgerEvent] = []
    breakdowns: list[OptionCostBreakdown] = []
    n_exits = 0
    n_expiries = 0

    ordered = sorted(steps, key=lambda step: (step.entry_timestamp_ms, step.structure.structure_id))
    for step in ordered:
        entry_structure = _quote_structure(step.structure, step.entry_snapshot)
        entry_breakdown = evaluate_structure_costs(
            entry_structure,
            taker_fee_bps=step.taker_fee_bps,
            usd_conversion_rate=step.usd_conversion_rate,
            cost_mode=step.cost_mode,
        )
        breakdowns.append(entry_breakdown)
        entry_event = _event(
            event_type="entry",
            timestamp_ms=step.entry_timestamp_ms,
            structure_id=step.structure.structure_id,
            reason="entry_cost_path_evaluated",
            pnl_usd=0.0,
            equity_before=equity,
            max_loss_usd=entry_breakdown.max_loss_usd,
            cost_hash=entry_breakdown.assumptions_hash,
            stress=False,
        )
        events.append(entry_event)

        for stress_price in step.stress_settlement_prices:
            stress_pnl = _bounded_pnl(
                _settlement_pnl_usd(
                    step.structure,
                    entry_breakdown,
                    settlement_price=stress_price,
                    usd_conversion_rate=step.usd_conversion_rate,
                    include_settlement_fee=True,
                ),
                entry_breakdown.max_loss_usd,
            )
            stress_after = equity + stress_pnl
            stress_curve.append(stress_after)
            events.append(
                _event(
                    event_type="stress",
                    timestamp_ms=step.entry_timestamp_ms,
                    structure_id=step.structure.structure_id,
                    reason=f"stress_settlement_price={stress_price:.12g}",
                    pnl_usd=stress_pnl,
                    equity_before=equity,
                    max_loss_usd=entry_breakdown.max_loss_usd,
                    cost_hash=entry_breakdown.assumptions_hash,
                    stress=True,
                )
            )

        equity_before_exit = equity
        if step.exit_snapshot is not None:
            exit_structure = _quote_structure(step.structure, step.exit_snapshot)
            exit_breakdown = evaluate_structure_exit_costs(
                exit_structure,
                taker_fee_bps=step.taker_fee_bps,
                usd_conversion_rate=step.usd_conversion_rate,
                cost_mode=step.cost_mode,
            )
            breakdowns.append(exit_breakdown)
            close_debit = close_debit_usd_from_cost_breakdown(exit_breakdown)
            pnl = _bounded_pnl(
                entry_breakdown.net_credit_usd
                - close_debit
                - _realized_exit_fee_reserve(entry_breakdown, exit_breakdown),
                entry_breakdown.max_loss_usd,
            )
            equity += pnl
            n_exits += 1
            events.append(
                _event(
                    event_type="exit",
                    timestamp_ms=step.exit_timestamp_ms or step.exit_snapshot.snapshot_ts_ms,
                    structure_id=step.structure.structure_id,
                    reason="bid_ask_close",
                    pnl_usd=pnl,
                    equity_before=equity_before_exit,
                    max_loss_usd=entry_breakdown.max_loss_usd,
                    cost_hash=entry_breakdown.assumptions_hash,
                    stress=False,
                )
            )
        else:
            settlement_price = _settlement_price(step)
            pnl = _bounded_pnl(
                _settlement_pnl_usd(
                    step.structure,
                    entry_breakdown,
                    settlement_price=settlement_price,
                    usd_conversion_rate=step.usd_conversion_rate,
                    include_settlement_fee=True,
                ),
                entry_breakdown.max_loss_usd,
            )
            equity += pnl
            n_expiries += 1
            events.append(
                _event(
                    event_type="expiry",
                    timestamp_ms=step.exit_timestamp_ms or step.structure.expiry_ms,
                    structure_id=step.structure.structure_id,
                    reason="european_settlement",
                    pnl_usd=pnl,
                    equity_before=equity_before_exit,
                    max_loss_usd=entry_breakdown.max_loss_usd,
                    cost_hash=entry_breakdown.assumptions_hash,
                    stress=False,
                )
            )
        curve.append(equity)
        stress_curve.append(equity)

    mdd = max_drawdown(curve)
    stress_mdd = max_drawdown(stress_curve)
    invariant_ok = all(event.invariant_ok for event in events)
    return VrpBacktestResult(
        schema_version=VRP_ENGINE_SCHEMA_VERSION,
        initial_equity_usd=float(initial_equity_usd),
        final_equity_usd=float(equity),
        realized_pnl_usd=float(equity - initial_equity_usd),
        n_entries=len(ordered),
        n_exits=n_exits,
        n_expiries=n_expiries,
        n_stress_events=sum(1 for event in events if event.stress),
        max_drawdown=float(mdd),
        max_drawdown_including_stress=float(max(mdd, stress_mdd)),
        max_loss_invariant_ok=invariant_ok,
        events=tuple(events),
        cost_breakdowns=tuple(breakdowns),
    )


def _quote_structure(
    structure: DefinedRiskStructure,
    snapshot: OptionChainSnapshot,
) -> DefinedRiskStructure:
    quoted_legs: list[OptionLeg] = []
    for leg in structure.legs:
        quoted = snapshot.leg_by_instrument_name(leg.instrument_name)
        quoted_legs.append(replace(quoted, side=leg.side))
    return replace(structure, legs=tuple(quoted_legs))



def _settlement_pnl_usd(
    structure: DefinedRiskStructure,
    entry_breakdown: OptionCostBreakdown,
    *,
    settlement_price: float,
    usd_conversion_rate: float,
    include_settlement_fee: bool,
) -> float:
    settlement_price = _require_positive("settlement_price", settlement_price)
    quantity = float(structure.quantity)
    debit = 0.0
    for leg in structure.legs:
        intrinsic = _intrinsic_value(leg, settlement_price)
        signed = intrinsic if leg.side is Side.SHORT else -intrinsic
        debit += signed * leg.contract_multiplier * quantity * usd_conversion_rate
    fee_reserve = _entry_expiry_fee_reserve(entry_breakdown) if include_settlement_fee else 0.0
    return float(entry_breakdown.net_credit_usd - max(0.0, debit) - fee_reserve)


def _intrinsic_value(leg: OptionLeg, settlement_price: float) -> float:
    if leg.option_type is OptionType.CALL:
        return max(0.0, settlement_price - leg.strike)
    if leg.option_type is OptionType.PUT:
        return max(0.0, leg.strike - settlement_price)
    raise ValueError(f"unsupported option_type: {leg.option_type}")


def _realized_exit_fee_reserve(
    entry_breakdown: OptionCostBreakdown,
    exit_breakdown: OptionCostBreakdown,
) -> float:
    return float(
        entry_breakdown.fees.get("entry", 0.0)
        + exit_breakdown.fees.get("exit", 0.0)
        + entry_breakdown.safety_margin
    )


def _entry_expiry_fee_reserve(breakdown: OptionCostBreakdown) -> float:
    return float(
        breakdown.fees.get("entry", 0.0)
        + breakdown.fees.get("expiry_settlement", 0.0)
        + breakdown.safety_margin
    )


def _bounded_pnl(pnl_usd: float, max_loss_usd: float) -> float:
    max_loss_usd = _require_positive("max_loss_usd", max_loss_usd)
    pnl_usd = float(pnl_usd)
    if not math.isfinite(pnl_usd):
        raise ValueError("pnl_usd must be finite")
    return float(max(pnl_usd, -max_loss_usd))


def _settlement_price(step: VrpBacktestStep) -> float:
    if step.settlement_price is not None:
        return step.settlement_price
    price = step.entry_snapshot.settlement_index_price or step.entry_snapshot.index_price
    if price is None:
        raise ValueError("settlement_price is required when no exit_snapshot is supplied")
    return price


def _event(
    *,
    event_type: str,
    timestamp_ms: int,
    structure_id: str,
    reason: str,
    pnl_usd: float,
    equity_before: float,
    max_loss_usd: float,
    cost_hash: str,
    stress: bool,
) -> VrpLedgerEvent:
    invariant_ok = pnl_usd >= -max_loss_usd - _EPSILON and max_loss_usd > 0.0
    return VrpLedgerEvent(
        event_type=event_type,
        timestamp_ms=timestamp_ms,
        structure_id=structure_id,
        reason=reason,
        pnl_usd=float(pnl_usd),
        equity_before_usd=float(equity_before),
        equity_after_usd=float(equity_before + pnl_usd),
        max_loss_usd=float(max_loss_usd),
        cost_assumptions_hash=cost_hash,
        invariant_ok=invariant_ok,
        stress=stress,
    )


def _require_positive(name: str, value: float) -> float:
    value = float(value)
    if not math.isfinite(value) or value <= 0.0:
        raise ValueError(f"{name} must be finite and positive")
    return value

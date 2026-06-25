"""Non-authorizing USD-consistent projection of ETH-denominated reconstructed chains.

Why this exists
---------------
The frozen reconstruction emits option premia in ETH (the Black-Scholes USD model value divided
by the index price) to match Deribit's native quote convention, while strikes and width are USD.
The frozen VRP entry gate then compares an ETH-denominated credit against a USD width with no
conversion (``net_credit / width``), so every structure is rejected and the official walk-forward
selects zero structures in all folds (see ``docs/research/full_frozen_run_findings.md``).

This module projects a snapshot's ETH-denominated leg premia back to USD so a *separate,
explicitly non-authorizing* evaluation can measure credit-to-width and PnL on one consistent
unit. Projection multiplies every premium-denominated quantity (bid/ask/mark price and the price
tick) by the same per-snapshot ETH/USD rate the reconstruction itself used, and moves the
currency labels with the numbers (``premium_currency`` -> ``"USD"``). It NEVER relabels an
ETH-priced leg as USD: prices and labels move together, by the same rate.

Evaluation-only. This does not authorize capital: the underlying source quality (reconstructed
chains + an effective-spread cost proxy) precludes a capital GO regardless of units. The sole
purpose is to measure the clean fold-level economics the frozen unit bug left unmeasured.
"""

from __future__ import annotations

import math
from dataclasses import replace

from ajentix_quant.options.types import OptionChainSnapshot, OptionLeg

USD_PROJECTION_VERSION = "usd-eval-projection-v1"
USD_PROJECTION_SOURCE = "usd_eval_projection:eth_premium_times_index_v1"
EVAL_PREMIUM_CURRENCY = "USD"
EVAL_FEE_CURRENCY = "USD"

__all__ = [
    "EVAL_FEE_CURRENCY",
    "EVAL_PREMIUM_CURRENCY",
    "USD_PROJECTION_SOURCE",
    "USD_PROJECTION_VERSION",
    "eth_usd_rate",
    "project_leg_to_usd",
    "project_snapshot_to_usd",
]


def eth_usd_rate(snapshot: OptionChainSnapshot) -> float | None:
    """Return the exact ETH->USD rate the reconstruction used, or None when unavailable.

    Prefers ``usd_conversion_inputs['ETH_USD']`` (the literal rate the reconstruction divided the
    USD model value by), then ``index_price``, then ``settlement_index_price``. Returns None when
    no positive finite rate exists so callers fail closed on that snapshot rather than fabricate a
    conversion.
    """
    for raw in (
        snapshot.usd_conversion_inputs.get("ETH_USD"),
        snapshot.index_price,
        snapshot.settlement_index_price,
    ):
        if raw is None:
            continue
        try:
            rate = float(raw)
        except (TypeError, ValueError):
            continue
        if math.isfinite(rate) and rate > 0.0:
            return rate
    return None


def project_leg_to_usd(leg: OptionLeg, *, rate: float) -> OptionLeg:
    """Return a copy of ``leg`` with ETH premia converted to USD and currency labels set to USD.

    Only premium-denominated quantities move: bid/ask/mark price and the price tick scale by
    ``rate``. Strike (already USD), implied vols, expiry, multiplier and source quality are
    untouched, so delta selection is identical to the ETH run. ``min_lot`` is normalized to 1 so the
    measurement reports per-contract edge economics (the reconstructed cache's min_lot is an
    artifact that would otherwise scale PnL and max-loss by the lot size).
    """
    if not (math.isfinite(rate) and rate > 0.0):
        raise ValueError("rate must be positive and finite")
    return replace(
        leg,
        bid_price=leg.bid_price * rate,
        ask_price=leg.ask_price * rate,
        mark_price=None if leg.mark_price is None else leg.mark_price * rate,
        min_tick=leg.min_tick * rate,
        min_lot=1.0,
        premium_currency=EVAL_PREMIUM_CURRENCY,
        fee_currency=EVAL_FEE_CURRENCY,
        usd_conversion_source=USD_PROJECTION_SOURCE,
    )


def project_snapshot_to_usd(snapshot: OptionChainSnapshot) -> OptionChainSnapshot | None:
    """Return a USD-consistent copy of ``snapshot``, or None when no ETH/USD rate is available."""
    rate = eth_usd_rate(snapshot)
    if rate is None:
        return None
    projected_legs = tuple(project_leg_to_usd(leg, rate=rate) for leg in snapshot.legs)
    inputs = dict(snapshot.usd_conversion_inputs)
    inputs["usd_projection"] = USD_PROJECTION_SOURCE
    inputs["usd_projection_rate"] = rate
    return replace(snapshot, legs=projected_legs, usd_conversion_inputs=inputs)

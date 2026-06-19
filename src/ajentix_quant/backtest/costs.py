"""Shared engine-equivalent two-leg cost helpers.

The backtest engine charges taker fees and size-based slippage once on entry and
once on exit. These helpers intentionally mirror
``TwoLegFundingBacktest._two_leg_costs`` so research code can use the same cost
surface without instantiating a ledger account.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Any

from ajentix_quant.backtest.account import to_decimal
from ajentix_quant.backtest.slippage import SlippageModel

_BPS_DENOMINATOR = Decimal("10000")


@dataclass(frozen=True)
class CostBreakdownUsd:
    """Fee/slippage split in USD for a two-leg cost calculation."""

    fee_usd: float
    slippage_usd: float

    @property
    def total_usd(self) -> float:
        return float(self.fee_usd + self.slippage_usd)

    def as_dict(self) -> dict[str, float]:
        return {
            "fee_usd": self.fee_usd,
            "slippage_usd": self.slippage_usd,
            "total_usd": self.total_usd,
        }


def slippage_model_from_settings(settings: Any) -> SlippageModel:
    """Build the exact default slippage model used by ``TwoLegFundingBacktest``."""

    return SlippageModel(
        base_bps=float(_setting(settings, "slippage_base_bps", 1.0)),
        impact_bps_per_pct_volume=float(
            _setting(settings, "slippage_impact_bps_per_pct_volume", 5.0)
        ),
        cap_bps=float(_setting(settings, "slippage_cap_bps", 50.0)),
    )


def one_way_two_leg_cost_usd(
    *,
    spot_notional: Decimal | float | int | str,
    perp_notional: Decimal | float | int | str,
    spot_volume_notional: float,
    perp_volume_notional: float,
    settings: Any,
    stress_multiplier: float = 1.0,
) -> CostBreakdownUsd:
    """Return one entry-or-exit two-leg taker fee + slippage cost.

    This is the public equivalent of ``TwoLegFundingBacktest._two_leg_costs``:
    spot/perp fee rates come from settings, slippage is computed from trade
    volume only, and missing/non-positive volume fails closed through
    ``SlippageModel``.
    """

    return one_way_two_leg_cost_usd_with_fee_bps(
        spot_notional=spot_notional,
        perp_notional=perp_notional,
        spot_volume_notional=spot_volume_notional,
        perp_volume_notional=perp_volume_notional,
        settings=settings,
        spot_fee_bps=float(_setting(settings, "spot_taker_fee_bps", 10.0)),
        perp_fee_bps=float(_setting(settings, "perp_taker_fee_bps", 5.5)),
        stress_multiplier=stress_multiplier,
    )


def one_way_two_leg_cost_usd_with_fee_bps(
    *,
    spot_notional: Decimal | float | int | str,
    perp_notional: Decimal | float | int | str,
    spot_volume_notional: float,
    perp_volume_notional: float,
    settings: Any,
    spot_fee_bps: float,
    perp_fee_bps: float,
    stress_multiplier: float = 1.0,
) -> CostBreakdownUsd:
    """Return one-way two-leg cost with explicit fee bps.

    This supports non-authorizing maker sensitivity while keeping the primary
    helper locked to taker fees.
    """

    spot = to_decimal(spot_notional)
    perp = to_decimal(perp_notional)
    spot_fee = spot * to_decimal(spot_fee_bps) / _BPS_DENOMINATOR
    perp_fee = perp * to_decimal(perp_fee_bps) / _BPS_DENOMINATOR
    slippage = slippage_model_from_settings(settings)
    spot_slip = slippage.slippage_cost(
        order_notional=float(spot),
        bar_volume_notional=spot_volume_notional,
        stress_multiplier=stress_multiplier,
    )
    perp_slip = slippage.slippage_cost(
        order_notional=float(perp),
        bar_volume_notional=perp_volume_notional,
        stress_multiplier=stress_multiplier,
    )
    return CostBreakdownUsd(
        fee_usd=float(spot_fee + perp_fee),
        slippage_usd=float(spot_slip + perp_slip),
    )


def round_trip_cost_usd(
    *,
    spot_notional: Decimal | float | int | str,
    perp_notional: Decimal | float | int | str,
    spot_volume_notional: float,
    perp_volume_notional: float,
    settings: Any,
    stress_multiplier: float = 1.0,
) -> float:
    """Return engine-equivalent entry+exit taker fees plus size slippage.

    The engine's expected-cost surface uses the current executable bar volume
    for both the entry and the modeled exit, so round trip is exactly twice the
    one-way two-leg cost.
    """

    one_way = one_way_two_leg_cost_usd(
        spot_notional=spot_notional,
        perp_notional=perp_notional,
        spot_volume_notional=spot_volume_notional,
        perp_volume_notional=perp_volume_notional,
        settings=settings,
        stress_multiplier=stress_multiplier,
    )
    return float(2.0 * one_way.total_usd)


def round_trip_cost_usd_with_fee_bps(
    *,
    spot_notional: Decimal | float | int | str,
    perp_notional: Decimal | float | int | str,
    spot_volume_notional: float,
    perp_volume_notional: float,
    settings: Any,
    spot_fee_bps: float,
    perp_fee_bps: float,
    stress_multiplier: float = 1.0,
) -> float:
    """Return entry+exit cost with explicit fees for sensitivity analysis."""

    one_way = one_way_two_leg_cost_usd_with_fee_bps(
        spot_notional=spot_notional,
        perp_notional=perp_notional,
        spot_volume_notional=spot_volume_notional,
        perp_volume_notional=perp_volume_notional,
        settings=settings,
        spot_fee_bps=spot_fee_bps,
        perp_fee_bps=perp_fee_bps,
        stress_multiplier=stress_multiplier,
    )
    return float(2.0 * one_way.total_usd)


def round_trip_cost_bps(
    *,
    spot_notional: Decimal | float | int | str,
    perp_notional: Decimal | float | int | str,
    spot_volume_notional: float,
    perp_volume_notional: float,
    settings: Any,
    stress_multiplier: float = 1.0,
    reference_notional: Decimal | float | int | str | None = None,
) -> float:
    """Return round-trip cost in bps of the per-setup notional."""

    reference = (
        max(float(spot_notional), float(perp_notional))
        if reference_notional is None
        else float(reference_notional)
    )
    if reference <= 0.0:
        raise ValueError("reference_notional must be positive")
    cost = round_trip_cost_usd(
        spot_notional=spot_notional,
        perp_notional=perp_notional,
        spot_volume_notional=spot_volume_notional,
        perp_volume_notional=perp_volume_notional,
        settings=settings,
        stress_multiplier=stress_multiplier,
    )
    return float(cost / reference * 10_000.0)


def safety_margin_usd(
    *,
    notional: Decimal | float | int | str,
    safety_margin_bps: float = 1.0,
) -> float:
    """Return the preregistered bps safety margin on per-setup notional."""

    n = to_decimal(notional)
    if n < 0:
        raise ValueError("notional must be non-negative")
    return float(n * to_decimal(safety_margin_bps) / _BPS_DENOMINATOR)


def _setting(settings: Any, name: str, default: float) -> float:
    return float(getattr(settings, name, default))

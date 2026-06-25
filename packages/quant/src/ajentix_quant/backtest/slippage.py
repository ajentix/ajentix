"""Deterministic size-based slippage model for replay backtests.

Slippage is driven only by trade-volume notional supplied by the caller. Mark and
index streams do not carry executable volume and must never be passed as the volume
source. Missing or non-positive trade volume fails closed.
"""

from __future__ import annotations

import math
from dataclasses import dataclass


@dataclass(frozen=True)
class SlippageModel:
    """Monotonic bps slippage as a function of order size / bar volume."""

    base_bps: float
    impact_bps_per_pct_volume: float
    cap_bps: float

    def __post_init__(self) -> None:
        _require_non_negative("base_bps", self.base_bps)
        _require_non_negative("impact_bps_per_pct_volume", self.impact_bps_per_pct_volume)
        _require_non_negative("cap_bps", self.cap_bps)

    def slippage_bps(
        self,
        *,
        order_notional: float,
        bar_volume_notional: float | None,
        stress_multiplier: float = 1.0,
    ) -> float:
        """Return deterministic slippage in bps, failing closed without trade volume."""

        order_notional = _require_non_negative("order_notional", order_notional)
        stress_multiplier = _require_non_negative("stress_multiplier", stress_multiplier)
        volume = _require_positive_volume(bar_volume_notional)
        pct_volume = order_notional / volume * 100.0
        raw_bps = self.base_bps + self.impact_bps_per_pct_volume * pct_volume
        return float(min(self.cap_bps, raw_bps) * stress_multiplier)

    def slippage_cost(
        self,
        *,
        order_notional: float,
        bar_volume_notional: float | None,
        stress_multiplier: float = 1.0,
    ) -> float:
        """Return deterministic slippage cost in quote currency."""

        order_notional = _require_non_negative("order_notional", order_notional)
        bps = self.slippage_bps(
            order_notional=order_notional,
            bar_volume_notional=bar_volume_notional,
            stress_multiplier=stress_multiplier,
        )
        return float(order_notional * bps / 10_000.0)


def _require_non_negative(name: str, value: float) -> float:
    value = float(value)
    if not math.isfinite(value):
        raise ValueError(f"{name} must be finite")
    if value < 0.0:
        raise ValueError(f"{name} must be non-negative")
    return value


def _require_positive_volume(value: float | None) -> float:
    if value is None:
        raise ValueError("bar_volume_notional is required for slippage (fail closed)")
    value = float(value)
    if not math.isfinite(value):
        raise ValueError("bar_volume_notional must be finite")
    if value <= 0.0:
        raise ValueError("bar_volume_notional must be positive for slippage (fail closed)")
    return value

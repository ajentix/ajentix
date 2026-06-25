"""Small-capital deterministic sizing policy for carry setups."""

from __future__ import annotations

import math
from dataclasses import dataclass


@dataclass(frozen=True)
class SmallCapitalSizingPolicy:
    """Bound one setup to a small slice of capital.

    At $500-2000 with <=25% per setup and one to two concurrent setups, this keeps
    single-setup capital <=25%. The reserve is enforced by the invariant
    max_position_pct <= 1 - reserve_pct; callers pass leverage separately so the returned
    value is target notional, not cash collateral.
    """

    max_position_pct: float = 0.25
    reserve_pct: float = 0.25
    min_notional_usd: float = 5.0

    def __post_init__(self) -> None:
        _require_finite_non_negative("max_position_pct", self.max_position_pct)
        _require_finite_non_negative("reserve_pct", self.reserve_pct)
        _require_finite_non_negative("min_notional_usd", self.min_notional_usd)
        if self.max_position_pct > 1.0:
            raise ValueError("max_position_pct must be <= 1")
        if self.reserve_pct > 1.0:
            raise ValueError("reserve_pct must be <= 1")
        if self.max_position_pct > 1.0 - self.reserve_pct:
            raise ValueError("max_position_pct must be <= 1 - reserve_pct")

    def size(
        self,
        *,
        equity_usd: float,
        leverage: float,
        min_notional_usd: float | None = None,
    ) -> float:
        _require_finite_non_negative("equity_usd", equity_usd)
        _require_finite_non_negative("leverage", leverage)
        if min_notional_usd is not None:
            _require_finite_non_negative("min_notional_usd", min_notional_usd)
        return equity_usd * self.max_position_pct * leverage

    def feasible(
        self,
        *,
        equity_usd: float,
        leverage: float,
        min_notional_usd: float | None = None,
    ) -> bool:
        threshold = self.min_notional_usd if min_notional_usd is None else min_notional_usd
        _require_finite_non_negative("min_notional_usd", threshold)
        return self.size(
            equity_usd=equity_usd,
            leverage=leverage,
            min_notional_usd=threshold,
        ) >= threshold


def _require_finite_non_negative(name: str, value: float) -> None:
    if not math.isfinite(value):
        raise ValueError(f"{name} must be finite")
    if value < 0.0:
        raise ValueError(f"{name} must be non-negative")

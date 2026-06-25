"""Typed market state and carry decision contracts."""

from __future__ import annotations

import enum
import math
from dataclasses import dataclass


class SignalAction(enum.StrEnum):
    ENTER = "enter"
    HOLD = "hold"
    EXIT = "exit"
    FLAT = "flat"


@dataclass(frozen=True)
class MarketState:
    symbol: str
    funding_rate: float
    interval_hours: float
    spot_close: float
    perp_mark_close: float
    index_close: float | None
    basis_bps: float
    realized_vol_annual: float
    expected_cost_bps: float
    equity_usd: float
    net_delta_frac: float = 0.0
    in_position: bool = False
    current_leverage: float = 0.0
    gap_survival_leverage_cap: float = 0.0
    health_factor: float = math.inf
    risk_deleverage: bool = False

    def __post_init__(self) -> None:
        self.validate()

    def validate(self) -> MarketState:
        finite_fields = {
            "funding_rate": self.funding_rate,
            "interval_hours": self.interval_hours,
            "spot_close": self.spot_close,
            "perp_mark_close": self.perp_mark_close,
            "basis_bps": self.basis_bps,
            "realized_vol_annual": self.realized_vol_annual,
            "expected_cost_bps": self.expected_cost_bps,
            "equity_usd": self.equity_usd,
            "net_delta_frac": self.net_delta_frac,
            "current_leverage": self.current_leverage,
            "gap_survival_leverage_cap": self.gap_survival_leverage_cap,
        }
        for name, value in finite_fields.items():
            _require_finite(name, value)

        if self.index_close is not None:
            _require_finite("index_close", self.index_close)
            if self.index_close <= 0.0:
                raise ValueError("index_close must be positive when present")

        if math.isnan(self.health_factor) or self.health_factor < 0.0:
            raise ValueError("health_factor must be non-negative or math.inf")
        if self.health_factor != math.inf and not math.isfinite(self.health_factor):
            raise ValueError("health_factor must be finite or math.inf")

        if self.interval_hours <= 0.0:
            raise ValueError("interval_hours must be positive")
        if self.spot_close <= 0.0:
            raise ValueError("spot_close must be positive")
        if self.perp_mark_close <= 0.0:
            raise ValueError("perp_mark_close must be positive")
        if self.realized_vol_annual < 0.0:
            raise ValueError("realized_vol_annual must be non-negative")
        if self.expected_cost_bps < 0.0:
            raise ValueError("expected_cost_bps must be non-negative")
        if self.equity_usd < 0.0:
            raise ValueError("equity_usd must be non-negative")
        if self.current_leverage < 0.0:
            raise ValueError("current_leverage must be non-negative")
        if self.gap_survival_leverage_cap < 0.0:
            raise ValueError("gap_survival_leverage_cap must be non-negative")
        return self


@dataclass(frozen=True)
class CarrySignal:
    symbol: str
    action: SignalAction
    target_notional_usd: float
    target_leverage: float
    target_net_delta: float
    reason: str


def _require_finite(name: str, value: float) -> None:
    if not math.isfinite(value):
        raise ValueError(f"{name} must be finite")

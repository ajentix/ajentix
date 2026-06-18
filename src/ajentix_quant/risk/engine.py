"""Risk engine (Phase 0 skeleton).

Encodes the deterministic risk model agreed in the spec:
  - dynamic regime-aware leverage ("lever up calm, down volatile"),
  - liquidation-distance floor, emergency reserve, funding-reversal forced exit,
  - drawdown kill-switch, ADL-rank awareness (interface).

Live enforcement (margin calls, real liquidation prices, ADL polling) lands in Phase 2.
"""

from __future__ import annotations

from dataclasses import dataclass

# Volatility-targeting reference: scale leverage toward this annualized vol budget.
_TARGET_VOL_ANNUAL = 0.40
# Funding regime considered "strong positive" -> allow lever-up.
_STRONG_FUNDING_8H = 0.0003


@dataclass
class RiskParams:
    base_leverage: float = 2.0
    max_leverage: float = 5.0
    min_liq_distance_pct: float = 0.15
    reserve_pct: float = 0.25
    max_drawdown_pct: float = 0.05
    funding_reversal_exit_hours: int = 24
    max_position_pct: float = 0.25


class RiskEngine:
    def __init__(self, params: RiskParams | None = None) -> None:
        self.params = params or RiskParams()

    def dynamic_leverage(self, *, realized_vol_annual: float, funding_rate_8h: float) -> float:
        """Lever up in low-vol + high-funding regimes; down on vol spikes. Bounded [1, max]."""
        p = self.params
        vol = max(realized_vol_annual, 1e-6)
        lev = p.base_leverage * (_TARGET_VOL_ANNUAL / vol)
        if funding_rate_8h >= _STRONG_FUNDING_8H:
            lev *= 1.5
        return float(min(max(lev, 1.0), p.max_leverage))

    def liquidation_distance_ok(self, *, leverage: float) -> bool:
        """Approx liquidation distance ~ 1/leverage; require >= floor."""
        approx_liq_distance = 1.0 / max(leverage, 1e-6)
        return approx_liq_distance >= self.params.min_liq_distance_pct

    def should_exit_funding_reversal(self, *, hours_negative: float) -> bool:
        return hours_negative >= self.params.funding_reversal_exit_hours

    def kill_switch(self, *, drawdown_pct: float) -> bool:
        return drawdown_pct >= self.params.max_drawdown_pct

    def deployable_fraction(self) -> float:
        """Fraction of capital deployable after holding the emergency reserve."""
        return max(0.0, 1.0 - self.params.reserve_pct)

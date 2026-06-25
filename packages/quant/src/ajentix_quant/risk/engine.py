"""Risk engine (Phase 0/1 deterministic safety model).

Encodes the deterministic risk model agreed in the spec:
  - dynamic regime-aware leverage ("lever up calm, down volatile"),
  - liquidation-distance floor, emergency reserve, funding-reversal forced exit,
  - drawdown kill-switch, ADL-rank awareness (absent-safe interface),
  - Phase 1 venue-margin gap-survival leverage caps.

Live enforcement (margin calls, real liquidation prices, ADL polling) lands in Phase 2.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Protocol

from ajentix_quant.risk.margin import VenueMarginModel

# Volatility-targeting reference: scale leverage toward this annualized vol budget.
_TARGET_VOL_ANNUAL = 0.40
# Funding regime considered "strong positive" -> allow lever-up.
_STRONG_FUNDING_8H = 0.0003
# Phase 1 hard risk cap: no risk-engine leverage path may return more than 5x.
_MAX_LEVERAGE_HARD_CAP = 5.0


def _require_finite(name: str, value: float) -> float:
    value = float(value)
    if not math.isfinite(value):
        raise ValueError(f"{name} must be finite")
    return value


@dataclass
class RiskParams:
    base_leverage: float = 2.0
    max_leverage: float = 5.0
    min_liq_distance_pct: float = 0.15
    reserve_pct: float = 0.25
    max_drawdown_pct: float = 0.05
    funding_reversal_exit_hours: int = 24
    max_position_pct: float = 0.25
    health_factor_floor: float = 1.5
    vol_spike_annual: float = 1.0
    funding_compression_8h: float = 0.00005
    funding_reversal_imminent_8h: float = 0.0
    max_net_delta_frac: float = 0.02
    gap_stress_pct: float = 0.20
    adl_rank_threshold: int = 3


class ADLRankProvider(Protocol):
    def adl_rank(self, symbol: str) -> int | None:
        """Return venue ADL rank for ``symbol`` when available; ``None`` means absent."""
        ...


class NullADLProvider:
    def adl_rank(self, symbol: str) -> int | None:
        return None


class RiskEngine:
    def __init__(
        self,
        params: RiskParams | None = None,
        adl_provider: ADLRankProvider | None = None,
    ) -> None:
        self.params = params or RiskParams()
        self.adl_provider = adl_provider or NullADLProvider()

    def dynamic_leverage(self, *, realized_vol_annual: float, funding_rate_8h: float) -> float:
        """Lever up in low-vol + high-funding regimes; down on vol spikes. Bounded [1, max]."""
        realized_vol_annual = _require_finite("realized_vol_annual", realized_vol_annual)
        funding_rate_8h = _require_finite("funding_rate_8h", funding_rate_8h)
        if realized_vol_annual < 0.0:
            raise ValueError("realized_vol_annual must be non-negative")
        p = self.params
        vol = max(realized_vol_annual, 1e-6)
        lev = p.base_leverage * (_TARGET_VOL_ANNUAL / vol)
        if funding_rate_8h >= _STRONG_FUNDING_8H:
            lev *= 1.5
        return float(min(max(lev, 1.0), p.max_leverage, _MAX_LEVERAGE_HARD_CAP))

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

    def gap_survival_leverage_cap(
        self,
        margin_model: VenueMarginModel,
        *,
        mmr: float | None = None,
        equity: float | None = None,
    ) -> float:
        return margin_model.gap_survival_leverage_cap(
            max_gap_pct=self.params.gap_stress_pct,
            health_factor_floor=self.params.health_factor_floor,
            reserve_pct=self.params.reserve_pct,
            mmr=mmr,
            equity=equity,
        )

    def dynamic_leverage_capped(
        self,
        *,
        realized_vol_annual: float,
        funding_rate_8h: float,
        margin_model: VenueMarginModel | None = None,
        equity: float | None = None,
    ) -> float:
        """Dynamic leverage, capped by gap survival. Returns 0.0 if no >=1x is safe."""
        lev = self.dynamic_leverage(
            realized_vol_annual=realized_vol_annual,
            funding_rate_8h=funding_rate_8h,
        )
        if margin_model is not None:
            cap = self.gap_survival_leverage_cap(margin_model, equity=equity)
            if cap < 1.0:
                # no leverage >= 1x survives the documented gap at the HF floor -> no entry
                return 0.0
            lev = min(lev, cap)
        return float(min(lev, self.params.max_leverage, _MAX_LEVERAGE_HARD_CAP))

    def deleverage_reasons(
        self,
        *,
        realized_vol_annual: float,
        funding_rate_8h: float,
        health_factor: float,
        hours_negative: float,
        drawdown_pct: float,
        adl_rank: int | None = None,
        net_delta_frac: float = 0.0,
    ) -> tuple[str, ...]:
        realized_vol_annual = _require_finite("realized_vol_annual", realized_vol_annual)
        funding_rate_8h = _require_finite("funding_rate_8h", funding_rate_8h)
        hours_negative = _require_finite("hours_negative", hours_negative)
        drawdown_pct = _require_finite("drawdown_pct", drawdown_pct)
        net_delta_frac = _require_finite("net_delta_frac", net_delta_frac)
        if math.isnan(float(health_factor)):
            raise ValueError("health_factor must not be NaN")
        if realized_vol_annual < 0.0:
            raise ValueError("realized_vol_annual must be non-negative")
        if hours_negative < 0.0:
            raise ValueError("hours_negative must be non-negative")
        if drawdown_pct < 0.0:
            raise ValueError("drawdown_pct must be non-negative")

        p = self.params
        reasons: list[str] = []
        if realized_vol_annual >= p.vol_spike_annual:
            reasons.append("vol_spike")
        if 0.0 <= funding_rate_8h < p.funding_compression_8h:
            reasons.append("funding_compression")
        if funding_rate_8h <= p.funding_reversal_imminent_8h:
            reasons.append("funding_reversal_imminent")
        if health_factor < p.health_factor_floor:
            reasons.append("health_factor")
        if self.kill_switch(drawdown_pct=drawdown_pct):
            reasons.append("drawdown_kill")
        if adl_rank is not None and adl_rank >= p.adl_rank_threshold:
            reasons.append("adl_rank")
        if abs(net_delta_frac) > p.max_net_delta_frac:
            reasons.append("net_delta")
        return tuple(reasons)

    def should_deleverage(
        self,
        *,
        realized_vol_annual: float,
        funding_rate_8h: float,
        health_factor: float,
        hours_negative: float,
        drawdown_pct: float,
        adl_rank: int | None = None,
        net_delta_frac: float = 0.0,
    ) -> bool:
        return bool(
            self.deleverage_reasons(
                realized_vol_annual=realized_vol_annual,
                funding_rate_8h=funding_rate_8h,
                health_factor=health_factor,
                hours_negative=hours_negative,
                drawdown_pct=drawdown_pct,
                adl_rank=adl_rank,
                net_delta_frac=net_delta_frac,
            )
        )

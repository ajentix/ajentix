"""Deterministic venue margin and risk-limit helpers.

The Bybit helpers in this module are illustrative frozen snapshots for tests and
Phase 1 safety modelling. They are internally consistent approximations, not live
venue metadata, and must be replaced by venue-sourced cache data before any live
order path is enabled.
"""

from __future__ import annotations

import math
from dataclasses import dataclass

from ajentix_quant.adapters.base import SourceQuality

_MAX_LEVERAGE_HARD_CAP = 5.0


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


def _tier_contains(tier: MaintenanceTier, notional: float) -> bool:
    return tier.notional_floor <= notional < tier.notional_cap


@dataclass(frozen=True, kw_only=True)
class InstrumentMeta:
    symbol: str
    contract_size: float = 1.0
    tick_size: float
    qty_step: float
    min_qty: float
    min_notional: float
    taker_fee_bps: float
    maker_fee_bps: float
    source_quality: SourceQuality

    def __post_init__(self) -> None:
        if not self.symbol:
            raise ValueError("symbol must be non-empty")
        _require_positive("contract_size", self.contract_size)
        _require_positive("tick_size", self.tick_size)
        _require_positive("qty_step", self.qty_step)
        _require_non_negative("min_qty", self.min_qty)
        _require_non_negative("min_notional", self.min_notional)
        _require_non_negative("taker_fee_bps", self.taker_fee_bps)
        _require_non_negative("maker_fee_bps", self.maker_fee_bps)

    def round_qty(self, qty: float) -> float:
        """Floor ``qty`` to the instrument step without returning a negative value."""
        qty = _require_finite("qty", qty)
        if qty <= 0.0:
            return 0.0
        stepped = math.floor(qty / self.qty_step) * self.qty_step
        return max(0.0, float(round(stepped, 12)))

    def meets_min_notional(self, notional: float) -> bool:
        notional = _require_non_negative("notional", notional)
        return notional >= self.min_notional


@dataclass(frozen=True, kw_only=True)
class MaintenanceTier:
    notional_floor: float
    notional_cap: float
    maintenance_margin_rate: float
    maintenance_amount: float = 0.0
    max_leverage: float

    def __post_init__(self) -> None:
        _require_non_negative("notional_floor", self.notional_floor)
        if not (math.isfinite(self.notional_cap) or self.notional_cap == math.inf):
            raise ValueError("notional_cap must be finite or math.inf")
        if self.notional_cap <= self.notional_floor:
            raise ValueError("notional_cap must exceed notional_floor")
        _require_non_negative("maintenance_margin_rate", self.maintenance_margin_rate)
        _require_non_negative("maintenance_amount", self.maintenance_amount)
        _require_positive("max_leverage", self.max_leverage)


@dataclass(frozen=True)
class RiskLimits:
    symbol: str
    tiers: tuple[MaintenanceTier, ...]
    source_quality: SourceQuality

    def __post_init__(self) -> None:
        if not self.symbol:
            raise ValueError("symbol must be non-empty")
        tiers = tuple(self.tiers)
        if not tiers:
            raise ValueError("tiers must be non-empty")
        previous_cap = 0.0
        for index, tier in enumerate(tiers):
            if index == 0:
                if tier.notional_floor != 0.0:
                    raise ValueError("first tier must start at notional 0")
            elif tier.notional_floor != previous_cap:
                raise ValueError("tiers must be contiguous")
            previous_cap = tier.notional_cap
        object.__setattr__(self, "tiers", tiers)

    def tier_for(self, notional: float) -> MaintenanceTier:
        notional = _require_non_negative("notional", notional)
        for tier in self.tiers:
            if _tier_contains(tier, notional):
                return tier
        raise ValueError(f"no maintenance tier for notional {notional}")

    def maintenance_margin(self, notional: float) -> float:
        notional = _require_non_negative("notional", notional)
        tier = self.tier_for(notional)
        return max(0.0, notional * tier.maintenance_margin_rate - tier.maintenance_amount)

    def max_leverage(self, notional: float) -> float:
        return self.tier_for(notional).max_leverage


class VenueMarginModel:
    """Margin model for a single short linear-perp leg."""

    def __init__(self, instrument: InstrumentMeta, limits: RiskLimits) -> None:
        if instrument.symbol != limits.symbol:
            raise ValueError("instrument and risk limits symbols must match")
        self.instrument = instrument
        self.limits = limits

    def short_unrealized_pnl(self, entry: float, mark: float, qty: float) -> float:
        entry = _require_positive("entry", entry)
        mark = _require_positive("mark", mark)
        qty = _require_non_negative("qty", qty)
        return (entry - mark) * qty

    def health_factor(
        self, *, entry: float, mark: float, qty: float, wallet_equity: float
    ) -> float:
        # wallet_equity may be negative (underwater account) -> HF < 1 -> liquidation.
        wallet_equity = _require_finite("wallet_equity", wallet_equity)
        pnl = self.short_unrealized_pnl(entry, mark, qty)
        maintenance = self.limits.maintenance_margin(mark * qty)
        if maintenance == 0.0:
            return math.inf
        return (wallet_equity + pnl) / maintenance

    def liquidation_mark(self, *, entry: float, qty: float, wallet_equity: float) -> float:
        entry = _require_positive("entry", entry)
        qty = _require_non_negative("qty", qty)
        # wallet_equity may be negative (underwater account); finite-only validation.
        wallet_equity = _require_finite("wallet_equity", wallet_equity)
        if qty == 0.0:
            return math.inf

        candidates: list[float] = []
        for tier in self.limits.tiers:
            denominator = qty * (1.0 + tier.maintenance_margin_rate)
            if denominator <= 0.0:
                continue
            candidate = (wallet_equity + entry * qty + tier.maintenance_amount) / denominator
            if not math.isfinite(candidate) or candidate <= 0.0:
                continue
            notional = candidate * qty
            if _tier_contains(tier, notional) and self.limits.maintenance_margin(notional) > 0.0:
                candidates.append(candidate)
        if not candidates:
            return math.inf
        return min(candidates)

    def liquidation_distance_pct(
        self, *, entry: float, mark: float, qty: float, wallet_equity: float
    ) -> float:
        mark = _require_positive("mark", mark)
        liquidation = self.liquidation_mark(entry=entry, qty=qty, wallet_equity=wallet_equity)
        return max(0.0, (liquidation - mark) / mark)

    def survives_gap(
        self,
        *,
        entry: float,
        mark: float,
        qty: float,
        wallet_equity: float,
        gap_pct: float,
        health_factor_floor: float,
    ) -> bool:
        mark = _require_positive("mark", mark)
        gap_pct = _require_non_negative("gap_pct", gap_pct)
        health_factor_floor = _require_positive("health_factor_floor", health_factor_floor)
        shocked_mark = mark * (1.0 + gap_pct)
        health = self.health_factor(
            entry=entry, mark=shocked_mark, qty=qty, wallet_equity=wallet_equity
        )
        return health >= health_factor_floor

    def gap_survival_leverage_cap(
        self,
        *,
        max_gap_pct: float,
        health_factor_floor: float,
        reserve_pct: float,
        mmr: float | None = None,
        equity: float | None = None,
    ) -> float:
        """Return the leverage cap that keeps a shocked short leg above the HF floor.

        With entry and current mark normalized to 1, total-equity leverage ``L`` means
        notional ``N = L * E`` while only ``(1 - reserve_pct) * E`` backs the leg.
        After an upward gap ``g``, the short loses ``g * N`` and maintenance is
        ``(1 + g) * N * mmr``. Requiring
        ``((1-r)/L - g) / ((1+g)*mmr) >= health_factor_floor`` gives
        ``L <= (1-r) / (g + health_factor_floor*(1+g)*mmr)``.

        Semantics: the returned cap is clamped to ``[0, min(tier.max_leverage, 5x)]``. A
        value ``< 1.0`` means NO leverage >= 1x survives the documented gap at the HF floor
        — the caller must treat that as "do not enter" (it is NOT floored up to 1x).

        Tier awareness: when ``equity`` is given (and ``mmr`` is not overridden), the cap is
        tightened iteratively using the maintenance tier that the resulting notional
        ``cap * equity`` actually lands in, so it stays conservative across tiers. When
        ``equity`` is omitted the lowest-tier MMR is used (valid only within the first tier).
        """
        max_gap_pct = _require_non_negative("max_gap_pct", max_gap_pct)
        health_factor_floor = _require_positive("health_factor_floor", health_factor_floor)
        reserve_pct = _require_non_negative("reserve_pct", reserve_pct)
        if reserve_pct >= 1.0:
            raise ValueError("reserve_pct must be less than 1")

        hard = _MAX_LEVERAGE_HARD_CAP

        def cap_for_mmr(m: float) -> float:
            denom = max_gap_pct + health_factor_floor * (1.0 + max_gap_pct) * m
            return math.inf if denom == 0.0 else (1.0 - reserve_pct) / denom

        lowest_tier = min(self.limits.tiers, key=lambda tier: tier.notional_floor)
        if mmr is not None:
            tier_mmr = _require_non_negative("mmr", mmr)
            cap = min(cap_for_mmr(tier_mmr), lowest_tier.max_leverage, hard)
            return float(max(0.0, cap))

        cap = min(cap_for_mmr(lowest_tier.maintenance_margin_rate), lowest_tier.max_leverage, hard)
        if equity is not None:
            equity = _require_positive("equity", equity)
            top_tier = max(self.limits.tiers, key=lambda tier: tier.notional_floor)
            # Tighten using the tier the SHOCKED notional lands in (monotone decreasing).
            # Maintenance after the gap is evaluated on the shocked notional
            # (1+g)*N, so tier (hence MMR) selection must use the shocked notional, not the
            # entry notional — otherwise a gap-induced tier crossing yields a false-safe cap.
            for _ in range(len(self.limits.tiers) + 2):
                notional = (1.0 + max_gap_pct) * cap * equity
                try:
                    tier = self.limits.tier_for(notional)
                except ValueError:
                    tier = top_tier  # beyond top tier -> use its (highest) MMR, most conservative
                tier_cap = min(cap_for_mmr(tier.maintenance_margin_rate), tier.max_leverage, hard)
                if tier_cap >= cap:
                    break
                cap = tier_cap
        return float(max(0.0, cap))


def bybit_btc_eth_risk_limits() -> dict[str, RiskLimits]:
    """Illustrative Bybit-like linear-perp risk tiers frozen for deterministic tests."""
    return {
        "BTC/USDT:USDT": RiskLimits(
            symbol="BTC/USDT:USDT",
            source_quality=SourceQuality.FROZEN_SNAPSHOT,
            tiers=(
                MaintenanceTier(
                    notional_floor=0.0,
                    notional_cap=1_000_000.0,
                    maintenance_margin_rate=0.005,
                    maintenance_amount=0.0,
                    max_leverage=100.0,
                ),
                MaintenanceTier(
                    notional_floor=1_000_000.0,
                    notional_cap=2_000_000.0,
                    maintenance_margin_rate=0.010,
                    maintenance_amount=5_000.0,
                    max_leverage=50.0,
                ),
                MaintenanceTier(
                    notional_floor=2_000_000.0,
                    notional_cap=math.inf,
                    maintenance_margin_rate=0.020,
                    maintenance_amount=25_000.0,
                    max_leverage=25.0,
                ),
            ),
        ),
        "ETH/USDT:USDT": RiskLimits(
            symbol="ETH/USDT:USDT",
            source_quality=SourceQuality.FROZEN_SNAPSHOT,
            tiers=(
                MaintenanceTier(
                    notional_floor=0.0,
                    notional_cap=500_000.0,
                    maintenance_margin_rate=0.006,
                    maintenance_amount=0.0,
                    max_leverage=75.0,
                ),
                MaintenanceTier(
                    notional_floor=500_000.0,
                    notional_cap=1_000_000.0,
                    maintenance_margin_rate=0.012,
                    maintenance_amount=3_000.0,
                    max_leverage=50.0,
                ),
                MaintenanceTier(
                    notional_floor=1_000_000.0,
                    notional_cap=math.inf,
                    maintenance_margin_rate=0.025,
                    maintenance_amount=16_000.0,
                    max_leverage=25.0,
                ),
            ),
        ),
    }


def bybit_btc_eth_instruments() -> dict[str, InstrumentMeta]:
    """Illustrative Bybit-like BTC/ETH linear-perp instrument metadata snapshots."""
    return {
        "BTC/USDT:USDT": InstrumentMeta(
            symbol="BTC/USDT:USDT",
            contract_size=1.0,
            tick_size=0.10,
            qty_step=0.001,
            min_qty=0.001,
            min_notional=5.0,
            taker_fee_bps=5.5,
            maker_fee_bps=2.0,
            source_quality=SourceQuality.FROZEN_SNAPSHOT,
        ),
        "ETH/USDT:USDT": InstrumentMeta(
            symbol="ETH/USDT:USDT",
            contract_size=1.0,
            tick_size=0.01,
            qty_step=0.01,
            min_qty=0.01,
            min_notional=5.0,
            taker_fee_bps=5.5,
            maker_fee_bps=2.0,
            source_quality=SourceQuality.FROZEN_SNAPSHOT,
        ),
    }

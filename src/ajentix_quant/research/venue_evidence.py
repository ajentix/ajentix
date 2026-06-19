"""Deterministic A2 venue-feasibility evidence logic.

This module is deliberately pure/stdlib-only apart from reusing the G002 cost helper.
Network collection, exchange adapters, and report I/O live in ``scripts/``.
"""

from __future__ import annotations

import math
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any

from ajentix_quant.backtest.costs import (
    round_trip_cost_usd_with_fee_bps,
    safety_margin_usd,
)

HOUR_MS = 60 * 60 * 1000
DAY_MS = 24 * HOUR_MS

REASON_SHORT_FUNDING_HISTORY = "SHORT_FUNDING_HISTORY"
REASON_MISSING_FUNDING_HISTORY = "MISSING_FUNDING_HISTORY"
REASON_MISSING_CADENCE = "MISSING_CADENCE"
REASON_MISSING_FEE_SCHEDULE = "MISSING_FEE_SCHEDULE"
REASON_MISSING_DEPTH_LIQUIDITY = "MISSING_DEPTH_LIQUIDITY"
REASON_SLIPPAGE_TOO_HIGH = "SLIPPAGE_TOO_HIGH"
REASON_MISSING_MEASURED_OPPORTUNITY_STATS = "MISSING_MEASURED_OPPORTUNITY_STATS"
REASON_QUALIFYING_24H_PCT_BELOW_BAR = "QUALIFYING_24H_PCT_BELOW_BAR"
REASON_QUALIFYING_24H_WINDOWS_BELOW_BAR = "QUALIFYING_24H_WINDOWS_BELOW_BAR"
REASON_OPPORTUNITY_CLUSTERS_BELOW_BAR = "OPPORTUNITY_CLUSTERS_BELOW_BAR"
REASON_WEEKLY_CONCENTRATION_TOO_HIGH = "WEEKLY_CONCENTRATION_TOO_HIGH"
REASON_MISSING_ADL_LIQUIDATION_METADATA = "MISSING_ADL_LIQUIDATION_METADATA"
REASON_MISSING_BORROW_BASIS_RISK = "MISSING_BORROW_BASIS_RISK"
REASON_MISSING_CEX_COMPARISON = "MISSING_CEX_COMPARISON"
REASON_ROADMAP_APR_CLAIM_NOT_AUTHORIZING = "ROADMAP_APR_CLAIM_NOT_AUTHORIZING"


@dataclass(frozen=True)
class FundingObservation:
    """One source-attributed funding settlement row.

    ``rate`` is fractional for ``interval_hours``; for Hyperliquid hourly rows,
    ``rate=0.0000125`` means 0.00125% for that hour.
    """

    timestamp_ms: int
    rate: float
    interval_hours: float
    source: str = "venue"

    def as_dict(self) -> dict[str, Any]:
        return {
            "timestamp_ms": int(self.timestamp_ms),
            "rate": _json_float(self.rate),
            "interval_hours": _json_float(self.interval_hours),
            "source": self.source,
        }


@dataclass(frozen=True)
class FeeScheduleEvidence:
    """Known taker/maker fee schedule with a citation."""

    venue: str
    taker_fee_bps: float
    maker_fee_bps: float | None
    source_url: str
    source_note: str

    @property
    def known(self) -> bool:
        return (
            bool(self.venue)
            and bool(self.source_url)
            and math.isfinite(float(self.taker_fee_bps))
            and float(self.taker_fee_bps) >= 0.0
        )

    def as_dict(self) -> dict[str, Any]:
        return {
            "venue": self.venue,
            "taker_fee_bps": _json_float(self.taker_fee_bps),
            "maker_fee_bps": _json_float(self.maker_fee_bps),
            "source_url": self.source_url,
            "source_note": self.source_note,
        }


@dataclass(frozen=True)
class DepthSlippageEstimate:
    """Measured order-book depth/slippage for one USD order size."""

    order_size_usd: float
    bid_slippage_bps: float
    ask_slippage_bps: float
    bid_depth_usd: float
    ask_depth_usd: float
    source: str
    fetched_at: str

    @property
    def max_slippage_bps(self) -> float:
        return max(float(self.bid_slippage_bps), float(self.ask_slippage_bps))

    def as_dict(self) -> dict[str, Any]:
        return {
            "order_size_usd": _json_float(self.order_size_usd),
            "bid_slippage_bps": _json_float(self.bid_slippage_bps),
            "ask_slippage_bps": _json_float(self.ask_slippage_bps),
            "max_slippage_bps": _json_float(self.max_slippage_bps),
            "bid_depth_usd": _json_float(self.bid_depth_usd),
            "ask_depth_usd": _json_float(self.ask_depth_usd),
            "source": self.source,
            "fetched_at": self.fetched_at,
        }


@dataclass(frozen=True)
class AdlLiquidationMetadataEvidence:
    """Whether metadata is sufficient for a no-live-order ADL/liquidation replay."""

    adl_present: bool
    liquidation_present: bool
    margin_tiers_present: bool
    source_urls: tuple[str, ...] = ()
    details: Mapping[str, Any] = field(default_factory=dict)

    @property
    def sufficient(self) -> bool:
        return bool(self.adl_present and self.liquidation_present and self.margin_tiers_present)

    def as_dict(self) -> dict[str, Any]:
        return {
            "adl_present": bool(self.adl_present),
            "liquidation_present": bool(self.liquidation_present),
            "margin_tiers_present": bool(self.margin_tiers_present),
            "sufficient": self.sufficient,
            "source_urls": list(self.source_urls),
            "details": dict(self.details),
        }


@dataclass(frozen=True)
class OpportunityWindow:
    start_index: int
    end_index: int
    start_timestamp_ms: int
    end_timestamp_ms: int
    carry_rate_sum: float
    gross_carry_usd: float
    threshold_usd: float
    edge_usd: float
    qualifying: bool

    def as_dict(self) -> dict[str, Any]:
        return {
            "start_index": self.start_index,
            "end_index": self.end_index,
            "start_timestamp_ms": self.start_timestamp_ms,
            "end_timestamp_ms": self.end_timestamp_ms,
            "carry_rate_sum": _json_float(self.carry_rate_sum),
            "gross_carry_usd": _json_float(self.gross_carry_usd),
            "threshold_usd": _json_float(self.threshold_usd),
            "edge_usd": _json_float(self.edge_usd),
            "qualifying": bool(self.qualifying),
        }


@dataclass(frozen=True)
class OpportunityCluster:
    start_timestamp_ms: int
    end_timestamp_ms: int
    positive_edge_usd: float
    window_count: int

    def as_dict(self) -> dict[str, Any]:
        return {
            "start_timestamp_ms": self.start_timestamp_ms,
            "end_timestamp_ms": self.end_timestamp_ms,
            "positive_edge_usd": _json_float(self.positive_edge_usd),
            "window_count": int(self.window_count),
        }


@dataclass(frozen=True)
class OpportunityStats:
    total_windows: int
    qualifying_windows: int
    clusters: tuple[OpportunityCluster, ...]
    max_single_week_share: float
    windows: tuple[OpportunityWindow, ...] = ()
    weekly_positive_edge_usd: Mapping[str, float] = field(default_factory=dict)

    @property
    def qualifying_pct(self) -> float:
        return float(self.qualifying_windows / self.total_windows) if self.total_windows else 0.0

    @property
    def cluster_count(self) -> int:
        return len(self.clusters)

    def as_dict(self, *, include_windows: bool = False) -> dict[str, Any]:
        out: dict[str, Any] = {
            "total_windows": int(self.total_windows),
            "qualifying_windows": int(self.qualifying_windows),
            "qualifying_pct": self.qualifying_pct,
            "cluster_count": self.cluster_count,
            "max_single_week_share": _json_float(self.max_single_week_share),
            "weekly_positive_edge_usd": {
                str(k): _json_float(v) for k, v in self.weekly_positive_edge_usd.items()
            },
            "clusters": [cluster.as_dict() for cluster in self.clusters],
        }
        if include_windows:
            out["windows"] = [window.as_dict() for window in self.windows]
        return out


@dataclass(frozen=True)
class CandidateEvidence:
    """All measured inputs needed to apply the pre-registered A2 bar."""

    venue: str
    symbol: str
    candidate_type: str
    funding_history: tuple[FundingObservation, ...]
    cadence_hours: float | None
    fee_schedule: FeeScheduleEvidence | None
    depth_estimates: tuple[DepthSlippageEstimate, ...]
    opportunity_stats: OpportunityStats | None
    adl_liquidation_metadata: AdlLiquidationMetadataEvidence | None
    borrow_basis_risk_present: bool = False
    cex_comparison_present: bool = False
    measured_evidence: bool = True
    roadmap_apr_claim: str | None = None
    notes: tuple[str, ...] = ()

    def as_dict(self, *, include_windows: bool = False) -> dict[str, Any]:
        return {
            "venue": self.venue,
            "symbol": self.symbol,
            "candidate_type": self.candidate_type,
            "funding_history_rows": len(self.funding_history),
            "funding_history_days": _json_float(funding_history_days(self.funding_history)),
            "funding_first_timestamp_ms": (
                min(row.timestamp_ms for row in self.funding_history)
                if self.funding_history
                else None
            ),
            "funding_last_timestamp_ms": (
                max(row.timestamp_ms for row in self.funding_history)
                if self.funding_history
                else None
            ),
            "cadence_hours": _json_float(self.cadence_hours),
            "fee_schedule": self.fee_schedule.as_dict() if self.fee_schedule else None,
            "depth_estimates": [estimate.as_dict() for estimate in self.depth_estimates],
            "opportunity_stats": (
                self.opportunity_stats.as_dict(include_windows=include_windows)
                if self.opportunity_stats
                else None
            ),
            "adl_liquidation_metadata": (
                self.adl_liquidation_metadata.as_dict()
                if self.adl_liquidation_metadata
                else None
            ),
            "borrow_basis_risk_present": bool(self.borrow_basis_risk_present),
            "cex_comparison_present": bool(self.cex_comparison_present),
            "measured_evidence": bool(self.measured_evidence),
            "roadmap_apr_claim": self.roadmap_apr_claim,
            "notes": list(self.notes),
        }


def evaluate_a2_candidate(
    evidence: CandidateEvidence,
    a2_bar: Mapping[str, Any],
) -> tuple[bool, list[str]]:
    """Apply PLAN_A2_BAR exactly to one candidate's measured evidence."""

    reasons: list[str] = []
    required = set(a2_bar.get("requires", ()))

    if evidence.roadmap_apr_claim and not evidence.measured_evidence:
        if not bool(a2_bar.get("roadmap_apr_claims_authorize", False)):
            reasons.append(REASON_ROADMAP_APR_CLAIM_NOT_AUTHORIZING)

    if not evidence.funding_history:
        reasons.append(REASON_MISSING_FUNDING_HISTORY)
    elif funding_history_days(evidence.funding_history) < float(
        a2_bar.get("min_days_funding_history", 90)
    ):
        reasons.append(REASON_SHORT_FUNDING_HISTORY)

    if (
        evidence.cadence_hours is None
        or not math.isfinite(float(evidence.cadence_hours))
        or float(evidence.cadence_hours) <= 0.0
    ):
        reasons.append(REASON_MISSING_CADENCE)

    if evidence.fee_schedule is None or not evidence.fee_schedule.known:
        reasons.append(REASON_MISSING_FEE_SCHEDULE)

    if not _has_required_depth(evidence.depth_estimates, a2_bar):
        reasons.append(REASON_MISSING_DEPTH_LIQUIDITY)
    elif _primary_depth_estimate(evidence.depth_estimates, a2_bar).max_slippage_bps > float(
        a2_bar.get("max_slippage_bps_per_leg", 5.0)
    ):
        reasons.append(REASON_SLIPPAGE_TOO_HIGH)

    stats = evidence.opportunity_stats
    if stats is None:
        reasons.append(REASON_MISSING_MEASURED_OPPORTUNITY_STATS)
    else:
        if stats.qualifying_pct < float(a2_bar.get("min_qualifying_24h_pct", 0.10)):
            reasons.append(REASON_QUALIFYING_24H_PCT_BELOW_BAR)
        if stats.qualifying_windows < int(a2_bar.get("min_qualifying_24h_windows", 30)):
            reasons.append(REASON_QUALIFYING_24H_WINDOWS_BELOW_BAR)
        if stats.cluster_count < int(a2_bar.get("min_clusters", 6)):
            reasons.append(REASON_OPPORTUNITY_CLUSTERS_BELOW_BAR)
        if stats.max_single_week_share > float(a2_bar.get("max_single_week_share", 0.40)):
            reasons.append(REASON_WEEKLY_CONCENTRATION_TOO_HIGH)

    if (
        evidence.adl_liquidation_metadata is None
        or not evidence.adl_liquidation_metadata.sufficient
    ):
        reasons.append(REASON_MISSING_ADL_LIQUIDATION_METADATA)

    if "borrow_basis_risk" in required and not evidence.borrow_basis_risk_present:
        reasons.append(REASON_MISSING_BORROW_BASIS_RISK)
    if "cex_comparison" in required and not evidence.cex_comparison_present:
        reasons.append(REASON_MISSING_CEX_COMPARISON)

    return (not reasons, reasons)


def compute_rolling_24h_opportunity_stats(
    funding_history: Sequence[FundingObservation],
    *,
    cost_per_window_usd: float,
    equity_usd: float,
    window_hours: float = 24.0,
    use_absolute_carry: bool = False,
) -> OpportunityStats:
    """Compute qualifying rolling windows, de-overlapped clusters, and week concentration.

    ``cost_per_window_usd`` is the full authorizing hurdle for the 24h window:
    measured taker round-trip cost plus the pre-registered safety margin. When
    ``use_absolute_carry`` is false, only positive funding sums are counted as
    harvestable gross carry; this is the conservative existing-strategy direction.
    """

    if not math.isfinite(float(cost_per_window_usd)) or float(cost_per_window_usd) < 0.0:
        threshold = math.inf
    else:
        threshold = float(cost_per_window_usd)
    if not math.isfinite(float(equity_usd)) or float(equity_usd) < 0.0:
        raise ValueError("equity_usd must be finite and non-negative")
    if not math.isfinite(float(window_hours)) or float(window_hours) <= 0.0:
        raise ValueError("window_hours must be finite and positive")

    rows = tuple(sorted(funding_history, key=lambda row: row.timestamp_ms))
    windows: list[OpportunityWindow] = []
    eps = 1e-9
    for start in range(len(rows)):
        cumulative_hours = 0.0
        rate_sum = 0.0
        for end in range(start, len(rows)):
            row = rows[end]
            interval = float(row.interval_hours)
            if not math.isfinite(interval) or interval <= 0.0:
                break
            if cumulative_hours + interval > float(window_hours) + eps:
                break
            cumulative_hours += interval
            rate_sum += float(row.rate)
            if abs(cumulative_hours - float(window_hours)) <= eps:
                carry_rate = abs(rate_sum) if use_absolute_carry else max(rate_sum, 0.0)
                gross_usd = float(carry_rate * float(equity_usd))
                edge = float(gross_usd - threshold)
                windows.append(
                    OpportunityWindow(
                        start_index=start,
                        end_index=end,
                        start_timestamp_ms=int(rows[start].timestamp_ms),
                        end_timestamp_ms=int(
                            rows[end].timestamp_ms + round(interval * HOUR_MS)
                        ),
                        carry_rate_sum=float(carry_rate),
                        gross_carry_usd=gross_usd,
                        threshold_usd=threshold,
                        edge_usd=edge,
                        qualifying=edge > 0.0,
                    )
                )
                break
    qualifying = tuple(window for window in windows if window.qualifying)
    clusters = _cluster_qualifying_windows(qualifying)
    weekly, max_week_share = _weekly_concentration(clusters)
    return OpportunityStats(
        total_windows=len(windows),
        qualifying_windows=len(qualifying),
        windows=tuple(windows),
        clusters=clusters,
        max_single_week_share=max_week_share,
        weekly_positive_edge_usd=weekly,
    )


def funding_history_days(rows: Sequence[FundingObservation]) -> float:
    """Return source coverage in calendar days, including the final interval."""

    if not rows:
        return 0.0
    ordered = sorted(rows, key=lambda row: row.timestamp_ms)
    first = ordered[0].timestamp_ms
    last = ordered[-1].timestamp_ms
    last_interval_ms = max(0.0, float(ordered[-1].interval_hours)) * HOUR_MS
    return float((last + last_interval_ms - first) / DAY_MS)


def taker_round_trip_cost_usd_for_two_legs(
    *,
    per_leg_notional_usd: float,
    first_leg_taker_fee_bps: float,
    second_leg_taker_fee_bps: float,
) -> float:
    """Return entry+exit taker fees for two equal-sized legs using the G002 helper."""

    settings = SimpleNamespace(
        slippage_base_bps=0.0,
        slippage_impact_bps_per_pct_volume=0.0,
        slippage_cap_bps=0.0,
    )
    return round_trip_cost_usd_with_fee_bps(
        spot_notional=per_leg_notional_usd,
        perp_notional=per_leg_notional_usd,
        spot_volume_notional=1_000_000_000.0,
        perp_volume_notional=1_000_000_000.0,
        settings=settings,
        spot_fee_bps=first_leg_taker_fee_bps,
        perp_fee_bps=second_leg_taker_fee_bps,
    )


def round_trip_slippage_cost_usd_for_two_legs(
    *,
    per_leg_notional_usd: float,
    first_leg_slippage_bps: float,
    second_leg_slippage_bps: float,
) -> float:
    if (
        not math.isfinite(float(first_leg_slippage_bps))
        or not math.isfinite(float(second_leg_slippage_bps))
    ):
        return math.inf
    return float(
        2.0
        * float(per_leg_notional_usd)
        * (float(first_leg_slippage_bps) + float(second_leg_slippage_bps))
        / 10_000.0
    )


def a2_cost_threshold_usd(
    *,
    per_leg_notional_usd: float,
    first_leg_taker_fee_bps: float,
    second_leg_taker_fee_bps: float,
    first_leg_slippage_bps: float,
    second_leg_slippage_bps: float,
    equity_usd: float,
    safety_margin_bps: float,
) -> dict[str, float]:
    fee = taker_round_trip_cost_usd_for_two_legs(
        per_leg_notional_usd=per_leg_notional_usd,
        first_leg_taker_fee_bps=first_leg_taker_fee_bps,
        second_leg_taker_fee_bps=second_leg_taker_fee_bps,
    )
    slippage = round_trip_slippage_cost_usd_for_two_legs(
        per_leg_notional_usd=per_leg_notional_usd,
        first_leg_slippage_bps=first_leg_slippage_bps,
        second_leg_slippage_bps=second_leg_slippage_bps,
    )
    safety = safety_margin_usd(notional=equity_usd, safety_margin_bps=safety_margin_bps)
    return {
        "fee_usd": float(fee),
        "slippage_usd": float(slippage),
        "safety_margin_usd": float(safety),
        "threshold_usd": float(fee + slippage + safety),
    }


def _has_required_depth(
    estimates: Sequence[DepthSlippageEstimate], a2_bar: Mapping[str, Any]
) -> bool:
    by_size = {round(float(estimate.order_size_usd), 8) for estimate in estimates}
    required = {round(float(size), 8) for size in a2_bar.get("depth_per_leg_usd", (250.0, 500.0))}
    return required.issubset(by_size)


def _primary_depth_estimate(
    estimates: Sequence[DepthSlippageEstimate], a2_bar: Mapping[str, Any]
) -> DepthSlippageEstimate:
    primary = max(float(size) for size in a2_bar.get("depth_per_leg_usd", (250.0, 500.0)))
    matches = [
        estimate
        for estimate in estimates
        if round(float(estimate.order_size_usd), 8) == round(primary, 8)
    ]
    if not matches:
        raise ValueError(f"missing primary depth estimate for {primary}")
    return matches[0]


def _cluster_qualifying_windows(
    qualifying: Sequence[OpportunityWindow],
) -> tuple[OpportunityCluster, ...]:
    clusters: list[OpportunityCluster] = []
    for window in sorted(
        qualifying,
        key=lambda item: (item.start_timestamp_ms, item.end_timestamp_ms),
    ):
        edge = max(float(window.edge_usd), 0.0)
        if not clusters or window.start_timestamp_ms >= clusters[-1].end_timestamp_ms:
            clusters.append(
                OpportunityCluster(
                    start_timestamp_ms=int(window.start_timestamp_ms),
                    end_timestamp_ms=int(window.end_timestamp_ms),
                    positive_edge_usd=edge,
                    window_count=1,
                )
            )
            continue
        prior = clusters[-1]
        clusters[-1] = OpportunityCluster(
            start_timestamp_ms=prior.start_timestamp_ms,
            end_timestamp_ms=max(prior.end_timestamp_ms, int(window.end_timestamp_ms)),
            positive_edge_usd=float(prior.positive_edge_usd + edge),
            window_count=prior.window_count + 1,
        )
    return tuple(clusters)


def _weekly_concentration(
    clusters: Sequence[OpportunityCluster],
) -> tuple[dict[str, float], float]:
    weekly: dict[str, float] = {}
    total = 0.0
    for cluster in clusters:
        edge = max(float(cluster.positive_edge_usd), 0.0)
        if edge <= 0.0:
            continue
        dt = datetime.fromtimestamp(cluster.start_timestamp_ms / 1000.0, UTC)
        iso = dt.isocalendar()
        key = f"{iso.year}-W{iso.week:02d}"
        weekly[key] = weekly.get(key, 0.0) + edge
        total += edge
    if total <= 0.0:
        return weekly, 0.0
    return weekly, max(weekly.values()) / total


def _json_float(value: float | int | None) -> float | None:
    if value is None:
        return None
    out = float(value)
    return out if math.isfinite(out) else None

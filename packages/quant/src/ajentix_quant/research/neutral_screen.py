"""Deterministic edge SCREEN for alternative market-neutral strategies.

Primary hypothesis (data-driven, from the strategy-v2 NO_GO post-mortem): the funding
edge that is absent on Bybit/HL majors lives in the **cross-venue funding-rate spread on
alts**. A delta-neutral long-one-venue / short-other-venue perp pair collects
``|funding_A - funding_B|`` with no spot-borrow leg. Single alts are spike-concentrated
(the strategy-v2 G003 failure), so the screen tests whether a **diversified basket**
de-concentrates the weekly edge below the bar.

This module is pure/stdlib-only and reuses the strategy-v2 cost + rolling-window helpers.
Network collection, order-book slippage measurement, and report I/O live in ``scripts/``.

This is a SCREEN, not an authorization: a PROMISING verdict only justifies opening a
fully pre-registered Phase-3 build (it does NOT authorize live capital).
"""

from __future__ import annotations

import bisect
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from ajentix_quant.research.venue_evidence import (
    FundingObservation,
    OpportunityStats,
    compute_rolling_24h_opportunity_stats,
)

SCREEN_SCHEMA_VERSION = "neutral-edge-screen-v1"

# Screen verdicts. PROMISING_BUILD only warrants a pre-registered build, never live capital.
VERDICT_PROMISING_BUILD = "PROMISING_BUILD"
VERDICT_NO_GO = "NO_GO"
VERDICT_INCONCLUSIVE = "INCONCLUSIVE"

# Per-candidate cross-venue bar (taker, executable per-name clip).
# NOTE: cluster-count / per-name weekly-concentration bars are deliberately NOT applied
# per name: unlike the episodic G003 harvest, steady cross-venue carry is *good* when
# continuous (it shows as one cluster / one cluster-week and would mis-fail those bars).
# Concentration risk is enforced at the PORTFOLIO level, where the basket hypothesis lives.
CROSS_VENUE_BAR: dict[str, Any] = {
    "min_days_history": 90.0,
    "min_direction_stability": 0.70,
    "max_slippage_bps_per_leg": 5.0,
    "min_qualifying_24h_pct": 0.10,
    "min_qualifying_24h_windows": 30,
    "per_name_notional_usd": 250.0,
    "safety_margin_bps": 1.0,
}

# Portfolio (basket) bar: the diversification hypothesis the single-name failures motivate.
PORTFOLIO_BAR: dict[str, Any] = {
    "min_qualifying_names": 5,
    "max_portfolio_single_week_share": 0.40,
    "min_partial_names_with_positive_edge": 8,
}

REASON_SHORT_HISTORY = "SHORT_FUNDING_HISTORY"
REASON_DIRECTION_UNSTABLE = "DIRECTION_UNSTABLE"
REASON_SLIPPAGE_TOO_HIGH = "SLIPPAGE_TOO_HIGH"
REASON_MISSING_STATS = "MISSING_MEASURED_STATS"
REASON_QUAL_PCT_BELOW = "QUALIFYING_24H_PCT_BELOW_BAR"
REASON_QUAL_WINDOWS_BELOW = "QUALIFYING_24H_WINDOWS_BELOW_BAR"

REASON_PORTFOLIO_TOO_FEW_NAMES = "PORTFOLIO_TOO_FEW_QUALIFYING_NAMES"
REASON_PORTFOLIO_CONCENTRATION = "PORTFOLIO_CONCENTRATION_TOO_HIGH"
REASON_NO_POSITIVE_EDGE = "NO_POSITIVE_EDGE_NAMES"


@dataclass(frozen=True)
class CrossVenueEvidence:
    """Measured evidence for one alt's cross-venue funding-spread harvest."""

    base: str
    long_venue: str
    short_venue: str
    spread_rows: tuple[FundingObservation, ...]
    stats: OpportunityStats | None
    direction_stability_pct: float
    max_slippage_bps_per_leg: float
    history_days: float
    mean_collectible_apr_pct: float


def align_cross_venue_spread(
    venue_a_rows: Sequence[FundingObservation],
    venue_b_rows: Sequence[FundingObservation],
    *,
    source: str = "cross_venue_spread",
) -> tuple[FundingObservation, ...]:
    """Build a per-hour SIGNED funding-spread series between two venues.

    Each row's ``rate`` is the per-hour funding differential
    ``a_per_hour - b_per_hour`` (each venue's settlement rate divided by its interval).
    The favorable delta-neutral direction is ``sign(rate)``; the magnitude is the
    collectible carry per hour for a position held in that direction. Venue B (typically
    the coarser CEX 8h cadence) is sampled by last-known value at each venue-A timestamp.
    """

    if not venue_a_rows or not venue_b_rows:
        return ()
    b_sorted = sorted(venue_b_rows, key=lambda r: r.timestamp_ms)
    b_ts = [r.timestamp_ms for r in b_sorted]
    out: list[FundingObservation] = []
    for a in sorted(venue_a_rows, key=lambda r: r.timestamp_ms):
        idx = bisect.bisect_right(b_ts, a.timestamp_ms) - 1
        if idx < 0:
            continue
        b = b_sorted[idx]
        a_interval = max(float(a.interval_hours), 1e-9)
        b_interval = max(float(b.interval_hours), 1e-9)
        a_per_hour = float(a.rate) / a_interval
        b_per_hour = float(b.rate) / b_interval
        out.append(
            FundingObservation(
                timestamp_ms=int(a.timestamp_ms),
                rate=a_per_hour - b_per_hour,
                interval_hours=1.0,
                source=source,
            )
        )
    return tuple(out)


def direction_stability_pct(spread_rows: Sequence[FundingObservation]) -> float:
    """Fraction (0..1) of nonzero intervals that match the modal favorable direction.

    Low stability means the favorable long/short leg flips often, so a held position
    would repeatedly pay round-trip costs to re-orient — that erodes the edge.
    """

    signs = [1 if r.rate > 0 else -1 for r in spread_rows if r.rate != 0.0]
    if not signs:
        return 0.0
    modal = 1 if sum(signs) >= 0 else -1
    return sum(1 for s in signs if s == modal) / len(signs)


def mean_collectible_apr_pct(spread_rows: Sequence[FundingObservation]) -> float:
    """Annualized mean collectible carry (|per-hour spread|, held in favorable direction)."""

    if not spread_rows:
        return 0.0
    mean_abs_hourly = sum(abs(float(r.rate)) for r in spread_rows) / len(spread_rows)
    return mean_abs_hourly * 24.0 * 365.0 * 100.0


def cross_venue_spread_stats(
    spread_rows: Sequence[FundingObservation],
    *,
    cost_per_window_usd: float,
    per_name_notional_usd: float,
    hold_window_hours: float = 168.0,
) -> OpportunityStats:
    """Rolling hold-window qualifying stats for the held-direction net collectible.

    ``hold_window_hours`` is the assumed position HOLD (default 7 days). Funding carry
    accrues across the whole window while ``cost_per_window_usd`` charges exactly ONE
    round-trip per held window — the faithful model for a position rotated occasionally,
    not re-opened daily. ``use_absolute_carry=True`` sums the signed spread over the
    window then takes the magnitude (net carry of the window's dominant direction,
    conservatively cancelling intra-window sign flips).
    """

    return compute_rolling_24h_opportunity_stats(
        spread_rows,
        cost_per_window_usd=cost_per_window_usd,
        equity_usd=per_name_notional_usd,
        window_hours=hold_window_hours,
        use_absolute_carry=True,
    )


def evaluate_cross_venue_candidate(
    evidence: CrossVenueEvidence,
    bar: Mapping[str, Any],
) -> tuple[bool, list[str]]:
    """Apply the per-name cross-venue bar; clears only when every condition holds."""

    reasons: list[str] = []
    if evidence.history_days < float(bar.get("min_days_history", 90.0)):
        reasons.append(REASON_SHORT_HISTORY)
    if evidence.direction_stability_pct < float(bar.get("min_direction_stability", 0.70)):
        reasons.append(REASON_DIRECTION_UNSTABLE)
    if evidence.max_slippage_bps_per_leg > float(bar.get("max_slippage_bps_per_leg", 5.0)):
        reasons.append(REASON_SLIPPAGE_TOO_HIGH)

    stats = evidence.stats
    if stats is None:
        reasons.append(REASON_MISSING_STATS)
    else:
        total = stats.total_windows or 1
        qual_pct = stats.qualifying_windows / total
        if qual_pct < float(bar.get("min_qualifying_24h_pct", 0.10)):
            reasons.append(REASON_QUAL_PCT_BELOW)
        if stats.qualifying_windows < int(bar.get("min_qualifying_24h_windows", 30)):
            reasons.append(REASON_QUAL_WINDOWS_BELOW)
    return (not reasons, reasons)


def split_spread_at(
    spread_rows: Sequence[FundingObservation],
    boundary_ms: int,
) -> tuple[tuple[FundingObservation, ...], tuple[FundingObservation, ...]]:
    """Split a spread series into disjoint (train, test) at ``boundary_ms`` (test = >= boundary)."""

    train = tuple(r for r in spread_rows if r.timestamp_ms < boundary_ms)
    test = tuple(r for r in spread_rows if r.timestamp_ms >= boundary_ms)
    return train, test


def walk_forward_survival(
    *,
    train_clearing: Sequence[str],
    test_clearing: Sequence[str],
    test_portfolio: Mapping[str, Any],
    min_surviving_names: int = 5,
    max_test_week_share: float = 0.40,
) -> dict[str, Any]:
    """Out-of-sample survival of the train-selected basket.

    The anti-overfit test: of the names that clear the bar IN TRAIN, how many still clear
    in the disjoint TEST window, and does the surviving basket stay de-concentrated in
    test? An in-sample-only artifact (the strategy-v2 failure mode) collapses here.
    """

    train_set = set(train_clearing)
    test_set = set(test_clearing)
    surviving = sorted(train_set & test_set)
    decayed = sorted(train_set - test_set)
    survival_rate = (len(surviving) / len(train_set)) if train_set else 0.0
    test_share = float(test_portfolio.get("max_single_week_share", 1.0))

    reasons: list[str] = []
    if len(surviving) < int(min_surviving_names):
        reasons.append(REASON_PORTFOLIO_TOO_FEW_NAMES)
    if test_share > float(max_test_week_share):
        reasons.append(REASON_PORTFOLIO_CONCENTRATION)
    verdict = VERDICT_PROMISING_BUILD if not reasons else VERDICT_NO_GO

    return {
        "verdict": verdict,
        "reasons": reasons,
        "train_clearing_names": sorted(train_set),
        "test_clearing_names": sorted(test_set),
        "surviving_names": surviving,
        "decayed_names": decayed,
        "survival_rate": survival_rate,
        "test_max_single_week_share": test_share,
    }


def _iso_week(ts_ms: int) -> str:
    dt = datetime.fromtimestamp(ts_ms / 1000.0, tz=UTC)
    year, week, _ = dt.isocalendar()
    return f"{year}-W{week:02d}"


def aggregate_portfolio_concentration(
    per_name_stats: Mapping[str, OpportunityStats],
) -> dict[str, Any]:
    """Pool qualifying-window edge across names by ISO week; the basket de-concentration test.

    Single alts spike (one week dominates); a basket whose names spike in different weeks
    should drop the pooled max-single-week share below the bar even when each name fails
    it alone.
    """

    weekly: dict[str, float] = {}
    names_with_positive_edge = 0
    for stats in per_name_stats.values():
        name_edge = 0.0
        for window in stats.windows:
            if window.qualifying and window.edge_usd > 0.0:
                weekly[_iso_week(window.start_timestamp_ms)] = (
                    weekly.get(_iso_week(window.start_timestamp_ms), 0.0) + window.edge_usd
                )
                name_edge += window.edge_usd
        if name_edge > 0.0:
            names_with_positive_edge += 1

    total = sum(weekly.values())
    max_share = (max(weekly.values()) / total) if total > 0.0 else 0.0
    return {
        "weekly_edge_usd": dict(sorted(weekly.items())),
        "total_edge_usd": total,
        "max_single_week_share": max_share,
        "names_with_positive_edge": names_with_positive_edge,
        "weeks_with_edge": len(weekly),
    }


def screen_verdict(
    *,
    clearing_names: Sequence[str],
    portfolio: Mapping[str, Any],
    history_sufficient: bool,
    portfolio_bar: Mapping[str, Any] = PORTFOLIO_BAR,
) -> tuple[str, list[str]]:
    """Decide the basket screen verdict from per-name clears + portfolio concentration."""

    if not history_sufficient:
        return VERDICT_INCONCLUSIVE, [REASON_SHORT_HISTORY]

    reasons: list[str] = []
    n_clear = len(clearing_names)
    min_names = int(portfolio_bar.get("min_qualifying_names", 5))
    if n_clear < min_names:
        reasons.append(REASON_PORTFOLIO_TOO_FEW_NAMES)

    max_share = float(portfolio.get("max_single_week_share", 1.0))
    if max_share > float(portfolio_bar.get("max_portfolio_single_week_share", 0.40)):
        reasons.append(REASON_PORTFOLIO_CONCENTRATION)

    if int(portfolio.get("names_with_positive_edge", 0)) == 0:
        reasons.append(REASON_NO_POSITIVE_EDGE)

    if not reasons:
        return VERDICT_PROMISING_BUILD, []

    # A NO_GO needs measured positive-edge names that still fail the basket bar; with zero
    # positive-edge names the measured universe simply shows no edge (also NO_GO).
    return VERDICT_NO_GO, reasons

def screen_verdict_multi_hold(
    *,
    clears_by_hold: Mapping[str, Sequence[str]],
    portfolio_by_hold: Mapping[str, Mapping[str, Any]],
    hold_order: Sequence[str],
    history_sufficient: bool,
    portfolio_bar: Mapping[str, Any] = PORTFOLIO_BAR,
) -> dict[str, Any]:
    """Hold-horizon-aware verdict for a carry strategy.

    A funding-carry position is held for weeks, so the round-trip cost amortizes over the
    hold. ``hold_order`` lists the PRE-DECLARED candidate holds (shortest first). The
    verdict is PROMISING_BUILD at the SHORTEST hold whose basket clears; the dependency on
    that minimum hold is reported explicitly so a Phase-3 build pre-commits + walk-forward
    validates it. Evaluating only pre-declared holds keeps this from being post-hoc tuning.
    """

    per_hold: dict[str, Any] = {}
    passing_hold: str | None = None
    for label in hold_order:
        verdict, reasons = screen_verdict(
            clearing_names=clears_by_hold.get(label, ()),
            portfolio=portfolio_by_hold.get(label, {}),
            history_sufficient=history_sufficient,
            portfolio_bar=portfolio_bar,
        )
        per_hold[label] = {
            "verdict": verdict,
            "reasons": reasons,
            "clearing_names": list(clears_by_hold.get(label, ())),
        }
        if verdict == VERDICT_PROMISING_BUILD and passing_hold is None:
            passing_hold = label

    if not history_sufficient:
        return {
            "verdict": VERDICT_INCONCLUSIVE,
            "reasons": [REASON_SHORT_HISTORY],
            "minimum_passing_hold": None,
            "per_hold": per_hold,
        }
    if passing_hold is not None:
        shortest = hold_order[0] if hold_order else passing_hold
        return {
            "verdict": VERDICT_PROMISING_BUILD,
            "reasons": [],
            "minimum_passing_hold": passing_hold,
            "hold_horizon_dependent": passing_hold != shortest,
            "per_hold": per_hold,
        }
    # No pre-declared hold clears -> NO_GO; surface the deepest-hold reasons.
    last_reasons: list[str] = [REASON_PORTFOLIO_TOO_FEW_NAMES]
    if hold_order:
        last_reasons = per_hold.get(hold_order[-1], {}).get("reasons", last_reasons)
    return {
        "verdict": VERDICT_NO_GO,
        "reasons": last_reasons,
        "minimum_passing_hold": None,
        "per_hold": per_hold,
    }


def screen_history_sufficient(
    per_name_history_days: Mapping[str, float],
    *,
    min_days: float = 90.0,
    min_names: int = 5,
) -> bool:
    enough = [d for d in per_name_history_days.values() if d >= min_days]
    return len(enough) >= min_names

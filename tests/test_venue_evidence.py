from __future__ import annotations

import pytest

from ajentix_quant.research.preregistration import PLAN_A2_BAR
from ajentix_quant.research.venue_evidence import (
    REASON_MISSING_ADL_LIQUIDATION_METADATA,
    REASON_OPPORTUNITY_CLUSTERS_BELOW_BAR,
    REASON_QUALIFYING_24H_PCT_BELOW_BAR,
    REASON_QUALIFYING_24H_WINDOWS_BELOW_BAR,
    REASON_ROADMAP_APR_CLAIM_NOT_AUTHORIZING,
    REASON_SHORT_FUNDING_HISTORY,
    REASON_SLIPPAGE_TOO_HIGH,
    REASON_WEEKLY_CONCENTRATION_TOO_HIGH,
    AdlLiquidationMetadataEvidence,
    CandidateEvidence,
    DepthSlippageEstimate,
    FeeScheduleEvidence,
    FundingObservation,
    OpportunityCluster,
    OpportunityStats,
    compute_rolling_24h_opportunity_stats,
    evaluate_a2_candidate,
    funding_history_days,
)

HOUR_MS = 60 * 60 * 1000
DAY_HOURS = 24


def _funding_rows(days: int = 91, *, rate: float = 0.0001) -> tuple[FundingObservation, ...]:
    return tuple(
        FundingObservation(
            timestamp_ms=i * HOUR_MS,
            rate=rate,
            interval_hours=1.0,
            source="venue",
        )
        for i in range(days * DAY_HOURS)
    )


def _fee() -> FeeScheduleEvidence:
    return FeeScheduleEvidence(
        venue="hyperliquid",
        taker_fee_bps=4.5,
        maker_fee_bps=1.5,
        source_url="https://hyperliquid.gitbook.io/hyperliquid-docs/trading/fees",
        source_note="Base tier perps taker fee.",
    )


def _depth(*, primary_slippage_bps: float = 1.0) -> tuple[DepthSlippageEstimate, ...]:
    return (
        DepthSlippageEstimate(
            order_size_usd=250.0,
            bid_slippage_bps=1.0,
            ask_slippage_bps=1.0,
            bid_depth_usd=10_000.0,
            ask_depth_usd=10_000.0,
            source="venue_order_book",
            fetched_at="2026-06-19T00:00:00Z",
        ),
        DepthSlippageEstimate(
            order_size_usd=500.0,
            bid_slippage_bps=primary_slippage_bps,
            ask_slippage_bps=primary_slippage_bps,
            bid_depth_usd=10_000.0,
            ask_depth_usd=10_000.0,
            source="venue_order_book",
            fetched_at="2026-06-19T00:00:00Z",
        ),
    )


def _clusters(n: int, *, edge: float = 1.0) -> tuple[OpportunityCluster, ...]:
    return tuple(
        OpportunityCluster(
            start_timestamp_ms=i * 8 * DAY_HOURS * HOUR_MS,
            end_timestamp_ms=i * 8 * DAY_HOURS * HOUR_MS + DAY_HOURS * HOUR_MS,
            positive_edge_usd=edge,
            window_count=1,
        )
        for i in range(n)
    )


def _stats(
    *,
    total_windows: int = 300,
    qualifying_windows: int = 31,
    cluster_count: int = 6,
    max_single_week_share: float = 0.30,
) -> OpportunityStats:
    return OpportunityStats(
        total_windows=total_windows,
        qualifying_windows=qualifying_windows,
        clusters=_clusters(cluster_count),
        max_single_week_share=max_single_week_share,
        weekly_positive_edge_usd={"2026-W01": max_single_week_share},
    )


def _metadata(*, sufficient: bool = True) -> AdlLiquidationMetadataEvidence:
    return AdlLiquidationMetadataEvidence(
        adl_present=sufficient,
        liquidation_present=sufficient,
        margin_tiers_present=sufficient,
        source_urls=(
            "https://hyperliquid.gitbook.io/hyperliquid-docs/trading/auto-deleveraging",
            "https://hyperliquid.gitbook.io/hyperliquid-docs/trading/liquidations",
        ),
        details={"maxLeverage": 50},
    )


def _candidate(**overrides) -> CandidateEvidence:
    data = {
        "venue": "hyperliquid",
        "symbol": "BTC/USDC:USDC",
        "candidate_type": "hl_direct",
        "funding_history": _funding_rows(),
        "cadence_hours": 1.0,
        "fee_schedule": _fee(),
        "depth_estimates": _depth(),
        "opportunity_stats": _stats(),
        "adl_liquidation_metadata": _metadata(),
        "borrow_basis_risk_present": True,
        "cex_comparison_present": True,
        "measured_evidence": True,
        "roadmap_apr_claim": None,
        "notes": (),
    }
    data.update(overrides)
    return CandidateEvidence(**data)


def test_evaluate_a2_candidate_clears_when_all_bar_conditions_are_met() -> None:
    clears, reasons = evaluate_a2_candidate(_candidate(), PLAN_A2_BAR)

    assert clears is True
    assert reasons == []


@pytest.mark.parametrize(
    ("candidate", "reason"),
    [
        (_candidate(funding_history=_funding_rows(days=89)), REASON_SHORT_FUNDING_HISTORY),
        (_candidate(depth_estimates=_depth(primary_slippage_bps=5.01)), REASON_SLIPPAGE_TOO_HIGH),
        (
            _candidate(opportunity_stats=_stats(total_windows=1000, qualifying_windows=99)),
            REASON_QUALIFYING_24H_PCT_BELOW_BAR,
        ),
        (
            _candidate(opportunity_stats=_stats(total_windows=290, qualifying_windows=29)),
            REASON_QUALIFYING_24H_WINDOWS_BELOW_BAR,
        ),
        (
            _candidate(opportunity_stats=_stats(cluster_count=5)),
            REASON_OPPORTUNITY_CLUSTERS_BELOW_BAR,
        ),
        (
            _candidate(opportunity_stats=_stats(max_single_week_share=0.401)),
            REASON_WEEKLY_CONCENTRATION_TOO_HIGH,
        ),
        (
            _candidate(adl_liquidation_metadata=_metadata(sufficient=False)),
            REASON_MISSING_ADL_LIQUIDATION_METADATA,
        ),
    ],
)
def test_evaluate_a2_candidate_fails_each_single_bar_condition(
    candidate: CandidateEvidence,
    reason: str,
) -> None:
    clears, reasons = evaluate_a2_candidate(candidate, PLAN_A2_BAR)

    assert clears is False
    assert reason in reasons


def test_rolling_window_cluster_and_weekly_concentration_math_on_tiny_series() -> None:
    # Two non-overlapping three-hour measured opportunities in different ISO weeks.
    rates = [0.0] * 173
    rates[0:3] = [0.01, 0.01, 0.01]
    rates[170:173] = [0.01, 0.01, 0.01]
    rows = tuple(
        FundingObservation(timestamp_ms=i * HOUR_MS, rate=rate, interval_hours=1.0)
        for i, rate in enumerate(rates)
    )

    stats = compute_rolling_24h_opportunity_stats(
        rows,
        cost_per_window_usd=25.0,
        equity_usd=1000.0,
        window_hours=3.0,
    )

    assert stats.total_windows == 171
    assert stats.qualifying_windows == 2
    assert stats.qualifying_pct == pytest.approx(2 / 171)
    assert stats.cluster_count == 2
    assert [cluster.window_count for cluster in stats.clusters] == [1, 1]
    assert [cluster.positive_edge_usd for cluster in stats.clusters] == pytest.approx([5.0, 5.0])
    assert stats.max_single_week_share == pytest.approx(0.5)
    assert sum(stats.weekly_positive_edge_usd.values()) == pytest.approx(10.0)


def test_funding_history_days_counts_the_final_interval() -> None:
    rows = _funding_rows(days=90)

    assert funding_history_days(rows) == pytest.approx(90.0)


def test_roadmap_apr_claim_cannot_authorize_without_measured_evidence() -> None:
    clears, reasons = evaluate_a2_candidate(
        _candidate(
            funding_history=(),
            cadence_hours=None,
            fee_schedule=None,
            depth_estimates=(),
            opportunity_stats=None,
            adl_liquidation_metadata=None,
            borrow_basis_risk_present=False,
            cex_comparison_present=False,
            measured_evidence=False,
            roadmap_apr_claim="Roadmap says this can earn 40% APR.",
        ),
        PLAN_A2_BAR,
    )

    assert clears is False
    assert REASON_ROADMAP_APR_CLAIM_NOT_AUTHORIZING in reasons

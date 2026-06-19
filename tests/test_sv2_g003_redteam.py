from __future__ import annotations

from dataclasses import fields, replace

import pytest

from ajentix_quant.research.preregistration import PLAN_A2_BAR
from ajentix_quant.research.venue_evidence import (
    REASON_MISSING_ADL_LIQUIDATION_METADATA,
    REASON_MISSING_BORROW_BASIS_RISK,
    REASON_MISSING_CADENCE,
    REASON_MISSING_CEX_COMPARISON,
    REASON_MISSING_DEPTH_LIQUIDITY,
    REASON_MISSING_FEE_SCHEDULE,
    REASON_MISSING_MEASURED_OPPORTUNITY_STATS,
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
)

HOUR_MS = 60 * 60 * 1000
DAY_MS = 24 * HOUR_MS


def _funding_history(days: int = 91) -> tuple[FundingObservation, ...]:
    return tuple(
        FundingObservation(
            timestamp_ms=hour * HOUR_MS,
            rate=0.00002,
            interval_hours=1.0,
            source="synthetic-redteam",
        )
        for hour in range(days * 24)
    )


def _fee_schedule() -> FeeScheduleEvidence:
    return FeeScheduleEvidence(
        venue="hyperliquid",
        taker_fee_bps=3.5,
        maker_fee_bps=1.0,
        source_url="synthetic://fee-schedule",
        source_note="Synthetic no-network fee evidence for A2 bar tests.",
    )


def _depth_estimates(primary_slippage_bps: float = 4.0) -> tuple[DepthSlippageEstimate, ...]:
    return (
        DepthSlippageEstimate(
            order_size_usd=250.0,
            bid_slippage_bps=2.0,
            ask_slippage_bps=2.0,
            bid_depth_usd=20_000.0,
            ask_depth_usd=20_000.0,
            source="synthetic-book",
            fetched_at="2026-06-19T00:00:00Z",
        ),
        DepthSlippageEstimate(
            order_size_usd=500.0,
            bid_slippage_bps=primary_slippage_bps,
            ask_slippage_bps=primary_slippage_bps,
            bid_depth_usd=20_000.0,
            ask_depth_usd=20_000.0,
            source="synthetic-book",
            fetched_at="2026-06-19T00:00:00Z",
        ),
    )


def _adl_metadata() -> AdlLiquidationMetadataEvidence:
    return AdlLiquidationMetadataEvidence(
        adl_present=True,
        liquidation_present=True,
        margin_tiers_present=True,
        source_urls=("synthetic://adl", "synthetic://liquidation"),
        details={"mode": "redteam"},
    )


def _clusters(count: int) -> tuple[OpportunityCluster, ...]:
    return tuple(
        OpportunityCluster(
            start_timestamp_ms=index * 8 * DAY_MS,
            end_timestamp_ms=index * 8 * DAY_MS + DAY_MS,
            positive_edge_usd=1.0,
            window_count=1,
        )
        for index in range(count)
    )


def _opportunity_stats(
    *,
    total_windows: int = 300,
    qualifying_windows: int = 31,
    cluster_count: int = 6,
    max_single_week_share: float = 0.25,
) -> OpportunityStats:
    return OpportunityStats(
        total_windows=total_windows,
        qualifying_windows=qualifying_windows,
        clusters=_clusters(cluster_count),
        max_single_week_share=max_single_week_share,
        weekly_positive_edge_usd={
            "1970-W01": max_single_week_share,
            "1970-W02": max(0.0, 1.0 - max_single_week_share),
        },
    )


def _candidate(**overrides: object) -> CandidateEvidence:
    data: dict[str, object] = {
        "venue": "hyperliquid",
        "symbol": "BTC/USDC:USDC",
        "candidate_type": "hl_direct",
        "funding_history": _funding_history(),
        "cadence_hours": 1.0,
        "fee_schedule": _fee_schedule(),
        "depth_estimates": _depth_estimates(),
        "opportunity_stats": _opportunity_stats(),
        "adl_liquidation_metadata": _adl_metadata(),
        "borrow_basis_risk_present": True,
        "cex_comparison_present": True,
        "measured_evidence": True,
        "roadmap_apr_claim": None,
        "notes": (),
    }
    data.update(overrides)
    return CandidateEvidence(**data)  # type: ignore[arg-type]


def test_synthetic_candidate_meeting_every_a2_condition_clears() -> None:
    assert evaluate_a2_candidate(_candidate(), PLAN_A2_BAR) == (True, [])


@pytest.mark.parametrize(
    ("candidate", "expected_reason"),
    [
        (
            _candidate(funding_history=_funding_history(days=89)),
            REASON_SHORT_FUNDING_HISTORY,
        ),
        (
            _candidate(depth_estimates=_depth_estimates(primary_slippage_bps=5.0001)),
            REASON_SLIPPAGE_TOO_HIGH,
        ),
        (
            _candidate(
                opportunity_stats=_opportunity_stats(
                    total_windows=310,
                    qualifying_windows=30,
                    cluster_count=6,
                )
            ),
            REASON_QUALIFYING_24H_PCT_BELOW_BAR,
        ),
        (
            _candidate(
                opportunity_stats=_opportunity_stats(
                    total_windows=290,
                    qualifying_windows=29,
                    cluster_count=6,
                )
            ),
            REASON_QUALIFYING_24H_WINDOWS_BELOW_BAR,
        ),
        (
            _candidate(opportunity_stats=_opportunity_stats(cluster_count=5)),
            REASON_OPPORTUNITY_CLUSTERS_BELOW_BAR,
        ),
        (
            _candidate(opportunity_stats=_opportunity_stats(max_single_week_share=0.400001)),
            REASON_WEEKLY_CONCENTRATION_TOO_HIGH,
        ),
        (
            _candidate(adl_liquidation_metadata=None),
            REASON_MISSING_ADL_LIQUIDATION_METADATA,
        ),
        (
            _candidate(cex_comparison_present=False),
            REASON_MISSING_CEX_COMPARISON,
        ),
        (
            _candidate(borrow_basis_risk_present=False),
            REASON_MISSING_BORROW_BASIS_RISK,
        ),
        (
            _candidate(cadence_hours=None),
            REASON_MISSING_CADENCE,
        ),
        (
            _candidate(fee_schedule=None),
            REASON_MISSING_FEE_SCHEDULE,
        ),
        (
            _candidate(depth_estimates=_depth_estimates()[:1]),
            REASON_MISSING_DEPTH_LIQUIDITY,
        ),
        (
            _candidate(opportunity_stats=None),
            REASON_MISSING_MEASURED_OPPORTUNITY_STATS,
        ),
    ],
    ids=(
        "short-history-under-90-days",
        "primary-depth-slippage-above-5bps",
        "qualifying-window-percentage-under-10pct",
        "qualifying-window-count-under-30",
        "deoverlapped-cluster-count-under-6",
        "single-week-positive-edge-share-above-40pct",
        "missing-adl-liquidation-metadata",
        "missing-cex-comparison",
        "missing-borrow-basis-risk",
        "missing-cadence",
        "missing-fee-schedule",
        "missing-required-depth-size",
        "missing-measured-opportunity-stats",
    ),
)
def test_each_single_a2_violation_fails_without_false_clear(
    candidate: CandidateEvidence,
    expected_reason: str,
) -> None:
    clears, reasons = evaluate_a2_candidate(candidate, PLAN_A2_BAR)

    assert clears is False
    assert reasons == [expected_reason]


def test_threshold_boundaries_are_inclusive_but_exact_zero_edge_is_not_qualifying() -> None:
    boundary_candidate = _candidate(
        funding_history=_funding_history(days=90),
        depth_estimates=_depth_estimates(primary_slippage_bps=5.0),
        opportunity_stats=_opportunity_stats(
            total_windows=300,
            qualifying_windows=30,
            cluster_count=6,
            max_single_week_share=0.40,
        ),
    )

    assert evaluate_a2_candidate(boundary_candidate, PLAN_A2_BAR) == (True, [])

    exact_hurdle_rows = (
        FundingObservation(timestamp_ms=0, rate=0.001, interval_hours=1.0),
        *(
            FundingObservation(timestamp_ms=hour * HOUR_MS, rate=0.0, interval_hours=1.0)
            for hour in range(1, 24)
        ),
    )
    exact_hurdle_stats = compute_rolling_24h_opportunity_stats(
        exact_hurdle_rows,
        cost_per_window_usd=1.0,
        equity_usd=1000.0,
    )

    assert exact_hurdle_stats.total_windows == 1
    assert exact_hurdle_stats.qualifying_windows == 0
    assert exact_hurdle_stats.windows[0].edge_usd == pytest.approx(0.0)
    assert exact_hurdle_stats.windows[0].qualifying is False


def test_roadmap_apr_claim_is_not_an_authorizing_path() -> None:
    field_names = {field.name for field in fields(CandidateEvidence)}
    assert "roadmap_apr_claim" in field_names
    assert PLAN_A2_BAR["roadmap_apr_claims_authorize"] is False

    measured_non_clearing = _candidate(
        opportunity_stats=_opportunity_stats(total_windows=310, qualifying_windows=30),
    )
    with_roadmap_claim = replace(
        measured_non_clearing,
        roadmap_apr_claim="Roadmap projects 100% APR after launch.",
    )

    assert evaluate_a2_candidate(measured_non_clearing, PLAN_A2_BAR) == (
        False,
        [REASON_QUALIFYING_24H_PCT_BELOW_BAR],
    )
    assert evaluate_a2_candidate(with_roadmap_claim, PLAN_A2_BAR) == (
        False,
        [REASON_QUALIFYING_24H_PCT_BELOW_BAR],
    )

    unmeasured_roadmap_only = _candidate(
        measured_evidence=False,
        roadmap_apr_claim="Roadmap projects 100% APR after launch.",
    )
    assert evaluate_a2_candidate(unmeasured_roadmap_only, PLAN_A2_BAR) == (
        False,
        [REASON_ROADMAP_APR_CLAIM_NOT_AUTHORIZING],
    )


def _segmented_24h_rows() -> tuple[FundingObservation, ...]:
    def segment(start_timestamp_ms: int) -> tuple[FundingObservation, ...]:
        rates = [0.0] * 26
        rates[0] = 0.002
        rates[1] = 0.009
        rates[24] = 0.002
        return tuple(
            FundingObservation(
                timestamp_ms=start_timestamp_ms + offset * HOUR_MS,
                rate=rate,
                interval_hours=1.0,
                source="synthetic-redteam",
            )
            for offset, rate in enumerate(rates)
        )

    return (
        *segment(0),
        FundingObservation(
            timestamp_ms=7 * DAY_MS,
            rate=0.0,
            interval_hours=0.0,
            source="synthetic-segment-break",
        ),
        *segment(8 * DAY_MS),
    )


def test_rolling_24h_cluster_and_weekly_concentration_helper_math() -> None:
    stats = compute_rolling_24h_opportunity_stats(
        _segmented_24h_rows(),
        cost_per_window_usd=10.0,
        equity_usd=1000.0,
    )
    qualifying_starts = [window.start_index for window in stats.windows if window.qualifying]

    assert stats.total_windows == 6
    assert stats.qualifying_windows == 4
    assert stats.qualifying_pct == pytest.approx(4 / 6)
    assert qualifying_starts == [0, 1, 27, 28]
    assert stats.cluster_count == 2
    assert [cluster.window_count for cluster in stats.clusters] == [2, 2]
    assert [cluster.positive_edge_usd for cluster in stats.clusters] == pytest.approx([2.0, 2.0])
    assert stats.weekly_positive_edge_usd == pytest.approx(
        {"1970-W01": 2.0, "1970-W02": 2.0}
    )
    assert stats.max_single_week_share == pytest.approx(0.5)


def test_evaluate_and_helper_outputs_are_deterministic() -> None:
    candidate = _candidate(
        opportunity_stats=_opportunity_stats(total_windows=310, qualifying_windows=30),
        roadmap_apr_claim="Roadmap claims cannot authorize this measured miss.",
    )
    first = evaluate_a2_candidate(candidate, PLAN_A2_BAR)
    second = evaluate_a2_candidate(candidate, PLAN_A2_BAR)

    assert first == second == (False, [REASON_QUALIFYING_24H_PCT_BELOW_BAR])

    first_stats = compute_rolling_24h_opportunity_stats(
        _segmented_24h_rows(),
        cost_per_window_usd=10.0,
        equity_usd=1000.0,
    )
    second_stats = compute_rolling_24h_opportunity_stats(
        _segmented_24h_rows(),
        cost_per_window_usd=10.0,
        equity_usd=1000.0,
    )

    assert first_stats.as_dict(include_windows=True) == second_stats.as_dict(include_windows=True)


def test_every_remaining_required_field_fails_closed_singly():
    # Architect LOW: ensure EACH remaining PLAN_A2_BAR required field has a single-violation
    # failure code, so future edits cannot silently weaken fail-closed behavior.
    from ajentix_quant.research import venue_evidence as ve

    cases = [
        (_candidate(funding_history=()), ve.REASON_MISSING_FUNDING_HISTORY),
        (_candidate(cadence_hours=None), ve.REASON_MISSING_CADENCE),
        (_candidate(fee_schedule=None), ve.REASON_MISSING_FEE_SCHEDULE),
        (_candidate(depth_estimates=()), ve.REASON_MISSING_DEPTH_LIQUIDITY),
        (_candidate(opportunity_stats=None), ve.REASON_MISSING_MEASURED_OPPORTUNITY_STATS),
        (_candidate(adl_liquidation_metadata=None), ve.REASON_MISSING_ADL_LIQUIDATION_METADATA),
        (_candidate(borrow_basis_risk_present=False), ve.REASON_MISSING_BORROW_BASIS_RISK),
        (_candidate(cex_comparison_present=False), ve.REASON_MISSING_CEX_COMPARISON),
    ]
    for cand, reason in cases:
        clears, reasons = evaluate_a2_candidate(cand, PLAN_A2_BAR)
        assert clears is False, f"{reason} candidate must not clear"
        assert reason in reasons, f"expected {reason} in {reasons}"

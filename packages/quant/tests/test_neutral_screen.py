"""Tests for the alt cross-venue funding-spread edge screen (pure logic, CI-safe)."""

from __future__ import annotations

from ajentix_quant.research.neutral_screen import (
    CROSS_VENUE_BAR,
    REASON_DIRECTION_UNSTABLE,
    REASON_PORTFOLIO_TOO_FEW_NAMES,
    REASON_QUAL_PCT_BELOW,
    REASON_SHORT_HISTORY,
    REASON_SLIPPAGE_TOO_HIGH,
    VERDICT_INCONCLUSIVE,
    VERDICT_NO_GO,
    VERDICT_PROMISING_BUILD,
    CrossVenueEvidence,
    aggregate_portfolio_concentration,
    align_cross_venue_spread,
    cross_venue_spread_stats,
    direction_stability_pct,
    evaluate_cross_venue_candidate,
    mean_collectible_apr_pct,
    screen_verdict,
    screen_verdict_multi_hold,
    split_spread_at,
)
from ajentix_quant.research.venue_evidence import FundingObservation, funding_history_days

HOUR = 3_600_000


def _hourly(n: int, rate: float, start: int = 0) -> list[FundingObservation]:
    return [
        FundingObservation(timestamp_ms=start + i * HOUR, rate=rate, interval_hours=1.0)
        for i in range(n)
    ]


def _eight_hourly(n: int, rate: float, start: int = 0) -> list[FundingObservation]:
    return [
        FundingObservation(timestamp_ms=start + i * 8 * HOUR, rate=rate, interval_hours=8.0)
        for i in range(n)
    ]


# --- alignment / metrics ---------------------------------------------------------------


def test_align_converts_to_per_hour_and_samples_coarser_venue() -> None:
    a = _hourly(16, rate=0.0001)  # 1 bps/hr
    b = _eight_hourly(2, rate=0.0008)  # 8 bps per 8h => 1 bps/hr
    spread = align_cross_venue_spread(a, b)
    assert len(spread) == 16
    # per-hour spread = 0.0001 - (0.0008/8) = 0.0001 - 0.0001 = 0.0 here
    assert all(abs(r.rate) < 1e-12 for r in spread)


def test_align_signed_spread_value() -> None:
    a = _hourly(8, rate=0.0002)  # 2 bps/hr
    b = _eight_hourly(1, rate=0.0008)  # 1 bps/hr
    spread = align_cross_venue_spread(a, b)
    assert all(abs(r.rate - 0.0001) < 1e-12 for r in spread)  # 2 - 1 = 1 bps/hr


def test_align_empty_on_missing_series() -> None:
    assert align_cross_venue_spread((), _eight_hourly(1, 0.001)) == ()
    assert align_cross_venue_spread(_hourly(4, 0.001), ()) == ()


def test_direction_stability_counts_modal_sign() -> None:
    rows = _hourly(10, rate=0.0001)  # all positive
    rows += [FundingObservation(timestamp_ms=10 * HOUR + i * HOUR, rate=-0.0001, interval_hours=1.0)
             for i in range(2)]  # 2 negative
    # 10 positive vs 2 negative => modal +, stability 10/12
    assert abs(direction_stability_pct(rows) - 10 / 12) < 1e-9


def test_mean_collectible_apr_uses_absolute_value() -> None:
    rows = _hourly(100, rate=-0.0001)  # |1 bps/hr| -> 1 bps/hr * 24 * 365 = 87.6% APR
    assert abs(mean_collectible_apr_pct(rows) - 87.6) < 1e-6


# --- per-name evaluation ---------------------------------------------------------------


def _clearing_evidence() -> CrossVenueEvidence:
    spread = _hourly(24 * 100, rate=0.0001)  # persistent positive, 100 days
    stats = cross_venue_spread_stats(
        spread, cost_per_window_usd=0.05, per_name_notional_usd=250.0, hold_window_hours=168.0
    )
    return CrossVenueEvidence(
        base="X",
        long_venue="hyperliquid",
        short_venue="binanceusdm",
        spread_rows=spread,
        stats=stats,
        direction_stability_pct=direction_stability_pct(spread),
        max_slippage_bps_per_leg=3.0,
        history_days=funding_history_days(spread),
        mean_collectible_apr_pct=mean_collectible_apr_pct(spread),
    )


def test_clearing_candidate_clears() -> None:
    clears, reasons = evaluate_cross_venue_candidate(_clearing_evidence(), CROSS_VENUE_BAR)
    assert clears is True
    assert reasons == []


def test_short_history_fails() -> None:
    ev = _clearing_evidence()
    ev = CrossVenueEvidence(**{**ev.__dict__, "history_days": 30.0})
    clears, reasons = evaluate_cross_venue_candidate(ev, CROSS_VENUE_BAR)
    assert clears is False
    assert REASON_SHORT_HISTORY in reasons


def test_unstable_direction_fails() -> None:
    ev = _clearing_evidence()
    ev = CrossVenueEvidence(**{**ev.__dict__, "direction_stability_pct": 0.55})
    clears, reasons = evaluate_cross_venue_candidate(ev, CROSS_VENUE_BAR)
    assert clears is False
    assert REASON_DIRECTION_UNSTABLE in reasons


def test_high_slippage_fails() -> None:
    ev = _clearing_evidence()
    ev = CrossVenueEvidence(**{**ev.__dict__, "max_slippage_bps_per_leg": 9.0})
    clears, reasons = evaluate_cross_venue_candidate(ev, CROSS_VENUE_BAR)
    assert clears is False
    assert REASON_SLIPPAGE_TOO_HIGH in reasons


def test_low_qualifying_pct_fails() -> None:
    # Tiny spread vs a high cost => almost no window clears.
    spread = _hourly(24 * 100, rate=0.000001)
    stats = cross_venue_spread_stats(
        spread, cost_per_window_usd=10.0, per_name_notional_usd=250.0, hold_window_hours=168.0
    )
    ev = CrossVenueEvidence(
        base="X", long_venue="a", short_venue="b", spread_rows=spread, stats=stats,
        direction_stability_pct=1.0, max_slippage_bps_per_leg=1.0,
        history_days=funding_history_days(spread), mean_collectible_apr_pct=0.0,
    )
    clears, reasons = evaluate_cross_venue_candidate(ev, CROSS_VENUE_BAR)
    assert clears is False
    assert REASON_QUAL_PCT_BELOW in reasons


# --- portfolio aggregation + verdict ---------------------------------------------------


def test_portfolio_de_concentration_across_names() -> None:
    # Two names whose qualifying edge falls in DIFFERENT weeks -> pooled max-week share < 1.
    week = 7 * 24 * HOUR
    a = cross_venue_spread_stats(
        _hourly(24 * 100, rate=0.0001, start=0),
        cost_per_window_usd=0.05, per_name_notional_usd=250.0, hold_window_hours=168.0,
    )
    b = cross_venue_spread_stats(
        _hourly(24 * 100, rate=0.0001, start=50 * week),
        cost_per_window_usd=0.05, per_name_notional_usd=250.0, hold_window_hours=168.0,
    )
    port = aggregate_portfolio_concentration({"A": a, "B": b})
    assert port["names_with_positive_edge"] == 2
    assert port["max_single_week_share"] < 1.0


def test_screen_verdict_promising_when_basket_clears() -> None:
    port = {"max_single_week_share": 0.30, "names_with_positive_edge": 9}
    verdict, reasons = screen_verdict(
        clearing_names=["A", "B", "C", "D", "E"], portfolio=port, history_sufficient=True
    )
    assert verdict == VERDICT_PROMISING_BUILD
    assert reasons == []


def test_screen_verdict_no_go_too_few_names() -> None:
    port = {"max_single_week_share": 0.30, "names_with_positive_edge": 9}
    verdict, reasons = screen_verdict(
        clearing_names=["A", "B"], portfolio=port, history_sufficient=True
    )
    assert verdict == VERDICT_NO_GO
    assert REASON_PORTFOLIO_TOO_FEW_NAMES in reasons


def test_screen_verdict_inconclusive_without_history() -> None:
    verdict, reasons = screen_verdict(
        clearing_names=["A", "B", "C", "D", "E"], portfolio={}, history_sufficient=False
    )
    assert verdict == VERDICT_INCONCLUSIVE
    assert reasons == [REASON_SHORT_HISTORY]


def test_multi_hold_picks_shortest_passing_and_flags_dependency() -> None:
    clears_by_hold = {
        "168h": ["A", "B"],  # fails (too few)
        "504h": ["A", "B", "C", "D", "E"],  # clears at the longer hold
    }
    portfolio_by_hold = {
        "168h": {"max_single_week_share": 0.30, "names_with_positive_edge": 5},
        "504h": {"max_single_week_share": 0.30, "names_with_positive_edge": 9},
    }
    result = screen_verdict_multi_hold(
        clears_by_hold=clears_by_hold,
        portfolio_by_hold=portfolio_by_hold,
        hold_order=["168h", "504h"],
        history_sufficient=True,
    )
    assert result["verdict"] == VERDICT_PROMISING_BUILD
    assert result["minimum_passing_hold"] == "504h"
    assert result["hold_horizon_dependent"] is True


def test_multi_hold_no_go_when_no_hold_clears() -> None:
    clears_by_hold = {"168h": ["A"], "504h": ["A", "B"]}
    portfolio_by_hold = {
        "168h": {"max_single_week_share": 0.30, "names_with_positive_edge": 3},
        "504h": {"max_single_week_share": 0.30, "names_with_positive_edge": 4},
    }
    result = screen_verdict_multi_hold(
        clears_by_hold=clears_by_hold,
        portfolio_by_hold=portfolio_by_hold,
        hold_order=["168h", "504h"],
        history_sufficient=True,
    )
    assert result["verdict"] == VERDICT_NO_GO
    assert result["minimum_passing_hold"] is None


def test_multi_hold_not_dependent_when_shortest_clears() -> None:
    clears_by_hold = {
        "168h": ["A", "B", "C", "D", "E"],
        "504h": ["A", "B", "C", "D", "E", "F"],
    }
    portfolio_by_hold = {
        "168h": {"max_single_week_share": 0.30, "names_with_positive_edge": 9},
        "504h": {"max_single_week_share": 0.30, "names_with_positive_edge": 10},
    }
    result = screen_verdict_multi_hold(
        clears_by_hold=clears_by_hold,
        portfolio_by_hold=portfolio_by_hold,
        hold_order=["168h", "504h"],
        history_sufficient=True,
    )
    assert result["verdict"] == VERDICT_PROMISING_BUILD
    assert result["minimum_passing_hold"] == "168h"
    assert result["hold_horizon_dependent"] is False


def test_split_spread_at_disjoint() -> None:
    rows = _hourly(100, rate=0.0001)
    boundary = 50 * HOUR
    train, test = split_spread_at(rows, boundary)
    assert len(train) == 50
    assert len(test) == 50
    assert all(r.timestamp_ms < boundary for r in train)
    assert all(r.timestamp_ms >= boundary for r in test)


def test_walk_forward_survival_requires_in_both_windows() -> None:
    from ajentix_quant.research.neutral_screen import walk_forward_survival

    result = walk_forward_survival(
        train_clearing=["A", "B", "C", "D", "E", "F"],
        test_clearing=["A", "B"],  # only 2 of the 6 survive
        test_portfolio={"max_single_week_share": 0.30},
        min_surviving_names=5,
    )
    assert result["surviving_names"] == ["A", "B"]
    assert result["decayed_names"] == ["C", "D", "E", "F"]
    assert result["verdict"] == VERDICT_NO_GO  # < 5 survive => fails OOS
    assert result["survival_rate"] == 2 / 6


def test_walk_forward_survival_promising_when_basket_persists() -> None:
    from ajentix_quant.research.neutral_screen import walk_forward_survival

    names = ["A", "B", "C", "D", "E", "F"]
    result = walk_forward_survival(
        train_clearing=names,
        test_clearing=names,  # all survive
        test_portfolio={"max_single_week_share": 0.25},
        min_surviving_names=5,
    )
    assert result["verdict"] == VERDICT_PROMISING_BUILD
    assert result["survival_rate"] == 1.0


def test_walk_forward_survival_blocks_on_test_concentration() -> None:
    from ajentix_quant.research.neutral_screen import walk_forward_survival

    names = ["A", "B", "C", "D", "E", "F"]
    result = walk_forward_survival(
        train_clearing=names,
        test_clearing=names,
        test_portfolio={"max_single_week_share": 0.80},  # too concentrated in test
        min_surviving_names=5,
        max_test_week_share=0.40,
    )
    assert result["verdict"] == VERDICT_NO_GO

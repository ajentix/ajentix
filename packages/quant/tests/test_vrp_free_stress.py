from __future__ import annotations

import math
from datetime import UTC, datetime

import pytest

from ajentix_quant.adapters.base import SourceQuality
from ajentix_quant.backtest.option_costs import max_loss_from_width_credit_usd
from ajentix_quant.backtest.vrp_free_stress import (
    VrpFreeStressCoverageError,
    VrpFreeStressStatus,
    evaluate_exact_underlying_stress,
    select_exact_underlying_stress_windows,
)
from ajentix_quant.data.vrp_free_history_cache import IndexPathPoint
from ajentix_quant.options.types import (
    DefinedRiskStructure,
    OptionChainSnapshot,
    OptionLeg,
    OptionType,
    Side,
    StructureType,
)
from ajentix_quant.research.vrp_free_preregistration import (
    DEFAULT_SCENARIO_ID,
    PLAN_SOURCE_QUALITY_BRIDGE,
    PLAN_STRESS_RULE,
)

HOUR_MS = 60 * 60 * 1_000
DAY_MS = 24 * HOUR_MS
COVERAGE_START_MS = int(datetime(2024, 9, 1, tzinfo=UTC).timestamp() * 1000)
WARMUP_START_MS = COVERAGE_START_MS - 30 * DAY_MS
EXPIRY_MS = COVERAGE_START_MS + 30 * DAY_MS


def _returns_with_sum_squares(total: float, *, max_abs: float) -> list[float]:
    remaining = total - max_abs * max_abs
    if remaining <= 0.0:
        raise AssertionError("max_abs too large for requested realized variance")
    small = math.sqrt(remaining / 23.0)
    values = [max_abs, *([small] * 23)]
    return [value if index % 2 == 0 else -value for index, value in enumerate(values)]


def _index_path_from_returns(returns: list[float]) -> tuple[IndexPathPoint, ...]:
    price = 2_500.0
    points = [
        IndexPathPoint(
            timestamp_ms=WARMUP_START_MS,
            underlying="ETH",
            index_price=price,
        )
    ]
    for offset, value in enumerate(returns, start=1):
        price *= math.exp(value)
        points.append(
            IndexPathPoint(
                timestamp_ms=WARMUP_START_MS + offset * HOUR_MS,
                underlying="ETH",
                index_price=price,
            )
        )
    return tuple(points)


def _stress_index_path() -> tuple[IndexPathPoint, ...]:
    baseline = 0.001
    warmup = [baseline if index % 2 == 0 else -baseline for index in range(30 * 24)]
    warmup_sum = sum(value * value for value in warmup)

    day1 = [0.02 if index % 2 == 0 else -0.02 for index in range(24)]
    day1_sum = sum(value * value for value in day1)
    day1_score = math.sqrt(day1_sum) / math.sqrt(warmup_sum / 30.0)

    trailing_day2_sum = 29 * 24 * baseline * baseline + day1_sum
    day2_sum = day1_score * day1_score * trailing_day2_sum / 30.0
    day2 = _returns_with_sum_squares(day2_sum, max_abs=0.10)

    trailing_day3_sum = 28 * 24 * baseline * baseline + day1_sum + day2_sum
    day3_sum = (day1_score / 2.0) ** 2 * trailing_day3_sum / 30.0
    day3 = _returns_with_sum_squares(day3_sum, max_abs=0.20)

    return _index_path_from_returns([*warmup, *day1, *day2, *day3])


def _leg(name: str, strike: float, side: Side, bid: float, ask: float) -> OptionLeg:
    return OptionLeg(
        instrument_name=name,
        underlying="ETH",
        contract_multiplier=1.0,
        option_type=OptionType.PUT,
        side=side,
        strike=strike,
        expiry_ms=EXPIRY_MS,
        settlement_style="european",
        settlement_index="deribit_eth_index",
        premium_currency="ETH",
        fee_currency="ETH",
        collateral_currency="USDC_or_ETH",
        usd_conversion_source="deribit_eth_index",
        quote_ts_ms=COVERAGE_START_MS,
        quote_age_s=0.0,
        bid_price=bid,
        bid_amount=10.0,
        bid_iv=0.55,
        ask_price=ask,
        ask_amount=10.0,
        ask_iv=0.56,
        mark_price=(bid + ask) / 2.0,
        greek_provenance_key="fixture-diagnostic",
        min_tick=0.05,
        min_lot=1.0,
        source_quality=SourceQuality.FIXTURE,
    )


def _snapshot() -> OptionChainSnapshot:
    legs = (
        _leg("ETH-30D-3000-P", 3000.0, Side.SHORT, 35.0, 36.0),
        _leg("ETH-30D-2900-P", 2900.0, Side.LONG, 9.5, 10.0),
    )
    return OptionChainSnapshot(
        underlying="ETH",
        exchange="deribit",
        snapshot_ts_ms=COVERAGE_START_MS,
        source_ts_ms=COVERAGE_START_MS,
        source_id="fixture-reconstructed",
        scenario_id=DEFAULT_SCENARIO_ID,
        settlement_index_price=2500.0,
        index_price=2500.0,
        usd_conversion_inputs={"ETH": 2500.0},
        legs=legs,
        source_quality_map={"option_chain": SourceQuality.FIXTURE},
        schema_version="aq-options-cache-v1",
        manifest_sha256="f" * 64,
    )


def _structure(snapshot: OptionChainSnapshot) -> DefinedRiskStructure:
    short = snapshot.leg_by_instrument_name("ETH-30D-3000-P")
    long = snapshot.leg_by_instrument_name("ETH-30D-2900-P")
    credit = short.bid_price - long.ask_price
    width = short.strike - long.strike
    return DefinedRiskStructure(
        structure_type=StructureType.PUT_CREDIT_SPREAD,
        legs=(short, long),
        quantity=1,
        entry_snapshot_id=f"{DEFAULT_SCENARIO_ID}:{snapshot.snapshot_ts_ms}:fixture",
        expiry_ms=EXPIRY_MS,
        dte_days=30,
        settlement_style="european",
        settlement_index="deribit_eth_index",
        premium_currency="ETH",
        fee_currency="ETH",
        collateral_currency="USDC_or_ETH",
        usd_conversion_source="deribit_eth_index",
        net_credit=credit,
        width=width,
        fees=0.0,
        max_loss_usd=max_loss_from_width_credit_usd(
            width=width,
            net_credit=credit,
            contract_multiplier=1.0,
            quantity=1,
        ),
        max_gain_usd=credit,
        entry_quote_ts_ms=snapshot.snapshot_ts_ms,
        max_quote_age_s=0.0,
        frozen_param_key="fixture|put|max-loss-stress",
    )


def test_window_selection_matches_plan_rule_non_overlap_tiebreak_and_bounds() -> None:
    windows = select_exact_underlying_stress_windows(_stress_index_path())

    assert len(windows) == PLAN_STRESS_RULE["k"]
    assert [window.start_ts_ms for window in windows] == [
        COVERAGE_START_MS + DAY_MS,
        COVERAGE_START_MS,
        COVERAGE_START_MS + 2 * DAY_MS,
    ]
    assert all(window.window_hours == PLAN_STRESS_RULE["window_hours"] for window in windows)
    assert all(window.point_count == 25 for window in windows)
    assert all(window.start_ts_ms >= COVERAGE_START_MS for window in windows)
    coverage_end_ms = int(datetime(2026, 6, 1, tzinfo=UTC).timestamp() * 1000)
    assert all(window.end_ts_ms <= coverage_end_ms for window in windows)
    assert windows[0].score == pytest.approx(windows[1].score, rel=1e-10)
    assert windows[0].max_abs_1h_return > windows[1].max_abs_1h_return
    assert windows[0].selected_rank == 1


def test_stress_evaluation_reuses_engine_and_checks_max_loss_every_event() -> None:
    snapshot = _snapshot()
    structure = _structure(snapshot)

    result = evaluate_exact_underlying_stress(
        structures=(structure,),
        index_path=_stress_index_path(),
        reconstructed_chains=(snapshot,),
        equity_usd=1_000.0,
        cost_mode="fixture",
        taker_fee_bps=0.0,
    )

    assert result.status is VrpFreeStressStatus.RAN
    assert result.ran is True
    assert result.max_loss_ok is True
    assert len(result.max_loss_evidence) == 3
    for item in result.max_loss_evidence:
        assert item.stress_event_count == 25
        assert item.event_count == 27
        assert item.invariant_ok is True
        assert item.worst_pnl_usd >= -item.max_loss_usd
        assert all(event.invariant_ok for event in item.events)
        assert all(event.pnl_usd >= -event.max_loss_usd for event in item.events)


def test_missing_stress_coverage_fails_closed_without_synthetic_pass() -> None:
    snapshot = _snapshot()
    structure = _structure(snapshot)
    short_path = tuple(
        IndexPathPoint(
            timestamp_ms=COVERAGE_START_MS + index * HOUR_MS,
            underlying="ETH",
            index_price=2_500.0 + index,
        )
        for index in range(5)
    )

    with pytest.raises(VrpFreeStressCoverageError):
        select_exact_underlying_stress_windows(short_path)

    result = evaluate_exact_underlying_stress(
        structures=(structure,),
        index_path=short_path,
        reconstructed_chains=(snapshot,),
        equity_usd=1_000.0,
        cost_mode="fixture",
        taker_fee_bps=0.0,
    )

    assert result.status is VrpFreeStressStatus.INCONCLUSIVE
    assert result.ran is False
    assert result.max_loss_ok is False
    assert result.selected_windows == ()
    assert result.max_loss_evidence == ()
    assert "missing_required_stress_coverage" in result.reason_codes


def test_reconstructed_lineage_stays_free_non_authorizing() -> None:
    snapshot = _snapshot()
    result = evaluate_exact_underlying_stress(
        structures=(_structure(snapshot),),
        index_path=_stress_index_path(),
        reconstructed_chains=(snapshot,),
        equity_usd=1_000.0,
        cost_mode="fixture",
        taker_fee_bps=0.0,
    )

    assert result.lineage["authorizing"] is False
    assert result.lineage["capital_go_allowed"] is False
    assert result.lineage["non_authorizing_reason"] == "reconstructed_from_real_trade_iv"
    assert (
        result.lineage["free_source_quality"]
        == PLAN_SOURCE_QUALITY_BRIDGE["free_source_quality"]
    )
    assert result.lineage["forbidden_option_leg_source_quality"] == "SourceQuality.VENUE"

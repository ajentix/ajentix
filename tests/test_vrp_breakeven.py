from __future__ import annotations

from dataclasses import replace

import pytest

from ajentix_quant.backtest.option_costs import max_loss_from_width_credit_usd
from ajentix_quant.backtest.vrp_breakeven import (
    VRP_BRANCH_WALK_FORWARD,
    VrpBreakevenSample,
    analyze_vrp_breakeven,
)
from ajentix_quant.options.types import (
    DefinedRiskStructure,
    OptionLeg,
    OptionType,
    Side,
    SourceQuality,
    StructureType,
)

TS0 = 1_700_000_000_000
EXPIRY = TS0 + 30 * 86_400_000


def _leg(name: str, strike: float, side: Side, *, quality: SourceQuality) -> OptionLeg:
    return OptionLeg(
        instrument_name=name,
        underlying="ETH",
        contract_multiplier=1.0,
        option_type=OptionType.PUT,
        side=side,
        strike=strike,
        expiry_ms=EXPIRY,
        settlement_style="european",
        settlement_index="deribit_eth_index",
        premium_currency="ETH",
        fee_currency="ETH",
        collateral_currency="USDC_or_ETH",
        usd_conversion_source="deribit_eth_index",
        quote_ts_ms=TS0,
        quote_age_s=1.0,
        bid_price=35.0 if side is Side.SHORT else 9.5,
        bid_amount=5.0,
        bid_iv=0.55,
        ask_price=36.0 if side is Side.SHORT else 10.0,
        ask_amount=5.0,
        ask_iv=0.56,
        mark_price=35.5 if side is Side.SHORT else 9.75,
        greek_provenance_key="vendor_cached_hashed_preferred_else_local",
        min_tick=0.05,
        min_lot=1.0,
        source_quality=quality,
    )


def _structure(
    *,
    quality: SourceQuality = SourceQuality.VENUE,
    key: str = "grid|put",
) -> DefinedRiskStructure:
    short = _leg("ETH-30D-3000-P", 3000.0, Side.SHORT, quality=quality)
    long = _leg("ETH-30D-2900-P", 2900.0, Side.LONG, quality=quality)
    credit = short.bid_price - long.ask_price
    width = short.strike - long.strike
    return DefinedRiskStructure(
        structure_type=StructureType.PUT_CREDIT_SPREAD,
        legs=(short, long),
        quantity=1,
        entry_snapshot_id="fixture-entry",
        expiry_ms=EXPIRY,
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
        entry_quote_ts_ms=TS0,
        max_quote_age_s=1.0,
        frozen_param_key=key,
    )


def _sample(ts: int, *, structure: DefinedRiskStructure | None = None) -> VrpBreakevenSample:
    return VrpBreakevenSample(
        timestamp_ms=ts,
        structure=structure or _structure(),
        cluster_key=f"cluster-{ts}",
        taker_fee_bps=0.0,
        safety_margin_bps=0.0,
    )


def test_train_only_branch_and_test_row_mutation_invariance() -> None:
    train_end = TS0 + 3_000
    baseline_samples = [
        _sample(TS0 + 1_000),
        _sample(TS0 + 2_000),
        _sample(TS0 + 4_000, structure=_structure(key="heldout-mutated")),
    ]
    baseline = analyze_vrp_breakeven(
        baseline_samples,
        train_start_ms=TS0,
        train_end_ms=train_end,
        min_valid_windows=2,
        min_qualifying_windows=2,
        min_qualifying_pct=1.0,
        max_single_cluster_share=0.60,
        max_single_expiry_share=1.0,
    )

    heldout = baseline_samples[-1]
    mutated_heldout = replace(
        heldout,
        structure=_structure(quality=SourceQuality.FIXTURE, key="heldout-fixture"),
        cost_mode="maker",
    )
    changed = analyze_vrp_breakeven(
        [*baseline_samples[:2], mutated_heldout],
        train_start_ms=TS0,
        train_end_ms=train_end,
        min_valid_windows=2,
        min_qualifying_windows=2,
        min_qualifying_pct=1.0,
        max_single_cluster_share=0.60,
        max_single_expiry_share=1.0,
    )

    assert baseline.branch_decision == VRP_BRANCH_WALK_FORWARD
    assert changed.branch_decision == baseline.branch_decision
    assert changed.selected_param_keys == baseline.selected_param_keys
    assert changed.param_freeze_hash == baseline.param_freeze_hash
    assert changed.train_samples == 2


def test_breakeven_uses_option_costs_for_each_train_window(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import ajentix_quant.backtest.vrp_breakeven as module

    calls: list[str] = []
    real = module.evaluate_structure_costs

    def wrapped(structure, **kwargs):
        calls.append(structure.structure_id)
        return real(structure, **kwargs)

    monkeypatch.setattr(module, "evaluate_structure_costs", wrapped)

    result = analyze_vrp_breakeven(
        [_sample(TS0 + 1_000), _sample(TS0 + 2_000), _sample(TS0 + 9_000)],
        train_start_ms=TS0,
        train_end_ms=TS0 + 3_000,
        min_valid_windows=2,
        min_qualifying_windows=2,
        min_qualifying_pct=1.0,
        max_single_cluster_share=0.60,
        max_single_expiry_share=1.0,
    )

    assert len(calls) == 2
    assert result.windows[0].cost_breakdown.assumptions_hash
    assert all(
        window.max_loss_usd == window.cost_breakdown.max_loss_usd
        for window in result.windows
    )


def test_cost_path_fee_mutation_changes_breakeven_edge() -> None:
    low_fee = analyze_vrp_breakeven(
        [_sample(TS0 + 1_000)],
        train_start_ms=TS0,
        train_end_ms=TS0 + 2_000,
        min_qualifying_pct=1.0,
        max_single_cluster_share=1.0,
    )
    high_fee_sample = replace(_sample(TS0 + 1_000), taker_fee_bps=500.0)
    high_fee = analyze_vrp_breakeven(
        [high_fee_sample],
        train_start_ms=TS0,
        train_end_ms=TS0 + 2_000,
        min_qualifying_pct=1.0,
        max_single_cluster_share=1.0,
    )

    assert high_fee.windows[0].total_cost_usd > low_fee.windows[0].total_cost_usd
    assert high_fee.windows[0].edge_usd < low_fee.windows[0].edge_usd
    assert high_fee.param_freeze_hash != low_fee.param_freeze_hash


@pytest.mark.parametrize(
    ("structure", "cost_mode", "reason"),
    [
        (_structure(), "maker", "NON_AUTHORIZING_MAKER"),
        (_structure(quality=SourceQuality.FIXTURE), "taker", "NON_AUTHORIZING_FIXTURE"),
        (_structure(quality=SourceQuality.PROXY), "taker", "NON_AUTHORIZING_PROXY"),
    ],
)
def test_maker_proxy_fixture_cannot_branch_go(
    structure: DefinedRiskStructure,
    cost_mode: str,
    reason: str,
) -> None:
    sample = replace(_sample(TS0 + 1_000, structure=structure), cost_mode=cost_mode)

    result = analyze_vrp_breakeven(
        [sample],
        train_start_ms=TS0,
        train_end_ms=TS0 + 2_000,
        min_valid_windows=1,
        min_qualifying_windows=1,
        min_qualifying_pct=1.0,
        max_single_cluster_share=1.0,
    )

    assert result.branch_decision != VRP_BRANCH_WALK_FORWARD
    assert reason in result.structure_decisions[0].reason_codes

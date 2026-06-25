from __future__ import annotations

from dataclasses import replace

from ajentix_quant.backtest.option_costs import max_loss_from_width_credit_usd
from ajentix_quant.backtest.vrp_breakeven import VRP_BRANCH_INCONCLUSIVE
from ajentix_quant.backtest.vrp_free_walk_forward import (
    VrpFreeOutcome,
    run_free_breakeven,
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
    *, quality: SourceQuality = SourceQuality.FIXTURE, key: str = "grid|put"
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


def _sample(ts: int, *, structure: DefinedRiskStructure | None = None):
    from ajentix_quant.backtest.vrp_free_walk_forward import VrpBreakevenSample

    return VrpBreakevenSample(
        timestamp_ms=ts,
        structure=structure or _structure(),
        cluster_key=f"cluster-{ts}",
        taker_fee_bps=0.0,
        safety_margin_bps=0.0,
    )


def test_free_breakeven_is_train_only_and_heldout_mutation_invariant() -> None:
    train_end = TS0 + 3_000
    baseline_samples = [
        _sample(TS0 + 1_000),
        _sample(TS0 + 2_000),
        _sample(TS0 + 4_000, structure=_structure(key="heldout-mutated")),
    ]
    baseline = run_free_breakeven(
        baseline_samples,
        train_start_ms=TS0,
        train_end_ms=train_end,
        min_valid_windows=2,
        min_qualifying_windows=2,
        min_qualifying_pct=1.0,
        max_single_cluster_share=0.60,
        max_single_expiry_share=1.0,
    )

    mutated_heldout = replace(
        baseline_samples[-1],
        structure=_structure(quality=SourceQuality.FIXTURE, key="heldout-fixture"),
        cost_mode="maker",
    )
    changed = run_free_breakeven(
        [*baseline_samples[:2], mutated_heldout],
        train_start_ms=TS0,
        train_end_ms=train_end,
        min_valid_windows=2,
        min_qualifying_windows=2,
        min_qualifying_pct=1.0,
        max_single_cluster_share=0.60,
        max_single_expiry_share=1.0,
    )

    assert baseline.committed_result.branch_decision == VRP_BRANCH_INCONCLUSIVE
    assert changed.committed_result.branch_decision == baseline.committed_result.branch_decision
    assert (
        changed.committed_result.selected_param_keys
        == baseline.committed_result.selected_param_keys
    )
    assert changed.committed_result.param_freeze_hash == baseline.committed_result.param_freeze_hash
    assert changed.committed_result.train_samples == 2


def test_walk_forward_branch_is_input_only_and_capped_not_capital_go() -> None:
    report = run_free_breakeven(
        [_sample(TS0 + 1_000), _sample(TS0 + 2_000)],
        train_start_ms=TS0,
        train_end_ms=TS0 + 3_000,
        min_valid_windows=2,
        min_qualifying_windows=2,
        min_qualifying_pct=1.0,
        max_single_cluster_share=0.60,
        max_single_expiry_share=1.0,
    )

    assert report.committed_result.branch_decision == VRP_BRANCH_INCONCLUSIVE
    assert report.outcome is VrpFreeOutcome.INCONCLUSIVE
    assert report.authorizing is False
    assert report.capital_go_allowed is False
    assert report.free_lineage["authorizing"] is False
    assert report.free_lineage["capital_go_allowed"] is False
    assert report.free_lineage["non_authorizing_reason"] == "reconstructed_from_real_trade_iv"
    assert report.as_dict()["verdict"] != "GO"


def test_free_breakeven_calls_committed_analyze_vrp_breakeven(monkeypatch) -> None:
    import ajentix_quant.backtest.vrp_free_walk_forward as module

    calls: list[tuple[int, int]] = []
    real = module.committed_vrp_breakeven.analyze_vrp_breakeven

    def wrapped(samples, **kwargs):
        calls.append((kwargs["train_start_ms"], kwargs["train_end_ms"]))
        return real(samples, **kwargs)

    monkeypatch.setattr(module.committed_vrp_breakeven, "analyze_vrp_breakeven", wrapped)
    report = run_free_breakeven(
        [_sample(TS0 + 1_000)],
        train_start_ms=TS0,
        train_end_ms=TS0 + 2_000,
        min_valid_windows=1,
        min_qualifying_windows=1,
        min_qualifying_pct=1.0,
        max_single_cluster_share=1.0,
        max_single_expiry_share=1.0,
    )

    assert calls == [(TS0, TS0 + 2_000)]
    assert report.committed_result.windows[0].cost_breakdown.assumptions_hash

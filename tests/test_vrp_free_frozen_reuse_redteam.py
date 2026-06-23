from __future__ import annotations

import json
from dataclasses import replace
from datetime import UTC, datetime
from typing import Any

from ajentix_quant.backtest.option_costs import max_loss_from_width_credit_usd
from ajentix_quant.backtest.vrp_breakeven import VRP_BRANCH_WALK_FORWARD
from ajentix_quant.backtest.vrp_free_stress import (
    StressLedgerEventEvidence,
    StressStructureEvidence,
    StressWindow,
    VrpFreeStressResult,
    VrpFreeStressStatus,
)
from ajentix_quant.backtest.vrp_free_walk_forward import (
    FREE_OUTCOME_PROMISING,
    REASON_FREE_SOURCE_QUALITY_INVALID,
    VrpFoldEvaluation,
    VrpFreeOutcome,
    VrpVerdict,
    free_non_authorizing_lineage,
    map_committed_vrp_to_free_outcome,
    plan_grid_hash,
    run_free_breakeven,
    run_free_walk_forward,
)
from ajentix_quant.data.tardis_free_spread_calibration import StructureSpreadSample
from ajentix_quant.options.types import (
    DefinedRiskStructure,
    OptionLeg,
    OptionType,
    Side,
    SourceQuality,
    StructureType,
)
from ajentix_quant.research.vrp_free_preregistration import (
    DEFAULT_SCENARIO_ID,
    PLAN_FOLDS,
    PLAN_STRESS_RULE,
    validate_free_lineage_payload,
)

TS0 = 1_700_000_000_000
EXPIRY = TS0 + 30 * 86_400_000
HONEST_LABELS = ("reconstructed_from_real_trade_iv", "calibrated_spread_sample", "fixture")
BIN = {
    "option_type": "put",
    "dte_bucket": "dte_30",
    "moneyness_bucket": "near",
    "regime_label": "normal",
}


def _parse_iso_ms(value: str) -> int:
    normalized = value[:-1] + "+00:00" if value.endswith("Z") else value
    return int(datetime.fromisoformat(normalized).astimezone(UTC).timestamp() * 1000)


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
        bid_amount=10.0,
        bid_iv=0.55,
        ask_price=36.0 if side is Side.SHORT else 10.0,
        ask_amount=10.0,
        ask_iv=0.56,
        mark_price=35.5 if side is Side.SHORT else 9.75,
        greek_provenance_key="vendor_cached_hashed_preferred_else_local",
        min_tick=0.05,
        min_lot=1.0,
        source_quality=quality,
    )


def _structure(*, quality: SourceQuality = SourceQuality.FIXTURE) -> DefinedRiskStructure:
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
        frozen_param_key="grid|put",
    )


def _sample(ts: int, *, structure: DefinedRiskStructure):
    from ajentix_quant.backtest.vrp_free_walk_forward import VrpBreakevenSample

    return VrpBreakevenSample(
        timestamp_ms=ts,
        structure=structure,
        cluster_key=f"cluster-{ts}",
        taker_fee_bps=0.0,
        safety_margin_bps=0.0,
    )


def _train_branches(structure: DefinedRiskStructure) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for fold in PLAN_FOLDS:
        train_start = _parse_iso_ms(str(fold["train_start"]))
        train_end = _parse_iso_ms(str(fold["train_end"]))
        report = run_free_breakeven(
            [_sample(train_start + 1_000, structure=structure)],
            train_start_ms=train_start,
            train_end_ms=train_end,
            min_valid_windows=1,
            min_qualifying_windows=1,
            min_qualifying_pct=1.0,
            max_single_cluster_share=1.0,
            max_single_expiry_share=1.0,
        )
        out[str(fold["id"])] = replace(
            report.committed_result,
            decision="CLEARS",
            branch_decision=VRP_BRANCH_WALK_FORWARD,
            selected_param_keys=(structure.frozen_param_key,),
            reason_codes=("CLEARS_BREAKEVEN_BAR",),
        )
    return out


def _folds(
    *,
    source_quality: SourceQuality = SourceQuality.FIXTURE,
    labels: tuple[str, ...] = HONEST_LABELS,
) -> list[VrpFoldEvaluation]:
    rows: list[VrpFoldEvaluation] = []
    for i, fold in enumerate(PLAN_FOLDS, start=1):
        rows.append(
            VrpFoldEvaluation(
                fold_id=str(fold["id"]),
                selected_param_key="grid|put",
                param_freeze_hash=f"freeze-{fold['id']}",
                grid_hash=plan_grid_hash(),
                train_trial_count=1,
                heldout_eval_count=1,
                test_rerun_count=1,
                entries=2,
                pnl_usd=10.0 + i,
                returns=(0.018 + i * 0.001,),
                max_drawdown=0.04,
                stress_max_drawdown=0.10,
                source_quality={"option_chain": source_quality},
                cost_modes=("taker",),
                non_authorizing_labels=labels,
                cluster_pnl={f"cluster-{fold['id']}": 10.0 + i},
                max_loss_invariant_ok=True,
                stress_evaluated=True,
            )
        )
    return rows


def _calibration_samples() -> tuple[StructureSpreadSample, ...]:
    samples: list[StructureSpreadSample] = []
    for month_index, month in enumerate(
        ("2024-09-01", "2024-10-01", "2024-11-01", "2024-12-01", "2025-01-01", "2025-02-01")
    ):
        ts = _parse_iso_ms(f"{month}T00:00:00Z")
        for i in range(5):
            samples.append(
                StructureSpreadSample(
                    sample_id=f"sample-{month}-{i}",
                    sample_timestamp_ms=ts + month_index * 10 + i,
                    sample_month=month,
                    option_type="put",
                    dte_bucket="dte_30",
                    moneyness_bucket="near",
                    regime_label="normal",
                    round_trip_structure_spread_usd=1.0,
                    leg_instruments=("short", "long"),
                )
            )
    return tuple(samples)


def _stress_result_for(structure: DefinedRiskStructure) -> VrpFreeStressResult:
    windows = tuple(
        StressWindow(
            window_id=f"stress-window-{rank}",
            scenario_id=DEFAULT_SCENARIO_ID,
            selected_rank=rank,
            start_ts_ms=TS0 + rank * 100_000,
            end_ts_ms=TS0 + rank * 100_000 + 24 * 60 * 60 * 1_000,
            window_hours=24,
            point_count=25,
            start_price=3000.0,
            end_price=2950.0 + rank,
            realized_vol_24h=0.10 + rank * 0.01,
            trailing_30d_realized_vol=0.05,
            score=2.0 + rank,
            max_abs_1h_return=0.03,
        )
        for rank in range(1, int(PLAN_STRESS_RULE["k"]) + 1)
    )
    evidence: list[StressStructureEvidence] = []
    for window in windows:
        event = StressLedgerEventEvidence(
            window_id=window.window_id,
            structure_id=structure.structure_id,
            event_type="stress",
            timestamp_ms=window.end_ts_ms,
            reason="unit_test",
            pnl_usd=0.0,
            max_loss_usd=structure.max_loss_usd,
            invariant_ok=True,
            stress=True,
        )
        evidence.append(
            StressStructureEvidence(
                window_id=window.window_id,
                structure_id=structure.structure_id,
                entry_timestamp_ms=TS0,
                settlement_price=window.end_price,
                event_count=1,
                stress_event_count=1,
                worst_event_type=event.event_type,
                worst_event_reason=event.reason,
                worst_pnl_usd=event.pnl_usd,
                max_loss_usd=event.max_loss_usd,
                max_loss_margin_usd=event.max_loss_margin_usd,
                invariant_ok=True,
                events=(event,),
            )
        )
    return VrpFreeStressResult(
        scenario_id=DEFAULT_SCENARIO_ID,
        selected_windows=windows,
        max_loss_evidence=tuple(evidence),
        max_loss_ok=True,
        ran=True,
        status=VrpFreeStressStatus.RAN,
        lineage=free_non_authorizing_lineage(),
        reason_codes=(),
    )


def test_free_module_reuses_committed_modules_by_import_identity() -> None:
    import ajentix_quant.backtest.option_costs as option_costs
    import ajentix_quant.backtest.vrp_breakeven as breakeven
    import ajentix_quant.backtest.vrp_engine as engine
    import ajentix_quant.backtest.vrp_free_walk_forward as free
    import ajentix_quant.backtest.vrp_verdict as verdict
    import ajentix_quant.risk.options_margin as margin
    import ajentix_quant.strategies.vrp_defined_risk as strategy

    assert free.committed_vrp_breakeven is breakeven
    assert free.committed_vrp_engine is engine
    assert free.committed_vrp_verdict is verdict
    assert free.committed_option_costs is option_costs
    assert free.committed_options_margin is margin
    assert free.committed_vrp_defined_risk is strategy
    assert free.VrpDefinedRiskStrategy is strategy.VrpDefinedRiskStrategy
    assert free.run_vrp_backtest is engine.run_vrp_backtest
    assert free.plan_grid_hash is verdict.plan_grid_hash


def test_free_breakeven_call_through_to_committed_module(monkeypatch) -> None:
    import ajentix_quant.backtest.vrp_free_walk_forward as free

    calls: list[int] = []
    real = free.committed_vrp_breakeven.analyze_vrp_breakeven
    structure = _structure()

    def wrapped(samples, **kwargs):
        calls.append(len(samples))
        return real(samples, **kwargs)

    monkeypatch.setattr(free.committed_vrp_breakeven, "analyze_vrp_breakeven", wrapped)
    report = run_free_breakeven(
        [_sample(TS0 + 1_000, structure=structure)],
        train_start_ms=TS0,
        train_end_ms=TS0 + 2_000,
        min_valid_windows=1,
        min_qualifying_windows=1,
        min_qualifying_pct=1.0,
        max_single_cluster_share=1.0,
        max_single_expiry_share=1.0,
    )

    assert calls == [1]
    assert report.committed_result.windows


def test_venue_masquerade_invalid_lineage_cannot_flip_to_authorizing_go() -> None:
    lineage_override = {"source_quality": SourceQuality.VENUE.value}
    invalid_payload = {
        "free_source_quality": "reconstructed_from_real_trade_iv",
        "spread_source_quality": "calibrated_spread_sample",
        "non_authorizing_reason": "reconstructed_from_real_trade_iv",
        "authorizing": False,
        "capital_go_allowed": False,
        "outcome": FREE_OUTCOME_PROMISING,
        **lineage_override,
    }

    assert validate_free_lineage_payload(invalid_payload).valid is False
    outcome = map_committed_vrp_to_free_outcome(
        VrpVerdict.GO,
        economics_pass=True,
        lineage_payload=lineage_override,
    )
    assert outcome is VrpFreeOutcome.INCONCLUSIVE
    assert outcome.value != "GO"


def test_venue_source_quality_fold_facts_are_rejected_and_do_not_leak_go_tokens() -> None:
    structure = _structure()
    report = run_free_walk_forward(
        train_clearing_branches=_train_branches(structure),
        fold_evaluations=_folds(source_quality=SourceQuality.VENUE, labels=()),
        fold_structures={str(fold["id"]): (structure,) for fold in PLAN_FOLDS},
        calibration_samples=_calibration_samples(),
        fold_bin_overrides={"default": BIN},
        stress_result=_stress_result_for(structure),
    )
    payload = report.as_dict()

    assert report.committed_report.clean_heldout_go is True
    assert report.outcome is VrpFreeOutcome.INCONCLUSIVE
    assert REASON_FREE_SOURCE_QUALITY_INVALID in report.reason_codes
    assert report.authorizing is False
    assert report.capital_go_allowed is False
    assert payload["verdict"] != "GO"
    assert all("GO" not in reason for reason in payload["reason_codes"])
    assert all("GO" not in reason for reason in payload["committed_reason_codes"])
    assert "CLEAN_HELDOUT_GO" not in json.dumps(payload, sort_keys=True)
    assert '"GO"' not in json.dumps(payload, sort_keys=True)


def test_non_authorizing_reconstructed_label_cannot_improve_free_outcome_to_capital() -> None:
    structure = _structure()
    report = run_free_walk_forward(
        train_clearing_branches=_train_branches(structure),
        fold_evaluations=_folds(),
        fold_structures={str(fold["id"]): (structure,) for fold in PLAN_FOLDS},
        calibration_samples=_calibration_samples(),
        fold_bin_overrides={"default": BIN},
        stress_result=_stress_result_for(structure),
    )

    assert report.outcome is VrpFreeOutcome.PROMISING_PENDING_REAL_SPREAD
    assert report.authorizing is False
    assert report.capital_go_allowed is False
    assert report.as_dict()["verdict"] != "GO"
    assert report.committed_report.non_authorizing_dependence is True

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
    REASON_NON_TRAIN_CLEARING_SELECTION,
    VrpFoldEvaluation,
    VrpFreeOutcome,
    VrpVerdict,
    free_non_authorizing_lineage,
    map_committed_vrp_to_free_outcome,
    plan_grid_hash,
    run_free_breakeven,
    run_free_walk_forward,
)
from ajentix_quant.backtest.vrp_verdict import (
    REASON_FOLD_DELETION,
    REASON_GRID_MUTATION,
    REASON_MAX_LOSS_INVARIANT,
    REASON_NON_AUTHORIZING_DEPENDENCE,
    REASON_SOURCE_QUALITY_BLOCK,
    REASON_TEST_RERUN,
    REASON_TRIAL_BUDGET_BREACH,
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
_MISSING = object()


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
    branches: dict[str, Any] = {}
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
        branches[str(fold["id"])] = replace(
            report.committed_result,
            decision="CLEARS",
            branch_decision=VRP_BRANCH_WALK_FORWARD,
            selected_param_keys=(structure.frozen_param_key,),
            reason_codes=("CLEARS_BREAKEVEN_BAR",),
        )
    return branches


def _folds(**overrides: dict[str, Any]) -> list[VrpFoldEvaluation]:
    rows: list[VrpFoldEvaluation] = []
    for i, fold in enumerate(PLAN_FOLDS, start=1):
        data: dict[str, Any] = {
            "fold_id": fold["id"],
            "selected_param_key": "grid|put",
            "param_freeze_hash": f"freeze-{fold['id']}",
            "grid_hash": plan_grid_hash(),
            "train_trial_count": 1,
            "heldout_eval_count": 1,
            "test_rerun_count": 1,
            "entries": 2,
            "pnl_usd": 10.0 + i,
            "returns": (0.018 + i * 0.001,),
            "max_drawdown": 0.04,
            "stress_max_drawdown": 0.10,
            "source_quality": {"option_chain": SourceQuality.FIXTURE},
            "cost_modes": ("taker",),
            "non_authorizing_labels": HONEST_LABELS,
            "cluster_pnl": {f"cluster-{fold['id']}": 10.0 + i},
            "max_loss_invariant_ok": True,
            "stress_evaluated": True,
        }
        data.update(overrides.get(fold["id"], {}))
        data.update(overrides.get("all", {}))
        rows.append(VrpFoldEvaluation(**data))
    return rows


def _calibration_samples(spread: float = 1.0) -> tuple[StructureSpreadSample, ...]:
    month_starts = [
        "2024-09-01T00:00:00Z",
        "2024-10-01T00:00:00Z",
        "2024-11-01T00:00:00Z",
        "2024-12-01T00:00:00Z",
        "2025-01-01T00:00:00Z",
        "2025-02-01T00:00:00Z",
    ]
    samples: list[StructureSpreadSample] = []
    for iso in month_starts:
        ts = _parse_iso_ms(iso)
        month = iso[:10]
        for i in range(5):
            samples.append(
                StructureSpreadSample(
                    sample_id=f"sample-{month}-{i}",
                    sample_timestamp_ms=ts + i,
                    sample_month=month,
                    option_type="put",
                    dte_bucket="dte_30",
                    moneyness_bucket="near",
                    regime_label="normal",
                    round_trip_structure_spread_usd=spread,
                    leg_instruments=("short", "long"),
                )
            )
    return tuple(samples)


def _stress_result_for(
    structures: tuple[DefinedRiskStructure, ...], *, max_loss_ok: bool = True
) -> VrpFreeStressResult:
    unique_structures = {structure.structure_id: structure for structure in structures}
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
        for structure in unique_structures.values():
            event = StressLedgerEventEvidence(
                window_id=window.window_id,
                structure_id=structure.structure_id,
                event_type="stress",
                timestamp_ms=window.end_ts_ms,
                reason="unit_test",
                pnl_usd=0.0 if max_loss_ok else -structure.max_loss_usd - 1.0,
                max_loss_usd=structure.max_loss_usd,
                invariant_ok=max_loss_ok,
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
                    invariant_ok=max_loss_ok,
                    events=(event,),
                )
            )
    return VrpFreeStressResult(
        scenario_id=DEFAULT_SCENARIO_ID,
        selected_windows=windows,
        max_loss_evidence=tuple(evidence),
        max_loss_ok=max_loss_ok,
        ran=True,
        status=VrpFreeStressStatus.RAN,
        lineage=free_non_authorizing_lineage(),
        reason_codes=(),
    )


def _empty_stress_result() -> VrpFreeStressResult:
    return VrpFreeStressResult(
        scenario_id=DEFAULT_SCENARIO_ID,
        selected_windows=(),
        max_loss_evidence=(),
        max_loss_ok=True,
        ran=True,
        status=VrpFreeStressStatus.RAN,
        lineage=free_non_authorizing_lineage(),
        reason_codes=(),
    )


def _report(
    *,
    evaluations: list[VrpFoldEvaluation] | None = None,
    calibration_samples: tuple[StructureSpreadSample, ...] | None = None,
    stress_result: VrpFreeStressResult | object | None = _MISSING,
    branches: dict[str, Any] | None = None,
):
    structure = _structure()
    structures = {str(fold["id"]): (structure,) for fold in PLAN_FOLDS}
    stress = (
        _stress_result_for((structure,)) if stress_result is _MISSING else stress_result
    )
    return run_free_walk_forward(
        train_clearing_branches=branches if branches is not None else _train_branches(structure),
        fold_evaluations=evaluations or _folds(),
        fold_structures=structures,
        calibration_samples=calibration_samples or _calibration_samples(),
        fold_bin_overrides={"default": BIN},
        stress_result=stress if isinstance(stress, VrpFreeStressResult) else None,
    )


def test_honest_reconstructed_positive_is_promising_without_committed_go_leak() -> None:
    report = _report()
    payload = report.as_dict()

    assert report.outcome is VrpFreeOutcome.PROMISING_PENDING_REAL_SPREAD
    assert payload["verdict"] == FREE_OUTCOME_PROMISING
    assert report.authorizing is False
    assert report.capital_go_allowed is False
    assert report.committed_clean_heldout_positive is False
    assert REASON_SOURCE_QUALITY_BLOCK in report.committed_reason_codes
    assert REASON_NON_AUTHORIZING_DEPENDENCE in report.committed_reason_codes
    assert "CLEAN_HELDOUT_GO" not in report.committed_reason_codes
    assert all("GO" not in code for code in payload["reason_codes"])
    assert "GO" not in json.dumps(payload, sort_keys=True)
    assert all(row.heldout_eval_count == 1 for row in report.fold_economics)
    assert all(row.test_rerun_count == 1 for row in report.fold_economics)
    assert report.cost_budget_status == "PASS"
    assert report.stress_ran is True
    assert report.stress_max_loss_ok is True
    first = report.cost_budget_evidence[0]
    assert first.calibration_bin is not None
    assert first.calibration_bin.as_dict() == BIN
    assert first.p75_round_trip_structure_spread_usd == 1.0
    assert first.max_absorbable_round_trip_spread_usd == 10.0
    assert first.p75_safety_spread_usd == 1.25


def test_empty_or_forged_stress_forces_inconclusive() -> None:
    report = _report(stress_result=_empty_stress_result())

    assert report.outcome is VrpFreeOutcome.INCONCLUSIVE
    assert report.stress_ran is False
    assert report.stress_status == VrpFreeStressStatus.INCONCLUSIVE.value
    assert "STRESS_OMITTED" in report.reason_codes


def test_missing_train_clearing_branch_forces_inconclusive_reason() -> None:
    report = _report(branches={})

    assert report.outcome is VrpFreeOutcome.INCONCLUSIVE
    assert REASON_NON_TRAIN_CLEARING_SELECTION in report.reason_codes
    assert report.cost_budget_status == "INCONCLUSIVE"


def test_cost_budget_fail_is_no_go_despite_expected_non_authorizing_reasons() -> None:
    report = _report(calibration_samples=_calibration_samples(spread=20.0))

    assert report.cost_budget_status == "FAIL_BUDGET"
    assert report.outcome is VrpFreeOutcome.NO_GO
    assert "FREE_COST_BUDGET_FAIL" in report.reason_codes
    assert REASON_SOURCE_QUALITY_BLOCK in report.committed_reason_codes
    assert REASON_NON_AUTHORIZING_DEPENDENCE in report.committed_reason_codes


def test_committed_go_verdict_is_capped_by_mapper() -> None:
    outcome = map_committed_vrp_to_free_outcome(VrpVerdict.GO, economics_pass=True)

    assert outcome is VrpFreeOutcome.PROMISING_PENDING_REAL_SPREAD
    assert outcome.value == FREE_OUTCOME_PROMISING
    assert outcome.value != "GO"


def test_committed_go_containing_reason_codes_are_sanitized_in_report_payload() -> None:
    report = _report(evaluations=_folds(all={"returns": (-0.01,), "pnl_usd": -1.0}))
    payload = report.as_dict()

    assert report.outcome is VrpFreeOutcome.NO_GO
    assert any("GO" in reason for reason in report.committed_reason_codes)
    assert all("GO" not in reason for reason in report.reason_codes)
    assert all("GO" not in reason for reason in payload["reason_codes"])
    assert all("GO" not in reason for reason in payload["committed_reason_codes"])


def test_committed_governance_invalidations_are_inconclusive() -> None:
    cases = [
        (_folds()[:-1], REASON_FOLD_DELETION),
        ([*_folds(), replace(_folds()[0], fold_id="F8")], REASON_FOLD_DELETION),
        (_folds(F2={"grid_hash": "mutated"}), REASON_GRID_MUTATION),
        (_folds(all={"train_trial_count": 109}), REASON_TRIAL_BUDGET_BREACH),
        (_folds(F3={"heldout_eval_count": 2, "test_rerun_count": 2}), REASON_TEST_RERUN),
    ]

    for evaluations, expected_reason in cases:
        report = _report(evaluations=evaluations)
        assert expected_reason in report.committed_reason_codes
        assert expected_reason in report.reason_codes
        assert report.outcome is VrpFreeOutcome.INCONCLUSIVE


def test_max_loss_invariant_failure_is_economic_no_go() -> None:
    report = _report(evaluations=_folds(all={"max_loss_invariant_ok": False}))

    assert report.outcome is VrpFreeOutcome.NO_GO
    assert REASON_MAX_LOSS_INVARIANT in report.committed_reason_codes
    assert REASON_MAX_LOSS_INVARIANT in report.reason_codes

def test_multi_clearing_branch_requires_coverage_of_every_selected_key() -> None:
    # A TRAIN-clearing branch that selected multiple param keys must carry a fold
    # structure (and downstream cost/stress evidence) for EVERY selected key. Dropping
    # one selected key's structure must fail closed with NON_TRAIN_CLEARING_SELECTION,
    # never reach PROMISING with cost/stress evidence for only a subset.
    structure = _structure(key="grid|put")
    branches = {
        fold_id: replace(branch, selected_param_keys=("grid|put", "grid|put-uncovered"))
        for fold_id, branch in _train_branches(structure).items()
    }
    structures = {str(fold["id"]): (structure,) for fold in PLAN_FOLDS}

    report = run_free_walk_forward(
        train_clearing_branches=branches,
        fold_evaluations=_folds(),
        fold_structures=structures,
        calibration_samples=_calibration_samples(),
        fold_bin_overrides={"default": BIN},
        stress_result=_stress_result_for((structure,)),
    )

    assert report.outcome is VrpFreeOutcome.INCONCLUSIVE
    assert report.outcome is not VrpFreeOutcome.PROMISING_PENDING_REAL_SPREAD
    assert REASON_NON_TRAIN_CLEARING_SELECTION in report.reason_codes

"""Free-data-native VRP breakeven and walk-forward economics gates.

This module is a non-authorizing adapter around the committed VRP breakeven,
backtest, cost, stress, and walk-forward verdict primitives. It never creates a
capital ``GO`` outcome: reconstructed/free evidence can be at most
``PROMISING_PENDING_REAL_SPREAD`` and always carries non-authorizing lineage.
"""

from __future__ import annotations

import math
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any

from ajentix_quant.backtest import option_costs as committed_option_costs
from ajentix_quant.backtest import vrp_breakeven as committed_vrp_breakeven
from ajentix_quant.backtest import vrp_engine as committed_vrp_engine
from ajentix_quant.backtest import vrp_verdict as committed_vrp_verdict
from ajentix_quant.backtest.vrp_free_cost_budget import (
    VrpFreeCostBudgetResult,
    VrpFreeCostBudgetStatus,
    evaluate_vrp_free_cost_budget,
)
from ajentix_quant.backtest.vrp_free_stress import (
    VrpFreeStressResult,
    VrpFreeStressStatus,
    evaluate_exact_underlying_stress,
)
from ajentix_quant.data.tardis_free_spread_calibration import (
    STATUS_RESOLVED,
    SpreadQuantileResolution,
    StructureSpreadSample,
    TardisFreeSpreadCalibrationError,
    abs_log_moneyness_to_bucket,
    dte_days_to_bucket,
    resolve_spread_quantiles,
)
from ajentix_quant.data.tardis_free_spread_calibration import (
    regime_label as classify_regime_label,
)
from ajentix_quant.data.vrp_free_history_cache import IndexPathPoint
from ajentix_quant.options.iv_surface_reconstruction import ReconstructedOptionChain
from ajentix_quant.options.types import (
    DefinedRiskStructure,
    OptionChainSnapshot,
    Side,
    SourceQuality,
)
from ajentix_quant.research.vrp_free_preregistration import (
    DEFAULT_SCENARIO_ID,
    PLAN_FOLDS,
    PLAN_OUTCOME_RULES,
    PLAN_PRIMARY_EQUITY,
    PLAN_SOURCE_QUALITY_BRIDGE,
    PLAN_STRESS_RULE,
    PLAN_TRIAL_BUDGET,
    load_preregistration,
    max_free_verdict_for_valid_reconstructed_evidence,
    validate_free_lineage_payload,
    verify_preregistration,
)
from ajentix_quant.risk import options_margin as committed_options_margin
from ajentix_quant.strategies import vrp_defined_risk as committed_vrp_defined_risk

FREE_WALK_FORWARD_SCHEMA_VERSION = "aq-vrp-free-walk-forward-economics-v1"
FREE_BREAKEVEN_SCHEMA_VERSION = "aq-vrp-free-breakeven-economics-v1"

FREE_OUTCOME_NO_GO = "NO_GO"
FREE_OUTCOME_PROMISING = "PROMISING_PENDING_REAL_SPREAD"
FREE_OUTCOME_INCONCLUSIVE = "INCONCLUSIVE"
_ALLOWED_FREE_OUTCOMES = frozenset(str(value) for value in PLAN_OUTCOME_RULES["allowed_outcomes"])

REASON_FREE_LINEAGE_INVALID = "FREE_LINEAGE_INVALID"
REASON_BRANCH_INPUT_ONLY = "BRANCH_DECISION_INPUT_ONLY_NON_AUTHORIZING"
REASON_COST_BUDGET_FAIL = "FREE_COST_BUDGET_FAIL"
REASON_COST_BUDGET_INCONCLUSIVE = "FREE_COST_BUDGET_INCONCLUSIVE"
REASON_COST_BUDGET_MISSING = "FREE_COST_BUDGET_MISSING"
REASON_STRESS_MISSING = committed_vrp_verdict.REASON_STRESS_OMITTED
REASON_STRESS_MAX_LOSS = committed_vrp_verdict.REASON_MAX_LOSS_INVARIANT
REASON_NON_TRAIN_CLEARING_SELECTION = "NON_TRAIN_CLEARING_SELECTION"
REASON_FREE_SOURCE_QUALITY_INVALID = "FREE_SOURCE_QUALITY_INVALID"

_FREE_RECONSTRUCTED_NON_AUTHORIZING_LABELS = frozenset(
    {
        "reconstructed_from_real_trade_iv",
        "calibrated_spread_sample",
    }
)
_EXPECTED_NON_AUTHORIZING_REASONS = frozenset(
    {
        committed_vrp_verdict.REASON_SOURCE_QUALITY_BLOCK,
        committed_vrp_verdict.REASON_NON_AUTHORIZING_DEPENDENCE,
    }
)
_RUN_INVALID_REASONS = frozenset(
    {
        committed_vrp_verdict.REASON_FOLD_DELETION,
        committed_vrp_verdict.REASON_TEST_RERUN,
        committed_vrp_verdict.REASON_GRID_MUTATION,
        committed_vrp_verdict.REASON_TRIAL_BUDGET_BREACH,
    }
)
_ECONOMIC_NO_GO_REASONS = frozenset(
    {
        committed_vrp_verdict.REASON_SHARPE,
        committed_vrp_verdict.REASON_MDD,
        committed_vrp_verdict.REASON_FOLD_COLLAPSE,
        committed_vrp_verdict.REASON_CONCENTRATION,
        committed_vrp_verdict.REASON_ENTRIES,
        committed_vrp_verdict.REASON_MAX_LOSS_INVARIANT,
    }
)
_COVERAGE_INCONCLUSIVE_REASONS = frozenset({committed_vrp_verdict.REASON_STRESS_OMITTED})
_KNOWN_COMMITTED_REASONS = (
    _EXPECTED_NON_AUTHORIZING_REASONS
    | _RUN_INVALID_REASONS
    | _ECONOMIC_NO_GO_REASONS
    | _COVERAGE_INCONCLUSIVE_REASONS
)
_REASON_CODE_REMAP = {
    committed_vrp_verdict.REASON_CLEAN_HELDOUT_GO: "COMMITTED_CLEAN_HELDOUT_SIGNAL",
    committed_vrp_verdict.REASON_SHARPE: "COMMITTED_SHARPE_BELOW_PROMISING_BAR",
    committed_vrp_verdict.REASON_MDD: "COMMITTED_MDD_INCLUDING_STRESS_ABOVE_PROMISING_BAR",
}
_MS_PER_DAY = 86_400_000
_EPSILON = 1e-12


class VrpFreeOutcome(StrEnum):
    """Allowed free-data-native VRP economics outcomes."""

    NO_GO = FREE_OUTCOME_NO_GO
    PROMISING_PENDING_REAL_SPREAD = FREE_OUTCOME_PROMISING
    INCONCLUSIVE = FREE_OUTCOME_INCONCLUSIVE


@dataclass(frozen=True, kw_only=True)
class VrpFreeBreakevenReport:
    """Non-authorizing free wrapper around committed TRAIN-only breakeven output."""

    schema_version: str
    outcome: VrpFreeOutcome
    committed_result: committed_vrp_breakeven.VrpBreakevenResult
    free_lineage: Mapping[str, Any]
    lineage_valid: bool
    lineage_mismatches: tuple[str, ...]
    reason_codes: tuple[str, ...]
    authorizing: bool = False
    capital_go_allowed: bool = False
    train_only: bool = True
    branch_decision_input_only: bool = True

    @property
    def verdict(self) -> VrpFreeOutcome:
        return self.outcome

    def as_dict(self, *, include_windows: bool = False) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "outcome": self.outcome.value,
            "verdict": self.outcome.value,
            **_lineage_top_level(self.free_lineage),
            "authorizing": self.authorizing,
            "capital_go_allowed": self.capital_go_allowed,
            "train_only": self.train_only,
            "branch_decision_input_only": self.branch_decision_input_only,
            "free_lineage": dict(self.free_lineage),
            "lineage_valid": self.lineage_valid,
            "lineage_mismatches": list(self.lineage_mismatches),
            "reason_codes": list(_surface_reason_codes(self.reason_codes)),
            "breakeven": self.committed_result.as_dict(include_windows=include_windows),
        }


@dataclass(frozen=True, kw_only=True)
class VrpFreeCalibrationBin:
    """Frozen cost-budget bin used for one structure/fold lookup."""

    option_type: str
    dte_bucket: str
    moneyness_bucket: str
    regime_label: str

    def as_dict(self) -> dict[str, str]:
        return {
            "option_type": self.option_type,
            "dte_bucket": self.dte_bucket,
            "moneyness_bucket": self.moneyness_bucket,
            "regime_label": self.regime_label,
        }


@dataclass(frozen=True, kw_only=True)
class VrpFreeCostBudgetEvidence:
    """One train-causal calibrated-spread gate record for a structure in a fold."""

    fold_id: str
    structure_id: str
    frozen_param_key: str
    train_end_ms: int
    calibration_bin: VrpFreeCalibrationBin | None
    resolution_status: str
    resolution_level: str
    resolution_reason: str
    resolution_sample_cutoff_ms: int | None
    p50_round_trip_structure_spread_usd: float | None
    p75_round_trip_structure_spread_usd: float | None
    sample_count: int
    distinct_month_count: int
    cost_budget_status: str
    budget_pass: bool
    gross_credit_usd: float | None
    width_usd: float | None
    gross_credit_to_width: float | None
    max_absorbable_round_trip_spread_usd: float | None
    p50_margin_spread_usd: float | None
    p75_safety_spread_usd: float | None
    net_credit_after_p50_margin_usd: float | None
    net_credit_after_p75_safety_usd: float | None
    fail_reasons: tuple[str, ...]
    authorizing: bool = False
    capital_go_allowed: bool = False
    non_authorizing_reason: str = "free_vrp_cost_budget_component_only"

    def as_dict(self) -> dict[str, Any]:
        return {
            "fold_id": self.fold_id,
            "structure_id": self.structure_id,
            "frozen_param_key": self.frozen_param_key,
            "train_end_ms": self.train_end_ms,
            "train_end_utc": _iso_ms(self.train_end_ms),
            "calibration_bin": None
            if self.calibration_bin is None
            else self.calibration_bin.as_dict(),
            "resolution_status": self.resolution_status,
            "resolution_level": self.resolution_level,
            "resolution_reason": self.resolution_reason,
            "resolution_sample_cutoff_ms": self.resolution_sample_cutoff_ms,
            "p50_round_trip_structure_spread_usd": self.p50_round_trip_structure_spread_usd,
            "p75_round_trip_structure_spread_usd": self.p75_round_trip_structure_spread_usd,
            "sample_count": self.sample_count,
            "distinct_month_count": self.distinct_month_count,
            "cost_budget_status": self.cost_budget_status,
            "budget_pass": self.budget_pass,
            "gross_credit_usd": self.gross_credit_usd,
            "width_usd": self.width_usd,
            "gross_credit_to_width": self.gross_credit_to_width,
            "max_absorbable_round_trip_spread_usd": (
                self.max_absorbable_round_trip_spread_usd
            ),
            "p50_margin_spread_usd": self.p50_margin_spread_usd,
            "p75_safety_spread_usd": self.p75_safety_spread_usd,
            "net_credit_after_p50_margin_usd": self.net_credit_after_p50_margin_usd,
            "net_credit_after_p75_safety_usd": self.net_credit_after_p75_safety_usd,
            "fail_reasons": list(self.fail_reasons),
            "authorizing": self.authorizing,
            "capital_go_allowed": self.capital_go_allowed,
            "non_authorizing_reason": self.non_authorizing_reason,
        }


@dataclass(frozen=True, kw_only=True)
class VrpFreeFoldEconomics:
    """Gross, fee-only, and calibrated-net economics sensitivity for one fold."""

    fold_id: str
    selected_param_key: str
    train_trial_count: int
    heldout_eval_count: int
    test_rerun_count: int
    entries: int
    pnl_usd: float
    gross_of_spread_pnl_usd: float
    fee_only_pnl_usd: float
    calibrated_net_p50_pnl_usd: float
    calibrated_net_p75_pnl_usd: float
    max_absorbable_round_trip_spread_usd: float | None
    cost_budget_statuses: tuple[str, ...]

    def as_dict(self) -> dict[str, Any]:
        return {
            "fold_id": self.fold_id,
            "selected_param_key": self.selected_param_key,
            "train_trial_count": self.train_trial_count,
            "heldout_eval_count": self.heldout_eval_count,
            "test_rerun_count": self.test_rerun_count,
            "entries": self.entries,
            "pnl_usd": self.pnl_usd,
            "gross_of_spread_pnl_usd": self.gross_of_spread_pnl_usd,
            "fee_only_pnl_usd": self.fee_only_pnl_usd,
            "calibrated_net_p50_pnl_usd": self.calibrated_net_p50_pnl_usd,
            "calibrated_net_p75_pnl_usd": self.calibrated_net_p75_pnl_usd,
            "max_absorbable_round_trip_spread_usd": (
                self.max_absorbable_round_trip_spread_usd
            ),
            "cost_budget_statuses": list(self.cost_budget_statuses),
        }


@dataclass(frozen=True, kw_only=True)
class VrpFreeWalkForwardReport:
    """Free-data VRP walk-forward economics report with non-authorizing outcome."""

    schema_version: str
    outcome: VrpFreeOutcome
    run_status: str
    committed_run_status: str
    committed_clean_heldout_positive: bool
    committed_reason_codes: tuple[str, ...]
    fold_ids: tuple[str, ...]
    committed_report: committed_vrp_verdict.VrpWalkForwardVerdictReport
    free_lineage: Mapping[str, Any]
    lineage_valid: bool
    lineage_mismatches: tuple[str, ...]
    cost_budget_status: str
    stress_status: str
    stress_max_loss_ok: bool
    stress_ran: bool
    stress_result: VrpFreeStressResult | None
    cost_budget_evidence: tuple[VrpFreeCostBudgetEvidence, ...]
    fold_economics: tuple[VrpFreeFoldEconomics, ...]
    reason_codes: tuple[str, ...]
    authorizing: bool = False
    capital_go_allowed: bool = False

    @property
    def verdict(self) -> VrpFreeOutcome:
        return self.outcome

    def as_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "run_status": self.run_status,
            "outcome": self.outcome.value,
            "verdict": self.outcome.value,
            **_lineage_top_level(self.free_lineage),
            "authorizing": self.authorizing,
            "capital_go_allowed": self.capital_go_allowed,
            "committed_run_status": self.committed_run_status,
            "committed_clean_heldout_positive": self.committed_clean_heldout_positive,
            "committed_reason_codes": list(_surface_reason_codes(self.committed_reason_codes)),
            "fold_ids": list(self.fold_ids),
            "free_lineage": dict(self.free_lineage),
            "lineage_valid": self.lineage_valid,
            "lineage_mismatches": list(self.lineage_mismatches),
            "cost_budget_status": self.cost_budget_status,
            "stress_status": self.stress_status,
            "stress_max_loss_ok": self.stress_max_loss_ok,
            "stress_ran": self.stress_ran,
            "stress": None if self.stress_result is None else self.stress_result.as_dict(),
            "cost_budget_evidence": [item.as_dict() for item in self.cost_budget_evidence],
            "fold_economics": [item.as_dict() for item in self.fold_economics],
            "reason_codes": list(_surface_reason_codes(self.reason_codes)),
            "committed_hard_gate_report": _committed_hard_gate_payload(self.committed_report),
        }


# Public aliases that make reuse auditable in tests and scripts.
VrpBreakevenSample = committed_vrp_breakeven.VrpBreakevenSample
VrpBreakevenResult = committed_vrp_breakeven.VrpBreakevenResult
VrpBacktestResult = committed_vrp_engine.VrpBacktestResult
VrpBacktestStep = committed_vrp_engine.VrpBacktestStep
VrpFoldEvaluation = committed_vrp_verdict.VrpFoldEvaluation
VrpVerdict = committed_vrp_verdict.VrpVerdict
VrpDefinedRiskStrategy = committed_vrp_defined_risk.VrpDefinedRiskStrategy
run_vrp_backtest = committed_vrp_engine.run_vrp_backtest
plan_grid_hash = committed_vrp_verdict.plan_grid_hash


def free_non_authorizing_lineage(
    *, outcome: str | VrpFreeOutcome | None = None, overrides: Mapping[str, Any] | None = None
) -> dict[str, Any]:
    """Return the exact non-authorizing free lineage payload for report validation."""

    payload: dict[str, Any] = {
        "free_source_quality": PLAN_SOURCE_QUALITY_BRIDGE["free_source_quality"],
        "spread_source_quality": PLAN_SOURCE_QUALITY_BRIDGE["spread_source_quality"],
        "legacy_source_quality": PLAN_SOURCE_QUALITY_BRIDGE["legacy_source_quality"],
        "legacy_option_leg_source_quality": PLAN_SOURCE_QUALITY_BRIDGE[
            "legacy_option_leg_source_quality"
        ],
        "forbidden_reconstructed_option_leg_source_quality": PLAN_SOURCE_QUALITY_BRIDGE[
            "forbidden_reconstructed_option_leg_source_quality"
        ],
        "authorizing": False,
        "capital_go_allowed": False,
        "non_authorizing_reason": PLAN_SOURCE_QUALITY_BRIDGE["non_authorizing_reason"],
        "uses_committed_authorizing_verdict": False,
    }
    if outcome is not None:
        payload["outcome"] = _coerce_free_outcome(outcome).value
    if overrides:
        payload.update(dict(overrides))
    return payload


def map_committed_vrp_to_free_outcome(
    committed: committed_vrp_verdict.VrpVerdict | str,
    *,
    economics_pass: bool | None = None,
    inconclusive: bool = False,
    lineage_payload: Mapping[str, Any] | None = None,
) -> VrpFreeOutcome:
    """Map committed VRP outputs to the free outcome set, capping positive evidence.

    A committed capital-capable ``VrpVerdict.GO`` or breakeven ``WALK_FORWARD`` branch is
    never propagated. When the free lineage is valid and economics pass, the strongest
    possible output is ``PROMISING_PENDING_REAL_SPREAD`` via the frozen Phase-0 helper.
    """

    committed_value = _committed_value(committed)
    if committed_value in {
        committed_vrp_verdict.VrpVerdict.INCONCLUSIVE.value,
        committed_vrp_breakeven.VRP_BRANCH_INCONCLUSIVE,
        committed_vrp_breakeven.VRP_BREAKEVEN_INCONCLUSIVE,
    }:
        inconclusive = True
    if committed_value in {
        committed_vrp_verdict.VrpVerdict.GO.value,
        committed_vrp_breakeven.VRP_BRANCH_WALK_FORWARD,
        committed_vrp_breakeven.VRP_BREAKEVEN_CLEARS,
    }:
        positive = True if economics_pass is None else bool(economics_pass)
    elif committed_value in {
        committed_vrp_verdict.VrpVerdict.NO_GO.value,
        committed_vrp_breakeven.VRP_BRANCH_NO_GO,
        committed_vrp_breakeven.VRP_BREAKEVEN_NO_GO,
    }:
        positive = False
    else:
        positive = bool(economics_pass)
        inconclusive = True if economics_pass is None else inconclusive

    candidate = max_free_verdict_for_valid_reconstructed_evidence(
        economics_pass=positive,
        inconclusive=inconclusive,
    )
    outcome = _coerce_free_outcome(candidate)
    lineage = free_non_authorizing_lineage(outcome=outcome, overrides=lineage_payload)
    lineage_check = validate_free_lineage_payload(lineage)
    if not lineage_check.valid:
        return VrpFreeOutcome.INCONCLUSIVE
    return outcome


def run_free_breakeven(
    samples: Sequence[committed_vrp_breakeven.VrpBreakevenSample],
    *,
    train_start_ms: int,
    train_end_ms: int,
    equity_usd: float = PLAN_PRIMARY_EQUITY,
    min_valid_windows: int = committed_vrp_breakeven.DEFAULT_MIN_VALID_WINDOWS,
    min_qualifying_windows: int = committed_vrp_breakeven.DEFAULT_MIN_QUALIFYING_WINDOWS,
    min_qualifying_pct: float = committed_vrp_breakeven.DEFAULT_MIN_QUALIFYING_PCT,
    max_single_cluster_share: float = committed_vrp_breakeven.DEFAULT_MAX_SINGLE_CLUSTER_SHARE,
    max_single_expiry_share: float = committed_vrp_breakeven.DEFAULT_MAX_SINGLE_EXPIRY_SHARE,
    max_quote_age_s: float = committed_vrp_breakeven.DEFAULT_MAX_QUOTE_AGE_S,
    lineage_overrides: Mapping[str, Any] | None = None,
) -> VrpFreeBreakevenReport:
    """Run committed TRAIN-only breakeven and wrap it as free non-authorizing evidence."""

    committed = committed_vrp_breakeven.analyze_vrp_breakeven(
        samples,
        train_start_ms=train_start_ms,
        train_end_ms=train_end_ms,
        equity_usd=equity_usd,
        min_valid_windows=min_valid_windows,
        min_qualifying_windows=min_qualifying_windows,
        min_qualifying_pct=min_qualifying_pct,
        max_single_cluster_share=max_single_cluster_share,
        max_single_expiry_share=max_single_expiry_share,
        max_quote_age_s=max_quote_age_s,
    )
    outcome = map_committed_vrp_to_free_outcome(
        committed.branch_decision,
        economics_pass=committed.branch_decision
        == committed_vrp_breakeven.VRP_BRANCH_WALK_FORWARD,
        inconclusive=committed.branch_decision
        == committed_vrp_breakeven.VRP_BRANCH_INCONCLUSIVE,
        lineage_payload=lineage_overrides,
    )
    lineage = free_non_authorizing_lineage(outcome=outcome, overrides=lineage_overrides)
    lineage_check = validate_free_lineage_payload(lineage)
    reasons = [REASON_BRANCH_INPUT_ONLY, *committed.reason_codes]
    if not lineage_check.valid:
        reasons.append(REASON_FREE_LINEAGE_INVALID)
        outcome = VrpFreeOutcome.INCONCLUSIVE
        lineage = free_non_authorizing_lineage(outcome=outcome, overrides=lineage_overrides)
        lineage_check = validate_free_lineage_payload(lineage)
    return VrpFreeBreakevenReport(
        schema_version=FREE_BREAKEVEN_SCHEMA_VERSION,
        outcome=outcome,
        committed_result=committed,
        free_lineage=lineage,
        lineage_valid=lineage_check.valid,
        lineage_mismatches=lineage_check.mismatches,
        reason_codes=tuple(dict.fromkeys(reasons)),
    )


def run_free_walk_forward(
    *,
    train_clearing_branches: Mapping[str, committed_vrp_breakeven.VrpBreakevenResult]
    | None = None,
    fold_evaluations: Sequence[committed_vrp_verdict.VrpFoldEvaluation] | None = None,
    test_backtests: Mapping[str, committed_vrp_engine.VrpBacktestResult] | None = None,
    fold_structures: Mapping[str, Sequence[DefinedRiskStructure]] | None = None,
    calibration_samples: Sequence[StructureSpreadSample] = (),
    reconstructed_chains: Sequence[ReconstructedOptionChain | OptionChainSnapshot] = (),
    index_path: Sequence[IndexPathPoint] = (),
    equity_usd: float = PLAN_PRIMARY_EQUITY,
    scenario_id: str = DEFAULT_SCENARIO_ID,
    fold_bin_overrides: Mapping[Any, Mapping[str, str] | VrpFreeCalibrationBin] | None = None,
    source_quality: Mapping[str, SourceQuality | str] | None = None,
    stress_result: VrpFreeStressResult | None = None,
    evaluate_stress: bool = True,
    expected_folds: Sequence[Mapping[str, str]] = PLAN_FOLDS,
    trial_budget: Mapping[str, Any] = PLAN_TRIAL_BUDGET,
    lineage_overrides: Mapping[str, Any] | None = None,
) -> VrpFreeWalkForwardReport:
    """Run the free-data walk-forward economics gates.

    The committed walk-forward verdict function is used as the hard gate for fold count,
    one held-out evaluation per frozen fold, grid hash, trial budget, hidden retries,
    stress omission, and max-loss invariant failures. Cost budget is then applied with
    fold-causal Tardis-free calibration samples, and stress is evaluated through the
    committed engine-backed free stress module when a precomputed result is not supplied.
    """

    fold_map = _fold_map(expected_folds)
    branches = dict(train_clearing_branches or {})
    structures_by_fold = {
        str(key): tuple(value) for key, value in (fold_structures or {}).items()
    }
    evaluations = tuple(
        fold_evaluations
        if fold_evaluations is not None
        else _build_fold_evaluations(
            branches=branches,
            test_backtests=test_backtests or {},
            fold_structures=structures_by_fold,
            source_quality=source_quality or {"option_chain": SourceQuality.FIXTURE},
            expected_folds=expected_folds,
            equity_usd=equity_usd,
            non_authorizing_labels=(
                *_FREE_RECONSTRUCTED_NON_AUTHORIZING_LABELS,
                "fixture",
            ),
        )
    )

    selected_structures, train_clearing_selection_valid = _selected_structures_by_fold(
        evaluations=evaluations,
        branches=branches,
        fold_structures=structures_by_fold,
        expected_folds=expected_folds,
    )
    all_selected_structures = tuple(
        structure for rows in selected_structures.values() for structure in rows
    )
    stress = _resolve_stress_result(
        stress_result=stress_result,
        evaluate_stress=evaluate_stress,
        structures=all_selected_structures,
        index_path=index_path,
        reconstructed_chains=reconstructed_chains,
        equity_usd=equity_usd,
        scenario_id=scenario_id,
    )
    stress_complete = _stress_structurally_complete(
        stress,
        structures=all_selected_structures,
        scenario_id=scenario_id,
    )
    stress_max_loss_ok = _stress_max_loss_ok(stress) if stress_complete else False
    stress_ran = bool(stress and stress.ran and stress_complete)
    stress_status = (
        stress.status.value
        if stress is not None and stress_complete
        else VrpFreeStressStatus.INCONCLUSIVE.value
    )

    decision_evaluations = _apply_stress_to_evaluations(
        _with_committed_non_authorizing_bridge(evaluations),
        stress_complete=stress_complete,
        stress_max_loss_ok=stress_max_loss_ok,
    )
    committed_report = committed_vrp_verdict.decide_vrp_walk_forward(
        decision_evaluations,
        expected_folds=expected_folds,
        trial_budget=trial_budget,
    )

    cost_evidence = _cost_budget_evidence_for_folds(
        selected_structures=selected_structures,
        fold_map=fold_map,
        calibration_samples=calibration_samples,
        index_path=index_path,
        fold_bin_overrides=fold_bin_overrides or {},
    )
    cost_budget_status = _aggregate_cost_budget_status(cost_evidence)
    fold_economics = _fold_economics(decision_evaluations, cost_evidence)

    committed_reasons = set(committed_report.reason_codes)
    run_invalid = (
        committed_report.run_status == committed_vrp_verdict.RUN_STATUS_INVALID
        or bool(committed_reasons & _RUN_INVALID_REASONS)
    )
    economic_no_go = bool(committed_reasons & _ECONOMIC_NO_GO_REASONS)
    coverage_inconclusive = bool(committed_reasons & _COVERAGE_INCONCLUSIVE_REASONS)
    unexpected_committed_reasons = committed_reasons - _KNOWN_COMMITTED_REASONS
    cost_fail = cost_budget_status == VrpFreeCostBudgetStatus.FAIL_BUDGET.value
    cost_inconclusive = cost_budget_status == VrpFreeCostBudgetStatus.INCONCLUSIVE.value
    stress_incomplete = not stress_complete
    fold_source_quality_valid = _fold_source_quality_valid(evaluations)

    if economic_no_go or cost_fail:
        outcome = VrpFreeOutcome.NO_GO
    elif (
        run_invalid
        or coverage_inconclusive
        or not train_clearing_selection_valid
        or cost_inconclusive
        or stress_incomplete
        or not fold_source_quality_valid
        or unexpected_committed_reasons
    ):
        outcome = VrpFreeOutcome.INCONCLUSIVE
    else:
        outcome = _coerce_free_outcome(
            max_free_verdict_for_valid_reconstructed_evidence(
                economics_pass=True,
                inconclusive=False,
            )
        )

    lineage = free_non_authorizing_lineage(outcome=outcome, overrides=lineage_overrides)
    lineage_check = validate_free_lineage_payload(lineage)
    reasons = list(committed_report.reason_codes)
    if cost_fail:
        reasons.append(REASON_COST_BUDGET_FAIL)
    if cost_inconclusive:
        reasons.append(REASON_COST_BUDGET_INCONCLUSIVE)
    if not cost_evidence:
        reasons.append(REASON_COST_BUDGET_MISSING)
    if not stress_complete:
        reasons.append(REASON_STRESS_MISSING)
    if not train_clearing_selection_valid:
        reasons.append(REASON_NON_TRAIN_CLEARING_SELECTION)
    if not fold_source_quality_valid:
        reasons.append(REASON_FREE_SOURCE_QUALITY_INVALID)
    if not lineage_check.valid:
        reasons.append(REASON_FREE_LINEAGE_INVALID)
        if not (economic_no_go or cost_fail):
            outcome = VrpFreeOutcome.INCONCLUSIVE
            lineage = free_non_authorizing_lineage(outcome=outcome, overrides=lineage_overrides)
            lineage_check = validate_free_lineage_payload(lineage)

    return VrpFreeWalkForwardReport(
        schema_version=FREE_WALK_FORWARD_SCHEMA_VERSION,
        outcome=outcome,
        run_status=(
            committed_vrp_verdict.RUN_STATUS_INVALID
            if (
                run_invalid
                or not train_clearing_selection_valid
                or not fold_source_quality_valid
                or not lineage_check.valid
            )
            else committed_vrp_verdict.RUN_STATUS_VALID
        ),
        committed_run_status=committed_report.run_status,
        committed_clean_heldout_positive=committed_report.clean_heldout_go,
        committed_reason_codes=committed_report.reason_codes,
        fold_ids=committed_report.fold_ids,
        committed_report=committed_report,
        free_lineage=lineage,
        lineage_valid=lineage_check.valid,
        lineage_mismatches=lineage_check.mismatches,
        cost_budget_status=cost_budget_status,
        stress_status=stress_status,
        stress_max_loss_ok=stress_max_loss_ok,
        stress_ran=stress_ran,
        stress_result=stress,
        cost_budget_evidence=cost_evidence,
        fold_economics=fold_economics,
        reason_codes=tuple(dict.fromkeys(_surface_reason_codes(reasons))),
    )


def _build_fold_evaluations(
    *,
    branches: Mapping[str, committed_vrp_breakeven.VrpBreakevenResult],
    test_backtests: Mapping[str, committed_vrp_engine.VrpBacktestResult],
    fold_structures: Mapping[str, Sequence[DefinedRiskStructure]],
    source_quality: Mapping[str, SourceQuality | str],
    expected_folds: Sequence[Mapping[str, str]],
    equity_usd: float,
    non_authorizing_labels: Sequence[str],
) -> tuple[committed_vrp_verdict.VrpFoldEvaluation, ...]:
    rows: list[committed_vrp_verdict.VrpFoldEvaluation] = []
    grid_hash = committed_vrp_verdict.plan_grid_hash()
    for fold in expected_folds:
        fold_id = str(fold["id"])
        branch = branches.get(fold_id)
        selected_keys = tuple(branch.selected_param_keys) if branch is not None else ()
        selected_key = selected_keys[0] if selected_keys else "-"
        selected_structures = tuple(
            structure
            for structure in fold_structures.get(fold_id, ())
            if structure.frozen_param_key in selected_keys
        )
        backtest = test_backtests.get(fold_id) or committed_vrp_engine.run_vrp_backtest(
            [], initial_equity_usd=equity_usd
        )
        rows.append(
            committed_vrp_verdict.VrpFoldEvaluation(
                fold_id=fold_id,
                selected_param_key=selected_key,
                param_freeze_hash=branch.param_freeze_hash if branch is not None else "MISSING",
                grid_hash=grid_hash,
                train_trial_count=branch.train_samples if branch is not None else 0,
                heldout_eval_count=1,
                test_rerun_count=1,
                entries=backtest.n_entries if selected_structures else 0,
                pnl_usd=backtest.realized_pnl_usd if selected_structures else 0.0,
                returns=(
                    (backtest.realized_pnl_usd / equity_usd,)
                    if selected_structures and backtest.n_entries > 0
                    else ()
                ),
                max_drawdown=backtest.max_drawdown,
                stress_max_drawdown=backtest.max_drawdown_including_stress,
                source_quality=source_quality,
                cost_modes=("taker",),
                non_authorizing_labels=tuple(non_authorizing_labels),
                cluster_pnl={fold_id: backtest.realized_pnl_usd if selected_structures else 0.0},
                max_loss_invariant_ok=backtest.max_loss_invariant_ok,
                stress_evaluated=True,
            )
        )
    return tuple(rows)


def _selected_structures_by_fold(
    *,
    evaluations: Sequence[committed_vrp_verdict.VrpFoldEvaluation],
    branches: Mapping[str, committed_vrp_breakeven.VrpBreakevenResult],
    fold_structures: Mapping[str, Sequence[DefinedRiskStructure]],
    expected_folds: Sequence[Mapping[str, str]],
) -> tuple[dict[str, tuple[DefinedRiskStructure, ...]], bool]:
    selected: dict[str, tuple[DefinedRiskStructure, ...]] = {}
    expected_ids = {str(fold["id"]) for fold in expected_folds}
    observed_ids = {evaluation.fold_id for evaluation in evaluations}
    valid = expected_ids <= observed_ids
    for evaluation in evaluations:
        structures = tuple(fold_structures.get(evaluation.fold_id, ()))
        branch = branches.get(evaluation.fold_id)
        branch_selected_keys = (
            set(branch.selected_param_keys)
            if branch is not None
            and branch.branch_decision == committed_vrp_breakeven.VRP_BRANCH_WALK_FORWARD
            else set()
        )
        evaluation_key_valid = evaluation.selected_param_key in branch_selected_keys
        if (
            evaluation.fold_id not in expected_ids
            or not branch_selected_keys
            or not evaluation_key_valid
        ):
            valid = False
            selected[evaluation.fold_id] = ()
            continue
        rows = tuple(
            structure
            for structure in structures
            if structure.frozen_param_key in branch_selected_keys
        )
        observed_keys = {structure.frozen_param_key for structure in rows}
        # Every TRAIN-clearing-selected key must carry a fold structure into the
        # downstream cost-budget and stress coverage. A multi-clearing branch cannot
        # reach PROMISING with cost/stress evidence for only a subset of its selected
        # keys, so require the observed structure key set to cover the full branch set.
        if observed_keys != branch_selected_keys:
            valid = False
            selected[evaluation.fold_id] = ()
            continue
        selected[evaluation.fold_id] = rows
    return selected, valid


def _resolve_stress_result(
    *,
    stress_result: VrpFreeStressResult | None,
    evaluate_stress: bool,
    structures: Sequence[DefinedRiskStructure],
    index_path: Sequence[IndexPathPoint],
    reconstructed_chains: Sequence[ReconstructedOptionChain | OptionChainSnapshot],
    equity_usd: float,
    scenario_id: str,
) -> VrpFreeStressResult | None:
    if evaluate_stress and structures and index_path and reconstructed_chains:
        computed = evaluate_exact_underlying_stress(
            structures=structures,
            index_path=index_path,
            reconstructed_chains=reconstructed_chains,
            equity_usd=equity_usd,
            scenario_id=scenario_id,
        )
        return (
            computed
            if _stress_structurally_complete(
                computed,
                structures=structures,
                scenario_id=scenario_id,
            )
            else None
        )
    if stress_result is None:
        return None
    return (
        stress_result
        if _stress_structurally_complete(
            stress_result,
            structures=structures,
            scenario_id=scenario_id,
        )
        else None
    )


def _apply_stress_to_evaluations(
    evaluations: Sequence[committed_vrp_verdict.VrpFoldEvaluation],
    *,
    stress_complete: bool,
    stress_max_loss_ok: bool,
) -> tuple[committed_vrp_verdict.VrpFoldEvaluation, ...]:
    return tuple(
        replace(
            row,
            stress_evaluated=row.stress_evaluated and stress_complete,
            max_loss_invariant_ok=row.max_loss_invariant_ok
            and (stress_max_loss_ok if stress_complete else True),
            stress_max_drawdown=(
                row.stress_max_drawdown
                if stress_complete
                else max(row.stress_max_drawdown, row.max_drawdown)
            ),
        )
        for row in evaluations
    )


def _with_committed_non_authorizing_bridge(
    evaluations: Sequence[committed_vrp_verdict.VrpFoldEvaluation],
) -> tuple[committed_vrp_verdict.VrpFoldEvaluation, ...]:
    bridged: list[committed_vrp_verdict.VrpFoldEvaluation] = []
    for row in evaluations:
        labels = tuple(row.non_authorizing_labels)
        lower_labels = {label.lower() for label in labels}
        if _FREE_RECONSTRUCTED_NON_AUTHORIZING_LABELS <= lower_labels:
            labels = tuple(dict.fromkeys((*labels, "fixture")))
        bridged.append(replace(row, non_authorizing_labels=labels))
    return tuple(bridged)


def _stress_structurally_complete(
    stress: VrpFreeStressResult | None,
    *,
    structures: Sequence[DefinedRiskStructure],
    scenario_id: str,
) -> bool:
    if stress is None or not stress.ran or stress.status is not VrpFreeStressStatus.RAN:
        return False
    if stress.scenario_id != scenario_id:
        return False
    if not validate_free_lineage_payload(dict(stress.lineage)).valid:
        return False
    windows = tuple(stress.selected_windows)
    if len(windows) != int(PLAN_STRESS_RULE["k"]):
        return False
    window_ids = {window.window_id for window in windows}
    if len(window_ids) != len(windows):
        return False
    if any(window.scenario_id != scenario_id for window in windows):
        return False
    structure_ids = {structure.structure_id for structure in structures}
    if not structure_ids:
        return False
    evidence = tuple(stress.max_loss_evidence)
    if not evidence:
        return False
    evidence_pairs = {(item.window_id, item.structure_id) for item in evidence}
    required_pairs = {
        (window_id, structure_id)
        for window_id in window_ids
        for structure_id in structure_ids
    }
    if not required_pairs <= evidence_pairs:
        return False
    return all(
        item.window_id in window_ids
        and item.structure_id in structure_ids
        and bool(item.events)
        for item in evidence
    )


def _stress_max_loss_ok(stress: VrpFreeStressResult | None) -> bool:
    if stress is None or not stress.max_loss_ok:
        return False
    return all(
        item.invariant_ok and all(event.invariant_ok for event in item.events)
        for item in stress.max_loss_evidence
    )


def _fold_source_quality_valid(
    evaluations: Sequence[committed_vrp_verdict.VrpFoldEvaluation],
) -> bool:
    for evaluation in evaluations:
        if not evaluation.source_quality:
            return False
        if any(
            _source_quality_value(value) != SourceQuality.FIXTURE.value
            for value in evaluation.source_quality.values()
        ):
            return False
        labels = {label.lower() for label in evaluation.non_authorizing_labels}
        if not _FREE_RECONSTRUCTED_NON_AUTHORIZING_LABELS <= labels:
            return False
    return True


def _cost_budget_evidence_for_folds(
    *,
    selected_structures: Mapping[str, Sequence[DefinedRiskStructure]],
    fold_map: Mapping[str, Mapping[str, str]],
    calibration_samples: Sequence[StructureSpreadSample],
    index_path: Sequence[IndexPathPoint],
    fold_bin_overrides: Mapping[Any, Mapping[str, str] | VrpFreeCalibrationBin],
) -> tuple[VrpFreeCostBudgetEvidence, ...]:
    evidence: list[VrpFreeCostBudgetEvidence] = []
    for fold_id in sorted(selected_structures):
        fold = fold_map.get(fold_id)
        if fold is None:
            continue
        train_end_ms = _parse_iso_ms(str(fold["train_end"]))
        for structure in selected_structures[fold_id]:
            evidence.append(
                _cost_budget_evidence(
                    fold_id=fold_id,
                    train_end_ms=train_end_ms,
                    structure=structure,
                    calibration_samples=calibration_samples,
                    index_path=index_path,
                    fold_bin_overrides=fold_bin_overrides,
                )
            )
    return tuple(evidence)


def _cost_budget_evidence(
    *,
    fold_id: str,
    train_end_ms: int,
    structure: DefinedRiskStructure,
    calibration_samples: Sequence[StructureSpreadSample],
    index_path: Sequence[IndexPathPoint],
    fold_bin_overrides: Mapping[Any, Mapping[str, str] | VrpFreeCalibrationBin],
) -> VrpFreeCostBudgetEvidence:
    try:
        bin_key = _calibration_bin_for_structure(
            fold_id=fold_id,
            structure=structure,
            index_path=index_path,
            fold_bin_overrides=fold_bin_overrides,
        )
        causal_samples = tuple(
            sample for sample in calibration_samples if sample.sample_timestamp_ms <= train_end_ms
        )
        resolution = resolve_spread_quantiles(
            causal_samples,
            option_type=bin_key.option_type,
            dte_bucket=bin_key.dte_bucket,
            moneyness_bucket=bin_key.moneyness_bucket,
            regime_label=bin_key.regime_label,
            train_end_ms=train_end_ms,
        )
        if (
            resolution.status != STATUS_RESOLVED
            or resolution.p50_round_trip_structure_spread_usd is None
            or resolution.p75_round_trip_structure_spread_usd is None
        ):
            return _inconclusive_cost_evidence(
                fold_id=fold_id,
                train_end_ms=train_end_ms,
                structure=structure,
                calibration_bin=bin_key,
                resolution=resolution,
                reason=resolution.reason,
            )
        gross_credit_usd, width_usd = _structure_credit_width_usd(structure)
        result = evaluate_vrp_free_cost_budget(
            gross_credit_usd=gross_credit_usd,
            width_usd=width_usd,
            p50_spread_usd=resolution.p50_round_trip_structure_spread_usd,
            p75_spread_usd=resolution.p75_round_trip_structure_spread_usd,
            sample_count=resolution.sample_count,
            distinct_months=resolution.distinct_month_count,
        )
        return _cost_evidence_from_result(
            fold_id=fold_id,
            train_end_ms=train_end_ms,
            structure=structure,
            calibration_bin=bin_key,
            resolution=resolution,
            result=result,
        )
    except (KeyError, ValueError, TypeError, TardisFreeSpreadCalibrationError) as exc:
        return _inconclusive_cost_evidence(
            fold_id=fold_id,
            train_end_ms=train_end_ms,
            structure=structure,
            calibration_bin=None,
            resolution=None,
            reason=exc.__class__.__name__,
        )


def _cost_evidence_from_result(
    *,
    fold_id: str,
    train_end_ms: int,
    structure: DefinedRiskStructure,
    calibration_bin: VrpFreeCalibrationBin,
    resolution: SpreadQuantileResolution,
    result: VrpFreeCostBudgetResult,
) -> VrpFreeCostBudgetEvidence:
    return VrpFreeCostBudgetEvidence(
        fold_id=fold_id,
        structure_id=structure.structure_id,
        frozen_param_key=structure.frozen_param_key,
        train_end_ms=train_end_ms,
        calibration_bin=calibration_bin,
        resolution_status=resolution.status,
        resolution_level=resolution.resolved_level,
        resolution_reason=resolution.reason,
        resolution_sample_cutoff_ms=resolution.sample_cutoff_ms,
        p50_round_trip_structure_spread_usd=resolution.p50_round_trip_structure_spread_usd,
        p75_round_trip_structure_spread_usd=resolution.p75_round_trip_structure_spread_usd,
        sample_count=resolution.sample_count,
        distinct_month_count=resolution.distinct_month_count,
        cost_budget_status=result.status.value,
        budget_pass=result.budget_pass,
        gross_credit_usd=result.gross_credit_usd,
        width_usd=result.width_usd,
        gross_credit_to_width=result.gross_credit_to_width,
        max_absorbable_round_trip_spread_usd=result.max_absorbable_round_trip_spread_usd,
        p50_margin_spread_usd=result.p50_margin_spread_usd,
        p75_safety_spread_usd=result.p75_safety_spread_usd,
        net_credit_after_p50_margin_usd=result.net_credit_after_p50_margin_usd,
        net_credit_after_p75_safety_usd=result.net_credit_after_p75_safety_usd,
        fail_reasons=result.fail_reasons,
        authorizing=result.authorizing,
        capital_go_allowed=result.capital_go_allowed,
        non_authorizing_reason=result.non_authorizing_reason,
    )


def _inconclusive_cost_evidence(
    *,
    fold_id: str,
    train_end_ms: int,
    structure: DefinedRiskStructure,
    calibration_bin: VrpFreeCalibrationBin | None,
    resolution: SpreadQuantileResolution | None,
    reason: str,
) -> VrpFreeCostBudgetEvidence:
    return VrpFreeCostBudgetEvidence(
        fold_id=fold_id,
        structure_id=structure.structure_id,
        frozen_param_key=structure.frozen_param_key,
        train_end_ms=train_end_ms,
        calibration_bin=calibration_bin,
        resolution_status=resolution.status if resolution is not None else "INCONCLUSIVE",
        resolution_level=resolution.resolved_level if resolution is not None else "fail_closed",
        resolution_reason=reason,
        resolution_sample_cutoff_ms=(
            resolution.sample_cutoff_ms if resolution is not None else train_end_ms
        ),
        p50_round_trip_structure_spread_usd=(
            resolution.p50_round_trip_structure_spread_usd if resolution is not None else None
        ),
        p75_round_trip_structure_spread_usd=(
            resolution.p75_round_trip_structure_spread_usd if resolution is not None else None
        ),
        sample_count=resolution.sample_count if resolution is not None else 0,
        distinct_month_count=resolution.distinct_month_count if resolution is not None else 0,
        cost_budget_status=VrpFreeCostBudgetStatus.INCONCLUSIVE.value,
        budget_pass=False,
        gross_credit_usd=None,
        width_usd=None,
        gross_credit_to_width=None,
        max_absorbable_round_trip_spread_usd=None,
        p50_margin_spread_usd=None,
        p75_safety_spread_usd=None,
        net_credit_after_p50_margin_usd=None,
        net_credit_after_p75_safety_usd=None,
        fail_reasons=(reason,),
    )


def _calibration_bin_for_structure(
    *,
    fold_id: str,
    structure: DefinedRiskStructure,
    index_path: Sequence[IndexPathPoint],
    fold_bin_overrides: Mapping[Any, Mapping[str, str] | VrpFreeCalibrationBin],
) -> VrpFreeCalibrationBin:
    override = _lookup_bin_override(fold_id, structure, fold_bin_overrides)
    if override is not None:
        return override

    short_leg = next(leg for leg in structure.legs if leg.side is Side.SHORT)
    option_type = short_leg.option_type.value
    dte_bucket = dte_days_to_bucket(structure.dte_days)
    if dte_bucket == "out_of_grid":
        raise ValueError("structure dte_days falls outside PLAN_BINNING")
    spot = _nearest_index_price(index_path, structure.entry_quote_ts_ms)
    if spot is None:
        raise ValueError("missing index path for moneyness/regime bin")
    moneyness_bucket = abs_log_moneyness_to_bucket(abs(math.log(short_leg.strike / spot)))
    if moneyness_bucket == "out_of_grid":
        raise ValueError("structure moneyness falls outside PLAN_BINNING")
    trailing_rv, abs_24h_return = _regime_inputs(index_path, structure.entry_quote_ts_ms)
    return VrpFreeCalibrationBin(
        option_type=option_type,
        dte_bucket=dte_bucket,
        moneyness_bucket=moneyness_bucket,
        regime_label=classify_regime_label(trailing_rv, abs_24h_return),
    )


def _lookup_bin_override(
    fold_id: str,
    structure: DefinedRiskStructure,
    overrides: Mapping[Any, Mapping[str, str] | VrpFreeCalibrationBin],
) -> VrpFreeCalibrationBin | None:
    keys = (
        (fold_id, structure.structure_id),
        (fold_id, structure.frozen_param_key),
        structure.structure_id,
        structure.frozen_param_key,
        fold_id,
        "default",
    )
    for key in keys:
        value = overrides.get(key)
        if value is None:
            continue
        if isinstance(value, VrpFreeCalibrationBin):
            return value
        return VrpFreeCalibrationBin(
            option_type=str(value["option_type"]),
            dte_bucket=str(value["dte_bucket"]),
            moneyness_bucket=str(value["moneyness_bucket"]),
            regime_label=str(value["regime_label"]),
        )
    return None


def _structure_credit_width_usd(structure: DefinedRiskStructure) -> tuple[float, float]:
    short_leg = next(leg for leg in structure.legs if leg.side is Side.SHORT)
    multiplier = float(short_leg.contract_multiplier)
    quantity = float(structure.quantity)
    return (
        float(structure.net_credit * multiplier * quantity),
        float(structure.width * multiplier * quantity),
    )


def _aggregate_cost_budget_status(evidence: Sequence[VrpFreeCostBudgetEvidence]) -> str:
    if not evidence:
        return VrpFreeCostBudgetStatus.INCONCLUSIVE.value
    statuses = {item.cost_budget_status for item in evidence}
    if VrpFreeCostBudgetStatus.FAIL_BUDGET.value in statuses:
        return VrpFreeCostBudgetStatus.FAIL_BUDGET.value
    if statuses == {VrpFreeCostBudgetStatus.PASS.value}:
        return VrpFreeCostBudgetStatus.PASS.value
    return VrpFreeCostBudgetStatus.INCONCLUSIVE.value


def _fold_economics(
    evaluations: Sequence[committed_vrp_verdict.VrpFoldEvaluation],
    cost_evidence: Sequence[VrpFreeCostBudgetEvidence],
) -> tuple[VrpFreeFoldEconomics, ...]:
    by_fold: dict[str, list[VrpFreeCostBudgetEvidence]] = {}
    for item in cost_evidence:
        by_fold.setdefault(item.fold_id, []).append(item)

    rows: list[VrpFreeFoldEconomics] = []
    for evaluation in evaluations:
        costs = tuple(by_fold.get(evaluation.fold_id, ()))
        p50 = sum(item.p50_margin_spread_usd or 0.0 for item in costs)
        p75 = sum(item.p75_safety_spread_usd or 0.0 for item in costs)
        absorbable = [
            item.max_absorbable_round_trip_spread_usd
            for item in costs
            if item.max_absorbable_round_trip_spread_usd is not None
        ]
        rows.append(
            VrpFreeFoldEconomics(
                fold_id=evaluation.fold_id,
                selected_param_key=evaluation.selected_param_key,
                train_trial_count=evaluation.train_trial_count,
                heldout_eval_count=evaluation.heldout_eval_count,
                test_rerun_count=evaluation.test_rerun_count,
                entries=evaluation.entries,
                pnl_usd=float(evaluation.pnl_usd),
                gross_of_spread_pnl_usd=float(evaluation.pnl_usd + p75),
                fee_only_pnl_usd=float(evaluation.pnl_usd),
                calibrated_net_p50_pnl_usd=float(evaluation.pnl_usd - p50),
                calibrated_net_p75_pnl_usd=float(evaluation.pnl_usd - p75),
                max_absorbable_round_trip_spread_usd=(
                    min(absorbable) if absorbable else None
                ),
                cost_budget_statuses=tuple(item.cost_budget_status for item in costs),
            )
        )
    return tuple(rows)


def _surface_reason_codes(reasons: Sequence[str]) -> tuple[str, ...]:
    return tuple(dict.fromkeys(_surface_reason_code(reason) for reason in reasons))


def _surface_reason_code(reason: str) -> str:
    if "GO" not in reason:
        return reason
    return _REASON_CODE_REMAP.get(reason, reason.replace("GO", "CAPITAL"))


def _committed_hard_gate_payload(
    report: committed_vrp_verdict.VrpWalkForwardVerdictReport,
) -> dict[str, Any]:
    return {
        "run_status": report.run_status,
        "clean_heldout_positive": report.clean_heldout_go,
        "reason_codes": list(_surface_reason_codes(report.reason_codes)),
        "fold_ids": list(report.fold_ids),
        "aggregate_sharpe": report.aggregate_sharpe,
        "max_drawdown_including_stress": report.max_drawdown_including_stress,
        "folds_non_negative": report.folds_non_negative,
        "folds_with_entries": report.folds_with_entries,
        "total_entries": report.total_entries,
        "train_trials": report.train_trials,
        "heldout_evals": report.heldout_evals,
        "trial_budget_valid": report.trial_budget_valid,
        "max_loss_invariant_ok": report.max_loss_invariant_ok,
    }


def _lineage_top_level(lineage: Mapping[str, Any]) -> dict[str, Any]:
    return dict(lineage)


def _fold_map(folds: Sequence[Mapping[str, str]]) -> dict[str, Mapping[str, str]]:
    return {str(fold["id"]): fold for fold in folds}


def _nearest_index_price(index_path: Sequence[IndexPathPoint], timestamp_ms: int) -> float | None:
    prior = [point for point in index_path if point.timestamp_ms <= timestamp_ms]
    if not prior:
        return None
    return float(max(prior, key=lambda point: point.timestamp_ms).index_price)


def _regime_inputs(index_path: Sequence[IndexPathPoint], timestamp_ms: int) -> tuple[float, float]:
    prior = tuple(point for point in index_path if point.timestamp_ms <= timestamp_ms)
    if len(prior) < 2:
        return 0.0, 0.0
    prices = [float(point.index_price) for point in prior]
    returns = [math.log(prices[i] / prices[i - 1]) for i in range(1, len(prices))]
    if not returns:
        return 0.0, 0.0
    trailing = returns[-30:] if len(returns) >= 30 else returns
    mean = sum(trailing) / len(trailing)
    variance = sum((value - mean) ** 2 for value in trailing) / max(len(trailing) - 1, 1)
    rv = math.sqrt(max(variance, 0.0)) * math.sqrt(365.0)
    day = returns[-24:] if len(returns) >= 24 else returns
    abs_24h = abs(sum(day))
    return float(rv), float(abs_24h)


def _parse_iso_ms(value: str) -> int:
    normalized = value[:-1] + "+00:00" if value.endswith("Z") else value
    parsed = datetime.fromisoformat(normalized)
    return int(parsed.astimezone(UTC).timestamp() * 1000)


def _iso_ms(ts_ms: int) -> str:
    return datetime.fromtimestamp(ts_ms / 1000, tz=UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _committed_value(committed: committed_vrp_verdict.VrpVerdict | str) -> str:
    if isinstance(committed, committed_vrp_verdict.VrpVerdict):
        return committed.value
    return str(committed)


def _coerce_free_outcome(value: str | VrpFreeOutcome) -> VrpFreeOutcome:
    if isinstance(value, VrpFreeOutcome):
        return value
    if str(value) not in _ALLOWED_FREE_OUTCOMES:
        return VrpFreeOutcome.INCONCLUSIVE
    return VrpFreeOutcome(str(value))


def _source_quality_value(value: SourceQuality | str) -> str:
    return value.value if isinstance(value, SourceQuality) else str(value)


__all__ = [
    "FREE_BREAKEVEN_SCHEMA_VERSION",
    "FREE_OUTCOME_INCONCLUSIVE",
    "FREE_OUTCOME_NO_GO",
    "FREE_OUTCOME_PROMISING",
    "FREE_WALK_FORWARD_SCHEMA_VERSION",
    "VrpBacktestResult",
    "VrpBacktestStep",
    "VrpBreakevenResult",
    "VrpBreakevenSample",
    "VrpDefinedRiskStrategy",
    "VrpFoldEvaluation",
    "VrpFreeBreakevenReport",
    "VrpFreeCalibrationBin",
    "VrpFreeCostBudgetEvidence",
    "VrpFreeFoldEconomics",
    "VrpFreeOutcome",
    "VrpFreeWalkForwardReport",
    "VrpVerdict",
    "committed_option_costs",
    "committed_options_margin",
    "committed_vrp_breakeven",
    "committed_vrp_defined_risk",
    "committed_vrp_engine",
    "committed_vrp_verdict",
    "free_non_authorizing_lineage",
    "load_preregistration",
    "map_committed_vrp_to_free_outcome",
    "plan_grid_hash",
    "run_free_breakeven",
    "run_free_walk_forward",
    "run_vrp_backtest",
    "verify_preregistration",
]

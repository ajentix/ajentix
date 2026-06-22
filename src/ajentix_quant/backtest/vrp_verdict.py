"""Walk-forward held-out verdict aggregation for VRP defined-risk spreads.

This mirrors the strategy-v2 Edge Verdict surface while enforcing the VRP frozen fold,
source-quality, concentration, stress, and hard trial-budget rules.  It does not load
files or reach the network; callers provide already-executed fold facts.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from enum import StrEnum
from typing import Any

from ajentix_quant.backtest.metrics import sharpe
from ajentix_quant.options.types import SourceQuality
from ajentix_quant.research.vrp_preregistration import (
    PLAN_FOLDS,
    PLAN_GO_BAR,
    PLAN_STRUCTURE_GRID,
    PLAN_TRIAL_BUDGET,
)

VRP_WALK_FORWARD_SCHEMA_VERSION = "vrp-walk-forward-verdict-v1"

RUN_STATUS_VALID = "valid"
RUN_STATUS_INVALID = "invalid"

REASON_FOLD_DELETION = "FOLD_DELETION_OR_EXTRA_FOLD"
REASON_TEST_RERUN = "TEST_RERUN_OR_HELDOUT_RETRY"
REASON_GRID_MUTATION = "GRID_MUTATION"
REASON_TRIAL_BUDGET_BREACH = "TRIAL_BUDGET_BREACH"
REASON_SOURCE_QUALITY_BLOCK = "SOURCE_QUALITY_NOT_FULL_REAL_CHAIN"
REASON_NON_AUTHORIZING_DEPENDENCE = "NON_AUTHORIZING_DEPENDENCE"
REASON_STRESS_OMITTED = "STRESS_OMITTED"
REASON_MAX_LOSS_INVARIANT = "MAX_LOSS_INVARIANT_FAILED"
REASON_SHARPE = "SHARPE_BELOW_GO_BAR"
REASON_MDD = "MDD_INCLUDING_STRESS_ABOVE_GO_BAR"
REASON_FOLD_COLLAPSE = "FOLD_COLLAPSE"
REASON_ENTRIES = "INSUFFICIENT_HELDOUT_ENTRIES"
REASON_CONCENTRATION = "CONCENTRATION_CAP_BREACH"
REASON_CLEAN_HELDOUT_GO = "CLEAN_HELDOUT_GO"

_NON_AUTHORIZING_LABELS = {
    "maker",
    "marks_only",
    "mark",
    "marks",
    "dvol",
    "proxy",
    "sample",
    "fixture",
    "naked",
    "btc",
    "iron_condor",
}


class VrpVerdict(StrEnum):
    GO = "GO"
    NO_GO = "NO_GO"
    INCONCLUSIVE = "INCONCLUSIVE"


@dataclass(frozen=True, kw_only=True)
class VrpFoldEvaluation:
    """Primitive facts from exactly one frozen TEST fold evaluation."""

    fold_id: str
    selected_param_key: str
    param_freeze_hash: str
    grid_hash: str
    train_trial_count: int
    heldout_eval_count: int
    test_rerun_count: int
    entries: int
    pnl_usd: float
    returns: tuple[float, ...]
    max_drawdown: float
    stress_max_drawdown: float
    source_quality: Mapping[str, SourceQuality | str]
    cost_modes: tuple[str, ...] = ("taker",)
    non_authorizing_labels: tuple[str, ...] = ()
    cluster_pnl: Mapping[str, float] | None = None
    max_loss_invariant_ok: bool = True
    stress_evaluated: bool = True


@dataclass(frozen=True, kw_only=True)
class VrpWalkForwardVerdictReport:
    """Aggregated walk-forward verdict and hard-gate evidence."""

    schema_version: str
    run_status: str
    verdict: VrpVerdict
    clean_heldout_go: bool
    reason_codes: tuple[str, ...]
    fold_ids: tuple[str, ...]
    aggregate_sharpe: float
    max_drawdown_including_stress: float
    folds_non_negative: int
    folds_with_entries: int
    total_entries: int
    train_trials: int
    heldout_evals: int
    max_single_fold_pnl_share: float
    max_single_cluster_pnl_share: float
    fold_collapse: bool
    concentration_failure: bool
    source_quality_authorizing: bool
    trial_budget_valid: bool
    non_authorizing_dependence: bool
    max_loss_invariant_ok: bool

    def as_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "run_status": self.run_status,
            "verdict": self.verdict.value,
            "clean_heldout_go": self.clean_heldout_go,
            "reason_codes": list(self.reason_codes),
            "fold_ids": list(self.fold_ids),
            "aggregate_sharpe": self.aggregate_sharpe,
            "max_drawdown_including_stress": self.max_drawdown_including_stress,
            "folds_non_negative": self.folds_non_negative,
            "folds_with_entries": self.folds_with_entries,
            "total_entries": self.total_entries,
            "train_trials": self.train_trials,
            "heldout_evals": self.heldout_evals,
            "max_single_fold_pnl_share": self.max_single_fold_pnl_share,
            "max_single_cluster_pnl_share": self.max_single_cluster_pnl_share,
            "fold_collapse": self.fold_collapse,
            "concentration_failure": self.concentration_failure,
            "source_quality_authorizing": self.source_quality_authorizing,
            "trial_budget_valid": self.trial_budget_valid,
            "non_authorizing_dependence": self.non_authorizing_dependence,
            "max_loss_invariant_ok": self.max_loss_invariant_ok,
        }


def plan_grid_hash() -> str:
    """Return the canonical hash of the frozen VRP structure grid."""

    return _canonical_sha256(PLAN_STRUCTURE_GRID)


def decide_vrp_walk_forward(
    evaluations: Sequence[VrpFoldEvaluation],
    *,
    expected_folds: Sequence[Mapping[str, str]] = PLAN_FOLDS,
    trial_budget: Mapping[str, Any] = PLAN_TRIAL_BUDGET,
    go_bar: Mapping[str, Any] = PLAN_GO_BAR,
) -> VrpWalkForwardVerdictReport:
    """Aggregate frozen TEST folds and enforce hard invalidation rules."""

    expected_ids = tuple(str(fold["id"]) for fold in expected_folds)
    rows = tuple(sorted(evaluations, key=lambda row: row.fold_id))
    observed_ids = tuple(row.fold_id for row in rows)
    reasons: list[str] = []

    if observed_ids != expected_ids:
        reasons.append(REASON_FOLD_DELETION)
    if any(row.heldout_eval_count != 1 or row.test_rerun_count != 1 for row in rows):
        reasons.append(REASON_TEST_RERUN)
    expected_grid_hash = plan_grid_hash()
    if any(row.grid_hash != expected_grid_hash for row in rows):
        reasons.append(REASON_GRID_MUTATION)

    train_trials = sum(row.train_trial_count for row in rows)
    heldout_evals = sum(row.heldout_eval_count for row in rows)
    trial_budget_valid = (
        train_trials <= int(trial_budget["max_train_trials"])
        and heldout_evals <= int(trial_budget["max_heldout_evals"])
    )
    if not trial_budget_valid:
        reasons.append(REASON_TRIAL_BUDGET_BREACH)

    source_quality_authorizing = all(
        _source_quality_authorizing(row.source_quality) for row in rows
    )
    if not source_quality_authorizing:
        reasons.append(REASON_SOURCE_QUALITY_BLOCK)

    non_authorizing_dependence = any(_has_non_authorizing_dependence(row) for row in rows)
    if non_authorizing_dependence:
        reasons.append(REASON_NON_AUTHORIZING_DEPENDENCE)

    stress_complete = all(row.stress_evaluated for row in rows)
    if not stress_complete:
        reasons.append(REASON_STRESS_OMITTED)

    max_loss_ok = all(row.max_loss_invariant_ok for row in rows)
    if not max_loss_ok:
        reasons.append(REASON_MAX_LOSS_INVARIANT)

    aggregate_returns = _aggregate_returns(rows)
    aggregate_sharpe = sharpe(aggregate_returns, periods_per_year=max(len(rows), 1))
    mdd_incl_stress = max(
        (max(row.max_drawdown, row.stress_max_drawdown) for row in rows),
        default=0.0,
    )
    folds_non_negative = sum(1 for row in rows if row.pnl_usd >= 0.0)
    folds_with_entries = sum(1 for row in rows if row.entries > 0)
    total_entries = sum(row.entries for row in rows)
    fold_share = _max_positive_share({row.fold_id: row.pnl_usd for row in rows})
    cluster_share = _max_positive_share(_cluster_pnl(rows))

    fold_collapse = folds_non_negative < int(go_bar["min_folds_nonneg"])
    concentration_failure = (
        fold_share > float(go_bar["max_single_fold_pnl_share"])
        or cluster_share > float(go_bar["max_single_cluster_pnl_share"])
    )

    if aggregate_sharpe + 1e-12 < float(go_bar["min_sharpe"]):
        reasons.append(REASON_SHARPE)
    if mdd_incl_stress > float(go_bar["max_mdd_incl_stress"]) + 1e-12:
        reasons.append(REASON_MDD)
    if fold_collapse:
        reasons.append(REASON_FOLD_COLLAPSE)
    if (
        folds_with_entries < int(go_bar["min_folds_with_entries"])
        or total_entries < int(go_bar["min_total_entries"])
    ):
        reasons.append(REASON_ENTRIES)
    if concentration_failure:
        reasons.append(REASON_CONCENTRATION)

    invalid_reasons = {
        REASON_FOLD_DELETION,
        REASON_TEST_RERUN,
        REASON_GRID_MUTATION,
        REASON_TRIAL_BUDGET_BREACH,
    }
    run_status = (
        RUN_STATUS_INVALID
        if any(reason in invalid_reasons for reason in reasons)
        else RUN_STATUS_VALID
    )
    blocking_inconclusive = {
        REASON_SOURCE_QUALITY_BLOCK,
        REASON_NON_AUTHORIZING_DEPENDENCE,
        REASON_STRESS_OMITTED,
        REASON_MAX_LOSS_INVARIANT,
    }
    clean_go = run_status == RUN_STATUS_VALID and not reasons
    if clean_go:
        verdict = VrpVerdict.GO
        reasons = [REASON_CLEAN_HELDOUT_GO]
    elif run_status == RUN_STATUS_INVALID or any(
        reason in blocking_inconclusive for reason in reasons
    ):
        verdict = VrpVerdict.INCONCLUSIVE
    else:
        verdict = VrpVerdict.NO_GO

    return VrpWalkForwardVerdictReport(
        schema_version=VRP_WALK_FORWARD_SCHEMA_VERSION,
        run_status=run_status,
        verdict=verdict,
        clean_heldout_go=clean_go,
        reason_codes=tuple(dict.fromkeys(reasons)),
        fold_ids=observed_ids,
        aggregate_sharpe=float(aggregate_sharpe),
        max_drawdown_including_stress=float(mdd_incl_stress),
        folds_non_negative=folds_non_negative,
        folds_with_entries=folds_with_entries,
        total_entries=total_entries,
        train_trials=train_trials,
        heldout_evals=heldout_evals,
        max_single_fold_pnl_share=float(fold_share),
        max_single_cluster_pnl_share=float(cluster_share),
        fold_collapse=fold_collapse,
        concentration_failure=concentration_failure,
        source_quality_authorizing=source_quality_authorizing,
        trial_budget_valid=trial_budget_valid,
        non_authorizing_dependence=non_authorizing_dependence,
        max_loss_invariant_ok=max_loss_ok,
    )


def _source_quality_authorizing(source_quality: Mapping[str, SourceQuality | str]) -> bool:
    if not source_quality:
        return False
    return all(
        _quality_value(value) == SourceQuality.VENUE.value
        for value in source_quality.values()
    )


def _has_non_authorizing_dependence(row: VrpFoldEvaluation) -> bool:
    labels = {label.lower() for label in row.non_authorizing_labels}
    labels.update(mode.lower() for mode in row.cost_modes)
    return bool(labels & _NON_AUTHORIZING_LABELS)


def _aggregate_returns(rows: tuple[VrpFoldEvaluation, ...]) -> list[float]:
    out: list[float] = []
    for row in rows:
        if row.returns:
            out.extend(float(value) for value in row.returns)
        else:
            out.append(float(row.pnl_usd) / 1_000.0)
    return out


def _cluster_pnl(rows: tuple[VrpFoldEvaluation, ...]) -> dict[str, float]:
    out: dict[str, float] = {}
    for row in rows:
        cluster_map = row.cluster_pnl or {row.fold_id: row.pnl_usd}
        for key, value in cluster_map.items():
            out[str(key)] = out.get(str(key), 0.0) + float(value)
    return out


def _max_positive_share(values: Mapping[str, float]) -> float:
    positives = [float(value) for value in values.values() if value > 0.0]
    total = sum(positives)
    if total <= 0.0:
        return 0.0
    return float(max(positives) / total)


def _quality_value(value: SourceQuality | str) -> str:
    if isinstance(value, SourceQuality):
        return value.value
    return str(value)


def _canonical_sha256(obj: Any) -> str:
    return hashlib.sha256(
        json.dumps(obj, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()

from __future__ import annotations

from typing import Any

import pytest

from ajentix_quant.backtest.vrp_verdict import (
    REASON_CONCENTRATION,
    REASON_FOLD_COLLAPSE,
    REASON_GRID_MUTATION,
    REASON_NON_AUTHORIZING_DEPENDENCE,
    REASON_SOURCE_QUALITY_BLOCK,
    REASON_TEST_RERUN,
    REASON_TRIAL_BUDGET_BREACH,
    RUN_STATUS_INVALID,
    VrpFoldEvaluation,
    VrpVerdict,
    decide_vrp_walk_forward,
    plan_grid_hash,
)
from ajentix_quant.options.types import SourceQuality
from ajentix_quant.research.vrp_preregistration import PLAN_FOLDS


def _folds(**overrides: dict[str, Any]) -> list[VrpFoldEvaluation]:
    rows: list[VrpFoldEvaluation] = []
    for i, fold in enumerate(PLAN_FOLDS, start=1):
        data: dict[str, Any] = {
            "fold_id": fold["id"],
            "selected_param_key": "grid|put",
            "param_freeze_hash": f"freeze-{fold['id']}",
            "grid_hash": plan_grid_hash(),
            "train_trial_count": 108,
            "heldout_eval_count": 1,
            "test_rerun_count": 1,
            "entries": 2,
            "pnl_usd": 10.0 + i,
            "returns": (0.018 + i * 0.001,),
            "max_drawdown": 0.04,
            "stress_max_drawdown": 0.10,
            "source_quality": {"option_chain": SourceQuality.VENUE},
            "cost_modes": ("taker",),
            "cluster_pnl": {f"cluster-{fold['id']}": 10.0 + i},
            "max_loss_invariant_ok": True,
            "stress_evaluated": True,
        }
        data.update(overrides.get(fold["id"], {}))
        data.update(overrides.get("all", {}))
        rows.append(VrpFoldEvaluation(**data))
    return rows


def test_clean_walk_forward_go_requires_test_once_per_fold() -> None:
    report = decide_vrp_walk_forward(_folds())

    assert report.verdict is VrpVerdict.GO
    assert report.clean_heldout_go is True
    assert report.heldout_evals == 7

    rerun = _folds(F3={"heldout_eval_count": 2, "test_rerun_count": 2})
    rerun_report = decide_vrp_walk_forward(rerun)
    assert rerun_report.run_status == RUN_STATUS_INVALID
    assert rerun_report.verdict is VrpVerdict.INCONCLUSIVE
    assert REASON_TEST_RERUN in rerun_report.reason_codes


def test_fold_deletion_and_grid_mutation_are_invalid() -> None:
    deletion_report = decide_vrp_walk_forward(_folds()[:-1])
    mutation_report = decide_vrp_walk_forward(_folds(F2={"grid_hash": "mutated"}))

    assert deletion_report.run_status == RUN_STATUS_INVALID
    assert deletion_report.verdict is VrpVerdict.INCONCLUSIVE
    assert mutation_report.run_status == RUN_STATUS_INVALID
    assert mutation_report.verdict is VrpVerdict.INCONCLUSIVE
    assert REASON_GRID_MUTATION in mutation_report.reason_codes


def test_trial_budget_breach_is_enforced_not_just_reported() -> None:
    report = decide_vrp_walk_forward(_folds(all={"train_trial_count": 109}))

    assert report.train_trials == 763
    assert report.trial_budget_valid is False
    assert report.run_status == RUN_STATUS_INVALID
    assert report.verdict is VrpVerdict.INCONCLUSIVE
    assert REASON_TRIAL_BUDGET_BREACH in report.reason_codes


def test_fold_collapse_and_concentration_block_go() -> None:
    collapse = _folds(
        F1={"pnl_usd": -10.0, "returns": (-0.02,)},
        F2={"pnl_usd": -10.0, "returns": (-0.02,)},
        F3={"pnl_usd": -10.0, "returns": (-0.02,)},
        F4={"pnl_usd": -10.0, "returns": (-0.02,)},
    )
    collapse_report = decide_vrp_walk_forward(collapse)

    concentration = _folds(F1={"pnl_usd": 100.0, "cluster_pnl": {"cluster-hot": 100.0}})
    concentration_report = decide_vrp_walk_forward(concentration)

    assert collapse_report.verdict is VrpVerdict.NO_GO
    assert collapse_report.fold_collapse is True
    assert REASON_FOLD_COLLAPSE in collapse_report.reason_codes
    assert concentration_report.verdict is VrpVerdict.NO_GO
    assert concentration_report.concentration_failure is True
    assert REASON_CONCENTRATION in concentration_report.reason_codes


@pytest.mark.parametrize(
    "override, expected_reason",
    [
        ({"cost_modes": ("maker",)}, REASON_NON_AUTHORIZING_DEPENDENCE),
        ({"non_authorizing_labels": ("naked",)}, REASON_NON_AUTHORIZING_DEPENDENCE),
        ({"source_quality": {"option_chain": SourceQuality.FIXTURE}}, REASON_SOURCE_QUALITY_BLOCK),
        ({"source_quality": {"option_chain": SourceQuality.PROXY}}, REASON_SOURCE_QUALITY_BLOCK),
    ],
)
def test_profitable_maker_proxy_naked_fixture_paths_still_block_go(
    override: dict[str, object],
    expected_reason: str,
) -> None:
    report = decide_vrp_walk_forward(_folds(all=override))

    assert report.verdict is VrpVerdict.INCONCLUSIVE
    assert report.clean_heldout_go is False
    assert expected_reason in report.reason_codes

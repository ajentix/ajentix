from __future__ import annotations

import pytest

from ajentix_quant.adapters.base import SourceQuality
from ajentix_quant.backtest.vrp_free_cost_budget import VrpFreeCostBudgetStatus
from ajentix_quant.research.vrp_free_final_verdict import (
    ALLOWED_FREE_FINAL_VERDICTS,
    FREE_FINAL_VERDICT_INCONCLUSIVE,
    FREE_FINAL_VERDICT_NO_GO,
    FREE_FINAL_VERDICT_PROMISING,
    REASON_COST_BUDGET_FAIL,
    REASON_ECONOMIC_FAILURE,
    REASON_STRESS_MAX_LOSS_BREACH,
    decide_vrp_free_final_verdict,
)
from ajentix_quant.research.vrp_free_preregistration import PLAN_PROMISING_CONFIRMATION_TRIGGER


def _has_go_scalar(value: object) -> bool:
    """Recursively detect an exact 'GO' scalar (NOT the 'NO_GO' substring)."""
    if isinstance(value, dict):
        return any(_has_go_scalar(item) for item in value.values())
    if isinstance(value, (list, tuple)):
        return any(_has_go_scalar(item) for item in value)
    return value == "GO"


def _perfect_kwargs(**overrides: object) -> dict[str, object]:
    kwargs: dict[str, object] = {
        "precalibration_artifact": {"schema_version": "vrp-free-precalibration-v1", "id": "pre"},
        "preregistration": {"schema_version": "vrp-free-prereg-v1", "run_id": "vrp-free-test"},
        "preregistration_valid": True,
        "raw_history_manifest": {"schema_version": "raw", "scenario_id": "ETH", "rows": 1},
        "reconstructed_chain_manifest": {
            "schema_version": "reconstructed",
            "scenario_id": "ETH",
            "chains": 1,
        },
        "tardis_spread_calibration_manifest": {
            "schema_version": "tardis-free-spread-calibration",
            "scenario_id": "ETH",
            "resolved_bins": 1,
        },
        "breakeven_report": {"outcome": FREE_FINAL_VERDICT_PROMISING, "lineage_valid": True},
        "walk_forward_report": {
            "outcome": FREE_FINAL_VERDICT_PROMISING,
            "verdict": FREE_FINAL_VERDICT_PROMISING,
            "lineage_valid": True,
            "cost_budget_status": VrpFreeCostBudgetStatus.PASS.value,
        },
        "stress_result": {"status": "RAN", "ran": True, "max_loss_ok": True},
        "cost_budget_report": {"status": VrpFreeCostBudgetStatus.PASS.value, "budget_pass": True},
    }
    kwargs.update(overrides)
    return kwargs


def _decide(**overrides: object):
    return decide_vrp_free_final_verdict(**_perfect_kwargs(**overrides))


def test_verdict_vocabulary_excludes_go_and_go_payload_is_not_promising():
    assert ALLOWED_FREE_FINAL_VERDICTS == (
        FREE_FINAL_VERDICT_NO_GO,
        FREE_FINAL_VERDICT_PROMISING,
        FREE_FINAL_VERDICT_INCONCLUSIVE,
    )
    assert "GO" not in ALLOWED_FREE_FINAL_VERDICTS

    result = _decide(free_lineage_overrides={"verdict": "GO"})

    assert result.verdict == FREE_FINAL_VERDICT_INCONCLUSIVE
    assert result.verdict != "GO"
    assert result.verdict != FREE_FINAL_VERDICT_PROMISING
    assert result.lineage_valid is False
    assert any("GO payload" in mismatch for mismatch in result.lineage_mismatches)
    # The rejected malicious 'GO' override must NOT be re-serialized into the report.
    payload = result.as_dict()
    assert result.free_lineage.get("verdict") != "GO"
    assert result.free_lineage.get("outcome") != "GO"
    assert not _has_go_scalar(payload["free_lineage"])
    assert not _has_go_scalar(payload)


def test_perfect_reconstructed_positive_is_capped_at_promising_pending_real_spread():
    result = _decide()

    assert result.verdict == FREE_FINAL_VERDICT_PROMISING
    assert result.authorizing is False
    assert result.capital_go_allowed is False
    assert result.free_lineage["authorizing"] is False
    assert result.free_lineage["capital_go_allowed"] is False
    assert "Capital GO is structurally impossible" in result.characterization
    assert PLAN_PROMISING_CONFIRMATION_TRIGGER in result.promising_confirmation_trigger
    assert set(result.lineage_chain["hashes"]) == {
        "precalibration_artifact_sha256",
        "preregistration_sha256",
        "raw_history_manifest_sha256",
        "reconstructed_chain_manifest_sha256",
        "tardis_spread_calibration_manifest_sha256",
        "breakeven_report_sha256",
        "walk_forward_report_sha256",
        "stress_report_sha256",
        "cost_budget_sha256",
    }
    assert result.lineage_chain["complete"] is True


@pytest.mark.parametrize(
    ("overrides", "drop_fields", "expected"),
    [
        ({"source_quality": SourceQuality.VENUE.value}, (), "VENUE masquerade"),
        ({}, ("free_source_quality",), "free_source_quality"),
    ],
)
def test_final_layer_rejects_venue_masquerade_or_missing_free_label(
    overrides: dict[str, object],
    drop_fields: tuple[str, ...],
    expected: str,
):
    result = _decide(free_lineage_overrides=overrides, free_lineage_drop_fields=drop_fields)

    assert result.verdict == FREE_FINAL_VERDICT_INCONCLUSIVE
    assert result.lineage_valid is False
    assert any(expected in mismatch for mismatch in result.lineage_mismatches)


def test_missing_upstream_report_is_inconclusive_not_fabricated_positive():
    result = _decide(walk_forward_report=None)

    assert result.verdict == FREE_FINAL_VERDICT_INCONCLUSIVE
    assert result.inputs.walk_forward_report_present is False
    assert "FREE_WALK_FORWARD_REPORT_MISSING" in result.reason_codes


def test_economic_failure_forces_no_go():
    walk = dict(_perfect_kwargs()["walk_forward_report"])
    walk.update({"outcome": FREE_FINAL_VERDICT_NO_GO, "verdict": FREE_FINAL_VERDICT_NO_GO})

    result = _decide(walk_forward_report=walk)

    assert result.verdict == FREE_FINAL_VERDICT_NO_GO
    assert REASON_ECONOMIC_FAILURE in result.reason_codes


def test_cost_fail_budget_forces_no_go():
    result = _decide(cost_budget_report={"status": VrpFreeCostBudgetStatus.FAIL_BUDGET.value})

    assert result.verdict == FREE_FINAL_VERDICT_NO_GO
    assert REASON_COST_BUDGET_FAIL in result.reason_codes


def test_stress_max_loss_breach_forces_no_go():
    result = _decide(stress_result={"status": "RAN", "ran": True, "max_loss_ok": False})

    assert result.verdict == FREE_FINAL_VERDICT_NO_GO
    assert REASON_STRESS_MAX_LOSS_BREACH in result.reason_codes


def test_committed_authorizing_verdict_cannot_be_reused_as_free_verdict():
    result = _decide(free_lineage_overrides={"uses_committed_authorizing_verdict": True})

    assert result.verdict == FREE_FINAL_VERDICT_INCONCLUSIVE
    assert result.lineage_valid is False
    assert any(
        "committed authorizing verdict" in mismatch for mismatch in result.lineage_mismatches
    )


def test_upstream_committed_go_shape_is_invalid_lineage_not_free_promising():
    walk = dict(_perfect_kwargs()["walk_forward_report"])
    walk.update({"verdict": "GO", "outcome": "GO"})

    result = _decide(walk_forward_report=walk)

    assert result.verdict == FREE_FINAL_VERDICT_INCONCLUSIVE
    assert result.inputs.upstream_lineage_valid is False
    assert "UPSTREAM_LINEAGE_INVALID" in result.reason_codes
    # The upstream committed 'GO' shape must not leak as a 'GO' scalar in the report.
    assert not _has_go_scalar(result.as_dict())

"""Red-team drift coverage for VRP-free pre-registration frozen surfaces."""

from __future__ import annotations

import copy
from pathlib import Path
from typing import Any

from ajentix_quant.research import vrp_free_preregistration as vrp

REPO_ROOT = Path(__file__).resolve().parents[1]


def _build() -> dict[str, Any]:
    return vrp.build_preregistration(REPO_ROOT)


def _assert_invalid(artifact: dict[str, Any], expected: str) -> None:
    result = vrp.verify_preregistration(artifact, REPO_ROOT)
    assert result.valid is False
    assert result.run_status == "invalid"
    assert any(expected in mismatch for mismatch in result.mismatches), result.mismatches


def test_source_hash_drift_is_invalid():
    artifact = copy.deepcopy(_build())
    first_key = next(iter(artifact["source_hashes"]))
    artifact["source_hashes"][first_key] = "0" * 64
    _assert_invalid(artifact, "source hash drift")


def test_fold_boundary_drift_is_invalid():
    artifact = copy.deepcopy(_build())
    artifact["plan"]["folds"][0]["train_end"] = "2025-04-01T00:00:00Z"
    _assert_invalid(artifact, "fold-boundary drift")


def test_structure_grid_drift_is_invalid():
    artifact = copy.deepcopy(_build())
    artifact["plan"]["structure_grid"]["dte_targets"] = [21, 30, 60]
    _assert_invalid(artifact, "structure-grid drift")


def test_reconstruction_config_drift_is_invalid():
    artifact = copy.deepcopy(_build())
    artifact["plan"]["reconstruction_config"]["cadence_hours"] = 12
    _assert_invalid(artifact, "reconstruction-config drift")


def test_precalibration_config_drift_is_invalid():
    artifact = copy.deepcopy(_build())
    artifact["plan"]["precalibration_config"]["sample_months"].append("2026-07-01")
    _assert_invalid(artifact, "pre-calibration config drift")


def test_precalibration_config_hash_drift_is_invalid():
    artifact = copy.deepcopy(_build())
    artifact["precalibration_config_sha256"] = "0" * 64
    _assert_invalid(artifact, "pre-calibration config hash drift")


def test_cost_budget_bar_drift_is_invalid():
    artifact = copy.deepcopy(_build())
    artifact["plan"]["cost_budget_bar"]["spread_safety_multiplier"] = 1.10
    _assert_invalid(artifact, "cost-budget-bar drift")


def test_trial_budget_drift_is_invalid():
    artifact = copy.deepcopy(_build())
    artifact["plan"]["trial_budget"]["max_train_trials"] = 757
    _assert_invalid(artifact, "trial-budget drift")


def test_raw_source_manifest_drift_is_invalid():
    artifact = copy.deepcopy(_build())
    scenario = next(iter(artifact["raw_source_manifest_sha256"]))
    artifact["raw_source_manifest_sha256"][scenario] = "deadbeef"
    _assert_invalid(artifact, "raw-source manifest drift")


def test_reconstructed_cache_manifest_drift_is_invalid():
    artifact = copy.deepcopy(_build())
    scenario = next(iter(artifact["reconstructed_cache_manifest_sha256"]))
    artifact["reconstructed_cache_manifest_sha256"][scenario] = "deadbeef"
    _assert_invalid(artifact, "reconstructed-cache manifest drift")


def test_tardis_sample_manifest_drift_is_invalid():
    artifact = copy.deepcopy(_build())
    scenario = next(iter(artifact["tardis_sample_manifest_sha256"]))
    artifact["tardis_sample_manifest_sha256"][scenario] = "deadbeef"
    _assert_invalid(artifact, "Tardis-sample manifest drift")


def test_spread_calibration_manifest_drift_is_invalid():
    artifact = copy.deepcopy(_build())
    scenario = next(iter(artifact["spread_calibration_manifest_sha256"]))
    artifact["spread_calibration_manifest_sha256"][scenario] = "deadbeef"
    _assert_invalid(artifact, "spread-calibration manifest drift")


def test_spread_calibration_precalibration_hash_drift_is_invalid():
    artifact = copy.deepcopy(_build())
    scenario = next(iter(artifact["spread_calibration_precalibration_config_sha256"]))
    artifact["spread_calibration_precalibration_config_sha256"][scenario] = "deadbeef"
    _assert_invalid(artifact, "spread-calibration pre-calibration config drift")


def test_stress_selector_input_drift_is_invalid():
    artifact = copy.deepcopy(_build())
    artifact["stress_selector_input_sha256"] = "deadbeef"
    _assert_invalid(artifact, "stress-selector-input manifest drift")


def test_source_quality_bridge_drift_is_invalid():
    artifact = copy.deepcopy(_build())
    artifact["plan"]["source_quality_bridge"]["capital_go_allowed"] = True
    _assert_invalid(artifact, "source-quality bridge drift")


def test_outcome_rules_drift_is_invalid():
    artifact = copy.deepcopy(_build())
    artifact["plan"]["outcome_rules"]["allowed_outcomes"].append("GO")
    _assert_invalid(artifact, "outcome-rule drift")


def test_run_id_tamper_is_invalid():
    artifact = copy.deepcopy(_build())
    artifact["run_id"] = "vrp-free-000000000000"
    _assert_invalid(artifact, "run_id")


def test_schema_version_mismatch_is_invalid():
    artifact = copy.deepcopy(_build())
    artifact["schema_version"] = "vrp-free-prereg-v999"
    _assert_invalid(artifact, "schema_version")

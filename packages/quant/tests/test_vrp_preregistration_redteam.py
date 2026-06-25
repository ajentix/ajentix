"""Red-team drift coverage for VRP pre-registration frozen surfaces."""

from __future__ import annotations

import copy
from pathlib import Path
from typing import Any

from ajentix_quant.research import vrp_preregistration as vrp

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


def test_plan_constant_drift_is_invalid():
    artifact = copy.deepcopy(_build())
    artifact["plan"]["primary_equity_usd"] = 500.0
    _assert_invalid(artifact, "plan-constant drift")


def test_structure_grid_drift_is_invalid():
    artifact = copy.deepcopy(_build())
    artifact["plan"]["structure_grid"]["dte_targets"] = [21, 30, 60]
    _assert_invalid(artifact, "structure-grid drift")


def test_greek_provenance_drift_is_invalid():
    artifact = copy.deepcopy(_build())
    artifact["plan"]["greek_provenance"]["local_formula"] = "binomial"
    _assert_invalid(artifact, "greek-provenance drift")


def test_stress_rule_drift_is_invalid():
    artifact = copy.deepcopy(_build())
    artifact["plan"]["stress_rule"]["k"] = 4
    _assert_invalid(artifact, "stress-rule drift")


def test_stress_window_drift_is_invalid():
    artifact = copy.deepcopy(_build())
    artifact["stress_windows"] = [
        {"start": "2025-01-01T00:00:00Z", "end": "2025-01-02T00:00:00Z"}
    ]
    _assert_invalid(artifact, "stress-window drift")


def test_trial_budget_drift_is_invalid():
    artifact = copy.deepcopy(_build())
    artifact["plan"]["trial_budget"]["max_train_trials"] = 757
    _assert_invalid(artifact, "trial-budget drift")


def test_raw_source_manifest_drift_is_invalid():
    artifact = copy.deepcopy(_build())
    scenario = next(iter(artifact["raw_source_manifest_sha256"]))
    artifact["raw_source_manifest_sha256"][scenario] = "deadbeef"
    _assert_invalid(artifact, "raw-source manifest drift")


def test_normalized_cache_manifest_drift_is_invalid():
    artifact = copy.deepcopy(_build())
    scenario = next(iter(artifact["normalized_cache_manifest_sha256"]))
    artifact["normalized_cache_manifest_sha256"][scenario] = "deadbeef"
    _assert_invalid(artifact, "normalized-cache manifest drift")


def test_run_id_tamper_is_invalid():
    artifact = copy.deepcopy(_build())
    artifact["run_id"] = "vrp-000000000000"
    _assert_invalid(artifact, "run_id")


def test_schema_version_mismatch_is_invalid():
    artifact = copy.deepcopy(_build())
    artifact["schema_version"] = "vrp-prereg-v999"
    _assert_invalid(artifact, "schema_version")

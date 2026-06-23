"""G001 Phase 0: free-data-native VRP pre-registration governance."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from ajentix_quant.research import vrp_free_preregistration as vrp

REPO_ROOT = Path(__file__).resolve().parents[1]


def _build() -> dict[str, object]:
    return vrp.build_preregistration(REPO_ROOT)


def _write_manifest(root: Path, payload: dict[str, object]) -> Path:
    path = root / vrp.DEFAULT_SCENARIO_ID / "manifest.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, sort_keys=True) + "\n", encoding="utf-8")
    return path


def test_build_is_deterministic_and_run_id_derives_from_content_hash():
    a = _build()
    b = _build()
    assert a == b
    assert str(a["run_id"]).startswith("vrp-free-")
    assert str(a["content_hash"])[:12] == str(a["run_id"]).removeprefix("vrp-free-")


def test_fresh_schema_build_verifies_valid_without_real_cache_or_precalibration_artifact():
    artifact = _build()
    result = vrp.verify_preregistration(artifact, REPO_ROOT)
    assert result.valid is True
    assert result.run_status == "valid"
    assert result.mismatches == ()
    assert artifact["precalibration_artifact_sha256"] == vrp.MISSING_SHA
    assert next(iter(artifact["raw_source_manifest_sha256"].values())) == vrp.MISSING_SHA


def test_write_preregistration_refuses_emit_without_required_manifests_and_precalibration(tmp_path):
    with pytest.raises(vrp.PreregistrationError):
        vrp.write_preregistration(REPO_ROOT, out_dir=tmp_path.as_posix())
    assert list(tmp_path.iterdir()) == []


def test_write_and_load_round_trip_with_tiny_fixture_manifests(tmp_path):
    pre = vrp.write_precalibration_artifact(tmp_path, out_dir="pre")
    raw = _write_manifest(tmp_path / "raw", {"kind": "raw", "rows": 0})
    reconstructed = _write_manifest(
        tmp_path / "reconstructed", {"kind": "reconstructed", "rows": 0}
    )
    tardis = _write_manifest(tmp_path / "tardis", {"kind": "tardis", "rows": 0})
    calibration = _write_manifest(
        tmp_path / "calibration",
        {
            "kind": "spread_calibration",
            "rows": 0,
            "precalibration_config_sha256": vrp.precalibration_config_sha256(),
        },
    )
    stress = tmp_path / "stress-selector-input.json"
    stress.write_text(json.dumps({"stress": []}, sort_keys=True) + "\n", encoding="utf-8")

    dest = vrp.write_preregistration(
        REPO_ROOT,
        raw_manifest_path=raw,
        reconstructed_manifest_path=reconstructed,
        tardis_sample_manifest_path=tardis,
        spread_calibration_manifest_path=calibration,
        precalibration_artifact_path=pre,
        stress_selector_input_path=stress,
        out_dir=(tmp_path / "out").as_posix(),
    )
    loaded = vrp.load_preregistration(dest)
    result = vrp.verify_preregistration(loaded, REPO_ROOT)

    assert dest.is_file()
    assert loaded["run_id"].startswith("vrp-free-")
    assert loaded["precalibration_artifact_sha256"] != vrp.MISSING_SHA
    assert result.valid is True


def test_locked_plan_values_present():
    artifact = _build()
    plan = artifact["plan"]

    assert plan["schema_version"] == vrp.SCHEMA_VERSION
    assert plan["scenarios"] == {"ETH": "deribit_history_eth_vrp_free_v1"}
    assert "BTC" not in plan["scenarios"]
    assert plan["primary_equity_usd"] == 1000.0
    assert plan["equity_grid"] == [500.0, 1000.0, 2000.0]
    assert len(plan["folds"]) == 7
    assert plan["warmup_start"] == "2024-08-01T00:00:00Z"
    assert plan["coverage_window"] == ["2024-09-01T00:00:00Z", "2026-06-01T00:00:00Z"]

    reconstruction = plan["reconstruction_config"]
    assert reconstruction["cadence_hours"] == 8
    assert reconstruction["utc_hours"] == [0, 8, 16]
    assert reconstruction["include_required_stress_timestamps"] is True
    assert reconstruction["include_expiry_settlement_timestamps"] is True
    assert reconstruction["no_future_trades"] is True

    grid = plan["structure_grid"]
    assert grid["structure_types"] == ["put_credit_spread", "call_credit_spread"]
    assert grid["dte_targets"] == [21, 30, 45]
    assert grid["short_leg_abs_delta"] == [0.10, 0.16, 0.25]
    assert grid["width_usd"] == [100, 150, 200]
    assert grid["min_credit_to_width"] == [0.15, 0.20]
    assert grid["rolls"] is False

    cost_budget = plan["cost_budget_bar"]
    assert cost_budget["spread_quantile"] == "p75"
    assert cost_budget["spread_safety_multiplier"] == 1.25
    assert cost_budget["median_spread_margin_multiplier"] == 1.50
    assert cost_budget["min_samples_per_bin"] == 30
    assert cost_budget["min_distinct_months_per_bin"] == 6
    assert cost_budget["missing_required_month_behavior"] == "INCONCLUSIVE"
    assert cost_budget["post_calibration_threshold_change_behavior"] == "INVALID"

    bridge = plan["source_quality_bridge"]
    assert bridge["legacy_source_quality"] == "FIXTURE"
    assert bridge["forbid_venue"] is True
    assert bridge["free_source_quality"] == "reconstructed_from_real_trade_iv"
    assert bridge["spread_source_quality"] == "calibrated_spread_sample"
    assert bridge["authorizing"] is False
    assert bridge["capital_go_allowed"] is False
    assert bridge["non_authorizing_reason"] == "reconstructed_from_real_trade_iv"

    outcome = plan["outcome_rules"]
    assert outcome["allowed_outcomes"] == [
        "NO_GO",
        "PROMISING_PENDING_REAL_SPREAD",
        "INCONCLUSIVE",
    ]
    assert outcome["no_capital_go_from_reconstructed_only"] is True
    assert outcome["capital_go_allowed"] is False
    assert "GO" not in outcome["allowed_outcomes"]


def test_load_missing_or_garbage_raises(tmp_path):
    with pytest.raises(vrp.PreregistrationError):
        vrp.load_preregistration(tmp_path / "missing.json")
    bad = tmp_path / "bad.json"
    bad.write_text("not json{", encoding="utf-8")
    with pytest.raises(vrp.PreregistrationError):
        vrp.load_preregistration(bad)

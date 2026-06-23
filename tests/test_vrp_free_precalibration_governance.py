"""Pre-calibration governance for VRP-free spread calibration."""

from __future__ import annotations

import json

from ajentix_quant.research import vrp_free_preregistration as vrp


def test_precalibration_artifact_is_deterministic_and_hash_is_stable():
    a = vrp.build_precalibration_artifact()
    b = vrp.build_precalibration_artifact()
    assert a == b
    assert a["schema_version"] == vrp.PRECALIBRATION_SCHEMA_VERSION
    assert a["artifact_id"].startswith("vrp-free-precalibration-")
    assert a["precalibration_config_sha256"] == vrp.precalibration_config_sha256()
    assert a["precalibration_config"]["sample_months"][0] == "2024-08-01"
    assert a["precalibration_config"]["sample_months"][-1] == "2026-06-01"
    assert len(a["precalibration_config"]["sample_months"]) == 23


def test_precalibration_artifact_writes_before_calibration_exists(tmp_path):
    dest = vrp.write_precalibration_artifact(tmp_path, out_dir="docs/preregistration")
    assert dest.is_file()
    assert vrp.precalibration_config_sha256()[:12] in dest.name
    loaded = json.loads(dest.read_text(encoding="utf-8"))
    assert loaded == vrp.load_precalibration_artifact(dest)
    assert loaded["calibration_output_required"] is False
    assert loaded["emittable_before_calibration"] is True


def test_precalibration_config_freezes_cost_budget_and_fallback_rules():
    cfg = vrp.precalibration_config()
    assert cfg["cost_budget_bar"]["spread_quantile"] == "p75"
    assert cfg["cost_budget_bar"]["spread_safety_multiplier"] == 1.25
    assert cfg["cost_budget_bar"]["median_spread_margin_multiplier"] == 1.50
    assert cfg["binning"]["fallback_order"] == [
        "option_type+dte_bucket+moneyness_bucket+regime_label",
        "option_type+dte_bucket+moneyness_bucket",
        "option_type+dte_bucket",
        "option_type+moneyness_bucket",
        "fail_closed",
    ]
    assert cfg["regime_labels"]["tail"].startswith("trailing_30d_rv_annualized > 1.00")
    assert cfg["unit_conversions"]["vol_points"] == "iv_fraction * 100"
    assert "sample_timestamp <= fold.train_end" in cfg["fold_causal_calibration_rule"]


def test_changing_a_precalibration_constant_changes_the_hash(monkeypatch):
    original = vrp.precalibration_config_sha256()
    monkeypatch.setitem(vrp.PLAN_COST_BUDGET_BAR, "spread_safety_multiplier", 1.26)
    assert vrp.precalibration_config_sha256() != original

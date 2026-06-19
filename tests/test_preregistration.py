"""G001: strategy-v2 pre-registration governance (build, verify, drift detection)."""

import copy
import json
from pathlib import Path

import pytest

from ajentix_quant.research import preregistration as prereg

REPO_ROOT = Path(__file__).resolve().parents[1]


def _build():
    return prereg.build_preregistration(REPO_ROOT)


def test_build_is_deterministic():
    a = _build()
    b = _build()
    assert a == b
    assert a["run_id"] == b["run_id"]
    assert a["run_id"].startswith("stratv2-")
    assert a["content_hash"][:12] == a["run_id"].split("-", 1)[1]


def test_fresh_build_verifies_valid():
    art = _build()
    result = prereg.verify_preregistration(art, REPO_ROOT)
    assert result.valid is True
    assert result.run_status == "valid"
    assert result.mismatches == ()


def test_write_and_load_round_trip(tmp_path):
    dest = prereg.write_preregistration(tmp_path, out_dir="docs/preregistration")
    assert dest.is_file()
    loaded = prereg.load_preregistration(dest)
    built = _build()
    # cache_root resolves relative to tmp_path here, so caches are MISSING but consistent
    assert loaded["run_id"] == built["run_id"] or loaded["run_id"].startswith("stratv2-")
    # round-trip the loaded artifact: it must self-verify against tmp_path
    result = prereg.verify_preregistration(loaded, tmp_path)
    assert result.valid is True


def test_source_hash_drift_is_invalid():
    art = copy.deepcopy(_build())
    # tamper a frozen source hash -> verify must catch it
    first_key = next(iter(art["source_hashes"]))
    art["source_hashes"][first_key] = "0" * 64
    result = prereg.verify_preregistration(art, REPO_ROOT)
    assert result.valid is False
    assert result.run_status == "invalid"
    assert any("source hash drift" in m or "content_hash drift" in m for m in result.mismatches)


def test_plan_constant_drift_is_invalid():
    art = copy.deepcopy(_build())
    art["plan"]["a1_bar"]["min_qualifying_pct"] = 0.0  # weaken the bar -> must be caught
    result = prereg.verify_preregistration(art, REPO_ROOT)
    assert result.valid is False
    assert any("plan-constant drift" in m or "content_hash drift" in m for m in result.mismatches)


def test_cache_manifest_drift_is_invalid():
    art = copy.deepcopy(_build())
    scenario = next(iter(art["cache_manifest_sha256"]))
    art["cache_manifest_sha256"][scenario] = "deadbeef"
    result = prereg.verify_preregistration(art, REPO_ROOT)
    assert result.valid is False
    assert any("cache manifest drift" in m or "content_hash drift" in m for m in result.mismatches)


def test_run_id_tamper_is_invalid():
    art = copy.deepcopy(_build())
    art["run_id"] = "stratv2-000000000000"
    result = prereg.verify_preregistration(art, REPO_ROOT)
    assert result.valid is False
    assert any("run_id" in m for m in result.mismatches)


def test_schema_version_mismatch_is_invalid():
    art = copy.deepcopy(_build())
    art["schema_version"] = "stratv2-prereg-v999"
    result = prereg.verify_preregistration(art, REPO_ROOT)
    assert result.valid is False
    assert any("schema_version" in m for m in result.mismatches)


def test_load_missing_or_garbage_raises(tmp_path):
    with pytest.raises(prereg.PreregistrationError):
        prereg.load_preregistration(tmp_path / "nope.json")
    bad = tmp_path / "bad.json"
    bad.write_text("not json{", encoding="utf-8")
    with pytest.raises(prereg.PreregistrationError):
        prereg.load_preregistration(bad)


def test_a1_bar_locked_values_present():
    # the locked numeric bar must carry the approved-plan values
    art = _build()
    bar = art["plan"]["a1_bar"]
    assert bar["min_insample_funding_rows"] == 900
    assert bar["min_qualifying_pct"] == 0.10
    assert bar["min_qualifying_windows"] == 80
    assert bar["min_clusters"] == 6
    assert bar["maker_can_authorize"] is False
    budget = art["plan"]["trial_budget"]
    assert budget["total_heldout_cap"] == 56
    assert len(art["plan"]["folds"]) == 7


def test_committed_artifact_exists_and_is_wellformed():
    # the run's pre-registration must be committed under docs/preregistration/
    d = REPO_ROOT / "docs" / "preregistration"
    arts = sorted(d.glob("stratv2-*.json")) if d.is_dir() else []
    assert arts, "no committed strategy-v2 pre-registration artifact found"
    data = json.loads(arts[0].read_text(encoding="utf-8"))
    assert data["schema_version"] == prereg.SCHEMA_VERSION
    assert data["run_id"].startswith("stratv2-")
    assert "source_hashes" in data and "plan" in data and "cache_manifest_sha256" in data

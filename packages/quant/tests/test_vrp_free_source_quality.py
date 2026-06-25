"""Executable source-quality bridge tests for VRP-free reconstructed evidence."""

from __future__ import annotations

from pathlib import Path

from ajentix_quant.adapters.base import SourceQuality
from ajentix_quant.research import vrp_free_preregistration as vrp

REPO_ROOT = Path(__file__).resolve().parents[1]


def _valid_payload() -> dict[str, object]:
    bridge = vrp.PLAN_SOURCE_QUALITY_BRIDGE
    return {
        "verdict": "PROMISING_PENDING_REAL_SPREAD",
        "source_quality": SourceQuality.FIXTURE.value,
        "legacy_source_quality": bridge["legacy_source_quality"],
        "free_source_quality": bridge["free_source_quality"],
        "spread_source_quality": bridge["spread_source_quality"],
        "authorizing": False,
        "capital_go_allowed": False,
        "non_authorizing_reason": bridge["non_authorizing_reason"],
    }


def test_bridge_freezes_legacy_fixture_without_extending_source_quality_enum():
    artifact = vrp.build_preregistration(REPO_ROOT)
    bridge = artifact["plan"]["source_quality_bridge"]
    assert SourceQuality.FIXTURE.name == "FIXTURE"
    assert SourceQuality.VENUE.name == "VENUE"
    assert {member.name for member in SourceQuality} == {
        "VENUE",
        "FROZEN_SNAPSHOT",
        "FIXTURE",
        "PROXY",
        "ABSENT",
    }
    assert bridge["legacy_source_quality"] == "FIXTURE"
    assert bridge["legacy_option_leg_source_quality"] == "SourceQuality.FIXTURE"
    assert bridge["forbidden_reconstructed_option_leg_source_quality"] == "SourceQuality.VENUE"
    assert bridge["forbid_venue"] is True


def test_valid_reconstructed_bridge_payload_passes_thin_guard():
    result = vrp.validate_free_lineage_payload(_valid_payload())
    assert result.valid is True
    assert result.mismatches == ()


def test_venue_forbidden_on_reconstructed_payload():
    payload = _valid_payload()
    payload["source_quality"] = SourceQuality.VENUE.value
    result = vrp.validate_free_lineage_payload(payload)
    assert result.valid is False
    assert any("VENUE masquerade" in mismatch for mismatch in result.mismatches)


def test_free_source_quality_label_is_required():
    payload = _valid_payload()
    payload.pop("free_source_quality")
    result = vrp.validate_free_lineage_payload(payload)
    assert result.valid is False
    assert any("free_source_quality" in mismatch for mismatch in result.mismatches)


def test_non_authorizing_reason_is_required_and_exact():
    payload = _valid_payload()
    payload["non_authorizing_reason"] = "calibrated_spread_sample"
    result = vrp.validate_free_lineage_payload(payload)
    assert result.valid is False
    assert any("non_authorizing_reason" in mismatch for mismatch in result.mismatches)

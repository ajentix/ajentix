"""Invalid-lineage red-team tests for VRP-free no-capital-GO governance."""

from __future__ import annotations

from pathlib import Path

from ajentix_quant.adapters.base import SourceQuality
from ajentix_quant.research import vrp_free_preregistration as vrp

REPO_ROOT = Path(__file__).resolve().parents[1]


def _payload(**overrides: object) -> dict[str, object]:
    bridge = vrp.PLAN_SOURCE_QUALITY_BRIDGE
    payload: dict[str, object] = {
        "verdict": "PROMISING_PENDING_REAL_SPREAD",
        "source_quality": SourceQuality.FIXTURE.value,
        "free_source_quality": bridge["free_source_quality"],
        "spread_source_quality": bridge["spread_source_quality"],
        "authorizing": False,
        "capital_go_allowed": False,
        "non_authorizing_reason": bridge["non_authorizing_reason"],
    }
    payload.update(overrides)
    return payload


def test_outcome_rule_layer_excludes_capital_go():
    artifact = vrp.build_preregistration(REPO_ROOT)
    rules = artifact["plan"]["outcome_rules"]
    assert rules["allowed_outcomes"] == [
        "NO_GO",
        "PROMISING_PENDING_REAL_SPREAD",
        "INCONCLUSIVE",
    ]
    assert "GO" not in rules["allowed_outcomes"]
    assert rules["no_capital_go_from_reconstructed_only"] is True
    assert rules["go_payload_behavior"] == "INVALID_LINEAGE"


def test_go_payload_in_free_verdict_is_invalid_lineage():
    result = vrp.validate_free_lineage_payload(_payload(verdict="GO"))
    assert result.valid is False
    assert any("GO payload" in m or "not allowed" in m for m in result.mismatches)


def test_reconstructed_as_venue_is_invalid_lineage():
    result = vrp.validate_free_lineage_payload(
        _payload(source_quality=SourceQuality.VENUE.value)
    )
    assert result.valid is False
    assert any("VENUE masquerade" in mismatch for mismatch in result.mismatches)


def test_committed_authorizing_verdict_reuse_pattern_is_invalid_lineage():
    result = vrp.validate_free_lineage_payload(
        _payload(uses_committed_authorizing_verdict=True, authorizing=True)
    )
    assert result.valid is False
    assert any("committed authorizing verdict" in mismatch for mismatch in result.mismatches)
    assert any("authorizing must be false" in mismatch for mismatch in result.mismatches)


def test_positive_reconstructed_evidence_is_promising_pending_real_spread_at_most():
    assert (
        vrp.max_free_verdict_for_valid_reconstructed_evidence(economics_pass=True)
        == "PROMISING_PENDING_REAL_SPREAD"
    )
    assert vrp.max_free_verdict_for_valid_reconstructed_evidence(economics_pass=False) == "NO_GO"
    assert (
        vrp.max_free_verdict_for_valid_reconstructed_evidence(
            economics_pass=True, inconclusive=True
        )
        == "INCONCLUSIVE"
    )
    result = vrp.validate_free_lineage_payload(_payload(verdict="PROMISING_PENDING_REAL_SPREAD"))
    assert result.valid is True


def test_missing_free_label_or_reason_is_invalid_lineage():
    missing_label = _payload()
    missing_label.pop("free_source_quality")
    label_result = vrp.validate_free_lineage_payload(missing_label)
    assert label_result.valid is False
    assert any("free_source_quality" in mismatch for mismatch in label_result.mismatches)

    missing_reason = _payload()
    missing_reason.pop("non_authorizing_reason")
    reason_result = vrp.validate_free_lineage_payload(missing_reason)
    assert reason_result.valid is False
    assert any("non_authorizing_reason" in mismatch for mismatch in reason_result.mismatches)

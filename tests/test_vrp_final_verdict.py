from __future__ import annotations

from ajentix_quant.backtest.vrp_breakeven import VRP_BRANCH_NO_GO, VRP_BRANCH_WALK_FORWARD
from ajentix_quant.research.vrp_final_verdict import (
    ADR_REASON_NO_CLEAN_HELDOUT_GO,
    ADR_REASON_STRESS,
    REASON_INVALID_LINEAGE,
    REASON_SOURCE_QUALITY_BLOCK,
    REASON_STRESS_MISSING,
    VERDICT_GO,
    VERDICT_INCONCLUSIVE,
    VERDICT_NO_GO,
    VrpFinalVerdictInputs,
    build_vrp_final_verdict,
    decide_vrp_final_verdict,
    should_promote_vrp_adr_0002,
)

PREREG = {"run_id": "vrp-test", "content_hash": "abc"}
PREREG_SHA = "a" * 64


def _breakeven(branch: str = VRP_BRANCH_WALK_FORWARD) -> dict[str, object]:
    return {
        "schema_version": "vrp-breakeven-v1",
        "branch_decision": branch,
        "param_freeze_hash": "freeze",
        "selected_param_keys": ["grid|put"],
    }


def _walk_forward(**overrides: object) -> dict[str, object]:
    payload: dict[str, object] = {
        "schema_version": "vrp-walk-forward-verdict-v1",
        "run_status": "valid",
        "verdict": "GO",
        "clean_heldout_go": True,
        "fold_ids": [f"F{i}" for i in range(1, 8)],
        "source_quality_authorizing": True,
        "trial_budget_valid": True,
        "non_authorizing_dependence": False,
        "fold_collapse": False,
        "concentration_failure": False,
        "max_loss_invariant_ok": True,
        "source_quality": {"option_chain": "venue"},
    }
    payload.update(overrides)
    return payload


def _stress(**overrides: object) -> dict[str, object]:
    payload: dict[str, object] = {
        "ran": True,
        "max_loss_invariant_ok": True,
        "non_authorizing_dependence": False,
        "source_quality": {"stress_index": "venue"},
    }
    payload.update(overrides)
    return payload


def test_final_verdict_chains_hashes_and_promotes_only_clean_go() -> None:
    report = build_vrp_final_verdict(
        preregistration=PREREG,
        preregistration_sha256=PREREG_SHA,
        preregistration_valid=True,
        breakeven=_breakeven(),
        walk_forward=_walk_forward(),
        stress=_stress(),
    )

    assert report.verdict == VERDICT_GO
    assert report.adr_0002_ready is True
    assert report.adr_0002_block_reasons == ()
    assert report.lineage_hashes["preregistration_sha256"] == PREREG_SHA
    assert report.lineage_hashes["breakeven_sha256"]
    assert report.lineage_hashes["walk_forward_sha256"]
    assert report.lineage_hashes["stress_sha256"]

    no_go = build_vrp_final_verdict(
        preregistration=PREREG,
        preregistration_sha256=PREREG_SHA,
        preregistration_valid=True,
        breakeven=_breakeven(VRP_BRANCH_NO_GO),
        walk_forward=_walk_forward(verdict="NO_GO", clean_heldout_go=False),
        stress=_stress(),
    )
    assert no_go.verdict == VERDICT_NO_GO
    assert no_go.adr_0002_ready is False
    assert ADR_REASON_NO_CLEAN_HELDOUT_GO in no_go.adr_0002_block_reasons


def test_missing_stress_payloads_fail_closed_and_block_adr() -> None:
    for stress_payload in (
        None,
        {},
        {"max_loss_invariant_ok": True},
        {"ran": True},
        {"ran": True, "max_loss_invariant_ok": True, "non_authorizing_dependence": False},
        {"ran": True, "max_loss_invariant_ok": True, "source_quality": {}},
        {"ran": True, "max_loss_invariant_ok": True, "source_quality": {"stress_index": "proxy"}},
    ):
        report = build_vrp_final_verdict(
            preregistration=PREREG,
            preregistration_sha256=PREREG_SHA,
            preregistration_valid=True,
            breakeven=_breakeven(),
            walk_forward=_walk_forward(),
            stress=stress_payload,
        )

        assert report.verdict == VERDICT_INCONCLUSIVE
        assert report.inputs.stress_complete is False
        assert REASON_STRESS_MISSING in report.reason_codes
        assert report.adr_0002_ready is False
        assert ADR_REASON_STRESS in report.adr_0002_block_reasons


def test_fixture_source_quality_is_non_authorizing_and_inconclusive() -> None:
    report = build_vrp_final_verdict(
        preregistration=PREREG,
        preregistration_sha256=PREREG_SHA,
        preregistration_valid=True,
        breakeven=_breakeven(),
        walk_forward=_walk_forward(
            source_quality_authorizing=False,
            source_quality={"option_chain": "fixture"},
        ),
        stress=_stress(source_quality={"stress_index": "fixture"}),
    )

    assert report.verdict == VERDICT_INCONCLUSIVE
    assert report.adr_0002_ready is False
    assert REASON_SOURCE_QUALITY_BLOCK in report.reason_codes


def test_invalid_lineage_is_inconclusive() -> None:
    report = build_vrp_final_verdict(
        preregistration=PREREG,
        preregistration_sha256=PREREG_SHA,
        preregistration_valid=True,
        breakeven=_breakeven(),
        walk_forward=_walk_forward(),
        stress=_stress(),
        upstream_lineage=({"run_status": "invalid"},),
    )

    assert report.verdict == VERDICT_INCONCLUSIVE
    assert report.adr_0002_ready is False
    assert report.reason_codes == (REASON_INVALID_LINEAGE,)


def test_pure_adr_gate_requires_clean_heldout_go() -> None:
    inputs = VrpFinalVerdictInputs(
        preregistration_valid=True,
        lineage_consistent=True,
        breakeven_authorized=True,
        walk_forward_ran=True,
        clean_heldout_go=False,
        source_quality_authorizing=True,
        stress_complete=True,
        trial_budget_valid=True,
        non_authorizing_dependence=False,
        fold_collapse=False,
        concentration_failure=False,
        max_loss_invariant_ok=True,
    )

    verdict, _ = decide_vrp_final_verdict(inputs)
    ready, reasons = should_promote_vrp_adr_0002(verdict, inputs)

    assert verdict != VERDICT_GO
    assert ready is False
    assert reasons == (ADR_REASON_NO_CLEAN_HELDOUT_GO,)

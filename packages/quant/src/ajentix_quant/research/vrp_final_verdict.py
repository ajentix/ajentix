"""Final verdict chaining for the VRP defined-risk short-vol harness.

Pure decision functions mirror strategy-v2's final verdict: invalid lineage or
non-authorizing evidence yields ``INCONCLUSIVE``; ``GO`` is possible only on a clean
held-out walk-forward result with real-chain source quality, stress coverage, intact
max-loss invariants, and no maker/proxy/naked/DVOL/sample/fixture dependence.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ajentix_quant.backtest.vrp_breakeven import VRP_BRANCH_WALK_FORWARD
from ajentix_quant.backtest.vrp_verdict import VrpVerdict
from ajentix_quant.options.types import SourceQuality
from ajentix_quant.research.vrp_preregistration import (
    VerifyResult,
    load_preregistration,
    verify_preregistration,
)

VRP_FINAL_VERDICT_SCHEMA_VERSION = "vrp-final-verdict-v1"

VERDICT_GO = "GO"
VERDICT_NO_GO = "NO_GO"
VERDICT_INCONCLUSIVE = "INCONCLUSIVE"

REASON_PREREGISTRATION_INVALID = "PREREGISTRATION_INVALID"
REASON_INVALID_LINEAGE = "INVALID_PREREGISTRATION_LINEAGE"
REASON_BREAKEVEN_NOT_AUTHORIZED = "BREAKEVEN_BRANCH_DID_NOT_AUTHORIZE_HELDOUT"
REASON_NO_WALK_FORWARD_GO = "NO_CLEAN_WALK_FORWARD_GO"
REASON_SOURCE_QUALITY_BLOCK = "SOURCE_QUALITY_NOT_FULL_REAL_CHAIN"
REASON_STRESS_MISSING = "STRESS_MISSING_OR_NOT_AUTHORIZING"
REASON_TRIAL_BUDGET = "TRIAL_BUDGET_INVALID"
REASON_NON_AUTHORIZING = "NON_AUTHORIZING_DEPENDENCE"
REASON_FOLD_COLLAPSE = "FOLD_COLLAPSE_PRESENT"
REASON_CONCENTRATION = "CONCENTRATION_FAILURE_PRESENT"
REASON_MAX_LOSS = "MAX_LOSS_INVARIANT_FAILED"
REASON_CLEAN_HELDOUT_GO = "CLEAN_HELDOUT_GO"

ADR_REASON_NO_CLEAN_HELDOUT_GO = "NO_CLEAN_HELDOUT_GO"
ADR_REASON_INVALID_LINEAGE = "INVALID_PREREGISTRATION_LINEAGE"
ADR_REASON_SOURCE_QUALITY = "SOURCE_QUALITY_NOT_FULL_REAL_CHAIN"
ADR_REASON_STRESS = "STRESS_MISSING_OR_NOT_AUTHORIZING"
ADR_REASON_TRIAL_BUDGET = "TRIAL_BUDGET_INVALID"
ADR_REASON_NON_AUTHORIZING = "NON_AUTHORIZING_DEPENDENCE"
ADR_REASON_FOLD_COLLAPSE = "FOLD_COLLAPSE_PRESENT"
ADR_REASON_CONCENTRATION = "CONCENTRATION_FAILURE_PRESENT"
ADR_REASON_MAX_LOSS = "MAX_LOSS_INVARIANT_FAILED"


@dataclass(frozen=True, kw_only=True)
class VrpFinalVerdictInputs:
    """Primitive facts extracted from pre-registration and upstream reports."""

    preregistration_valid: bool
    lineage_consistent: bool
    breakeven_authorized: bool
    walk_forward_ran: bool
    clean_heldout_go: bool
    source_quality_authorizing: bool
    stress_complete: bool
    trial_budget_valid: bool
    non_authorizing_dependence: bool
    fold_collapse: bool
    concentration_failure: bool
    max_loss_invariant_ok: bool


@dataclass(frozen=True, kw_only=True)
class VrpFinalVerdictReport:
    """Final VRP verdict with chained hashes and ADR readiness."""

    schema_version: str
    run_status: str
    verdict: str
    reason_codes: tuple[str, ...]
    preregistration_sha256: str
    preregistration_run_id: str | None
    lineage_hashes: Mapping[str, str]
    inputs: VrpFinalVerdictInputs
    adr_0002_ready: bool
    adr_0002_block_reasons: tuple[str, ...]

    def as_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "run_status": self.run_status,
            "verdict": self.verdict,
            "reason_codes": list(self.reason_codes),
            "preregistration_sha256": self.preregistration_sha256,
            "preregistration_run_id": self.preregistration_run_id,
            "lineage_hashes": dict(self.lineage_hashes),
            "inputs": {
                "preregistration_valid": self.inputs.preregistration_valid,
                "lineage_consistent": self.inputs.lineage_consistent,
                "breakeven_authorized": self.inputs.breakeven_authorized,
                "walk_forward_ran": self.inputs.walk_forward_ran,
                "clean_heldout_go": self.inputs.clean_heldout_go,
                "source_quality_authorizing": self.inputs.source_quality_authorizing,
                "stress_complete": self.inputs.stress_complete,
                "trial_budget_valid": self.inputs.trial_budget_valid,
                "non_authorizing_dependence": self.inputs.non_authorizing_dependence,
                "fold_collapse": self.inputs.fold_collapse,
                "concentration_failure": self.inputs.concentration_failure,
                "max_loss_invariant_ok": self.inputs.max_loss_invariant_ok,
            },
            "adr_0002": {
                "ready": self.adr_0002_ready,
                "block_reasons": list(self.adr_0002_block_reasons),
            },
        }


def load_verified_preregistration(
    path: str | Path,
    repo_root: str | Path,
    **verify_kwargs: Any,
) -> tuple[dict[str, Any], VerifyResult, str]:
    """Load, verify, and sha-chain the VRP pre-registration artifact."""

    artifact = load_preregistration(path)
    verify = verify_preregistration(artifact, repo_root, **verify_kwargs)
    prereg_sha = hashlib.sha256(Path(path).read_bytes()).hexdigest()
    return artifact, verify, prereg_sha


def decide_vrp_final_verdict(inp: VrpFinalVerdictInputs) -> tuple[str, tuple[str, ...]]:
    """Map primitive facts to the final GO / NO_GO / INCONCLUSIVE decision."""

    if not inp.preregistration_valid:
        return VERDICT_INCONCLUSIVE, (REASON_PREREGISTRATION_INVALID,)
    if not inp.lineage_consistent:
        return VERDICT_INCONCLUSIVE, (REASON_INVALID_LINEAGE,)

    inconclusive: list[str] = []
    if not inp.source_quality_authorizing:
        inconclusive.append(REASON_SOURCE_QUALITY_BLOCK)
    if not inp.stress_complete:
        inconclusive.append(REASON_STRESS_MISSING)
    if not inp.trial_budget_valid:
        inconclusive.append(REASON_TRIAL_BUDGET)
    if inp.non_authorizing_dependence:
        inconclusive.append(REASON_NON_AUTHORIZING)
    if not inp.max_loss_invariant_ok:
        inconclusive.append(REASON_MAX_LOSS)
    if inconclusive:
        return VERDICT_INCONCLUSIVE, tuple(inconclusive)

    if inp.clean_heldout_go and inp.breakeven_authorized and inp.walk_forward_ran:
        return VERDICT_GO, (REASON_CLEAN_HELDOUT_GO,)

    reasons: list[str] = []
    if not inp.breakeven_authorized:
        reasons.append(REASON_BREAKEVEN_NOT_AUTHORIZED)
    if not inp.clean_heldout_go or not inp.walk_forward_ran:
        reasons.append(REASON_NO_WALK_FORWARD_GO)
    if inp.fold_collapse:
        reasons.append(REASON_FOLD_COLLAPSE)
    if inp.concentration_failure:
        reasons.append(REASON_CONCENTRATION)
    return VERDICT_NO_GO, tuple(reasons or (REASON_NO_WALK_FORWARD_GO,))


def should_promote_vrp_adr_0002(
    verdict: str,
    inp: VrpFinalVerdictInputs,
) -> tuple[bool, tuple[str, ...]]:
    """Promote ADR-0002 readiness only on a clean held-out GO."""

    reasons: list[str] = []
    if verdict != VERDICT_GO or not inp.clean_heldout_go or not inp.breakeven_authorized:
        reasons.append(ADR_REASON_NO_CLEAN_HELDOUT_GO)
    if not inp.preregistration_valid or not inp.lineage_consistent:
        reasons.append(ADR_REASON_INVALID_LINEAGE)
    if not inp.source_quality_authorizing:
        reasons.append(ADR_REASON_SOURCE_QUALITY)
    if not inp.stress_complete:
        reasons.append(ADR_REASON_STRESS)
    if not inp.trial_budget_valid:
        reasons.append(ADR_REASON_TRIAL_BUDGET)
    if inp.non_authorizing_dependence:
        reasons.append(ADR_REASON_NON_AUTHORIZING)
    if inp.fold_collapse:
        reasons.append(ADR_REASON_FOLD_COLLAPSE)
    if inp.concentration_failure:
        reasons.append(ADR_REASON_CONCENTRATION)
    if not inp.max_loss_invariant_ok:
        reasons.append(ADR_REASON_MAX_LOSS)
    return not reasons, tuple(reasons)


def build_vrp_final_verdict(
    *,
    preregistration: Mapping[str, Any],
    preregistration_sha256: str,
    preregistration_valid: bool,
    breakeven: Mapping[str, Any],
    walk_forward: Mapping[str, Any],
    stress: Mapping[str, Any] | None = None,
    source_quality: Mapping[str, SourceQuality | str] | None = None,
    upstream_lineage: Sequence[Mapping[str, Any]] = (),
) -> VrpFinalVerdictReport:
    """Build the final report from upstream evidence payloads."""

    lineage_consistent = preregistration_valid and all(
        row.get("run_status", "valid") == "valid" for row in upstream_lineage
    )
    stress_payload = dict(stress) if stress is not None else {}
    walk_payload = dict(walk_forward)
    breakeven_payload = dict(breakeven)
    quality = dict(source_quality or {})
    if not quality:
        quality = _merged_source_quality(walk_payload, stress_payload)
    stress_complete = _stress_payload_complete(stress_payload)
    stress_max_loss_ok = stress_payload.get("max_loss_invariant_ok") is True

    inputs = VrpFinalVerdictInputs(
        preregistration_valid=preregistration_valid,
        lineage_consistent=lineage_consistent,
        breakeven_authorized=breakeven_payload.get("branch_decision") == VRP_BRANCH_WALK_FORWARD,
        walk_forward_ran=bool(walk_payload.get("fold_ids") or walk_payload.get("ran", False)),
        clean_heldout_go=bool(walk_payload.get("clean_heldout_go", False))
        and walk_payload.get("verdict") == VrpVerdict.GO.value,
        source_quality_authorizing=_source_quality_authorizing(quality)
        and walk_payload.get("source_quality_authorizing") is not False
        and stress_payload.get("source_quality_authorizing") is not False,
        stress_complete=stress_complete,
        trial_budget_valid=bool(walk_payload.get("trial_budget_valid", False)),
        non_authorizing_dependence=bool(walk_payload.get("non_authorizing_dependence", False))
        or stress_payload.get("non_authorizing_dependence") is True,
        fold_collapse=bool(walk_payload.get("fold_collapse", False)),
        concentration_failure=bool(walk_payload.get("concentration_failure", False)),
        max_loss_invariant_ok=bool(
            walk_payload.get("max_loss_invariant_ok", False) and stress_max_loss_ok
        ),
    )
    verdict, reasons = decide_vrp_final_verdict(inputs)
    adr_ready, adr_block = should_promote_vrp_adr_0002(verdict, inputs)
    lineage_hashes = {
        "preregistration_sha256": preregistration_sha256,
        "breakeven_sha256": _canonical_sha256(breakeven_payload),
        "walk_forward_sha256": _canonical_sha256(walk_payload),
        "stress_sha256": _canonical_sha256(stress_payload),
    }
    return VrpFinalVerdictReport(
        schema_version=VRP_FINAL_VERDICT_SCHEMA_VERSION,
        run_status="valid" if preregistration_valid else "invalid",
        verdict=verdict,
        reason_codes=reasons,
        preregistration_sha256=preregistration_sha256,
        preregistration_run_id=_optional_str(preregistration.get("run_id")),
        lineage_hashes=lineage_hashes,
        inputs=inputs,
        adr_0002_ready=adr_ready,
        adr_0002_block_reasons=adr_block,
    )


def _stress_payload_complete(stress: Mapping[str, Any]) -> bool:
    if "ran" not in stress or stress["ran"] is not True:
        return False
    if (
        "max_loss_invariant_ok" not in stress
        or stress["max_loss_invariant_ok"] is not True
    ):
        return False
    if stress.get("non_authorizing_dependence") is True:
        return False
    if stress.get("source_quality_authorizing") is False:
        return False
    raw_quality = stress.get("source_quality")
    if not isinstance(raw_quality, Mapping) or not raw_quality:
        return False
    stress_quality = {str(key): value for key, value in raw_quality.items()}
    if not _source_quality_authorizing(stress_quality):
        return False
    return True

def _source_quality_authorizing(source_quality: Mapping[str, SourceQuality | str]) -> bool:
    if not source_quality:
        return False
    return all(
        _quality_value(value) == SourceQuality.VENUE.value
        for value in source_quality.values()
    )


def _merged_source_quality(
    walk_forward: Mapping[str, Any],
    stress: Mapping[str, Any],
) -> dict[str, SourceQuality | str]:
    out: dict[str, SourceQuality | str] = {}
    for payload in (walk_forward, stress):
        raw = payload.get("source_quality")
        if isinstance(raw, Mapping):
            for key, value in raw.items():
                out[str(key)] = str(value)
    return out


def _quality_value(value: SourceQuality | str) -> str:
    if isinstance(value, SourceQuality):
        return value.value
    return str(value)


def _optional_str(value: Any) -> str | None:
    if value is None:
        return None
    return str(value)


def _canonical_sha256(obj: Any) -> str:
    return hashlib.sha256(
        json.dumps(obj, sort_keys=True, separators=(",", ":"), default=str).encode("utf-8")
    ).hexdigest()

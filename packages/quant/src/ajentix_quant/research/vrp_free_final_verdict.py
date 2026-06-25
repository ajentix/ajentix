"""Final non-authorizing verdict mapper for free-data-native VRP research.

The free-data path can reject the VRP candidate or mark it promising enough for a
continuous real-spread confirmation. It can never produce, embed, or authorize a
capital GO from reconstructed option chains and sampled/calibrated spreads.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping, Sequence
from dataclasses import asdict, dataclass, is_dataclass
from enum import Enum, StrEnum
from typing import Any

from ajentix_quant.adapters.base import SourceQuality
from ajentix_quant.backtest.vrp_free_cost_budget import VrpFreeCostBudgetStatus
from ajentix_quant.backtest.vrp_free_stress import VrpFreeStressStatus
from ajentix_quant.research.vrp_free_preregistration import (
    DEFAULT_SCENARIO_ID,
    PLAN_OUTCOME_RULES,
    PLAN_PROMISING_CONFIRMATION_TRIGGER,
    PLAN_SOURCE_QUALITY_BRIDGE,
    max_free_verdict_for_valid_reconstructed_evidence,
    validate_free_lineage_payload,
)

VRP_FREE_FINAL_VERDICT_SCHEMA_VERSION = "aq-vrp-free-final-verdict-v1"

FREE_FINAL_VERDICT_NO_GO = "NO_GO"
FREE_FINAL_VERDICT_PROMISING = "PROMISING_PENDING_REAL_SPREAD"
FREE_FINAL_VERDICT_INCONCLUSIVE = "INCONCLUSIVE"
ALLOWED_FREE_FINAL_VERDICTS: tuple[str, ...] = tuple(
    str(value) for value in PLAN_OUTCOME_RULES["allowed_outcomes"]
)
MISSING_SHA256 = "MISSING"

REASON_PREREGISTRATION_INVALID = "PREREGISTRATION_INVALID"
REASON_FREE_LINEAGE_INVALID = "FREE_LINEAGE_INVALID"
REASON_UPSTREAM_LINEAGE_INVALID = "UPSTREAM_LINEAGE_INVALID"
REASON_ECONOMIC_FAILURE = "ECONOMIC_FAILURE"
REASON_COST_BUDGET_FAIL = "FREE_COST_BUDGET_FAIL"
REASON_COST_BUDGET_INCONCLUSIVE = "FREE_COST_BUDGET_INCONCLUSIVE"
REASON_COST_BUDGET_MISSING = "FREE_COST_BUDGET_MISSING"
REASON_STRESS_MISSING = "FREE_STRESS_MISSING"
REASON_STRESS_INCOMPLETE = "FREE_STRESS_INCOMPLETE"
REASON_STRESS_MAX_LOSS_BREACH = "FREE_STRESS_MAX_LOSS_BREACH"
REASON_BREAKEVEN_REPORT_MISSING = "FREE_BREAKEVEN_REPORT_MISSING"
REASON_WALK_FORWARD_REPORT_MISSING = "FREE_WALK_FORWARD_REPORT_MISSING"
REASON_PRECALIBRATION_MISSING = "FREE_PRECALIBRATION_ARTIFACT_MISSING"
REASON_RAW_HISTORY_MANIFEST_MISSING = "FREE_RAW_HISTORY_MANIFEST_MISSING"
REASON_RECONSTRUCTED_CHAIN_MANIFEST_MISSING = "FREE_RECONSTRUCTED_CHAIN_MANIFEST_MISSING"
REASON_TARDIS_CALIBRATION_MANIFEST_MISSING = "FREE_TARDIS_SPREAD_CALIBRATION_MANIFEST_MISSING"
REASON_ECONOMICS_NOT_POSITIVE = "FREE_ECONOMICS_NOT_POSITIVE"
REASON_PROMISING_PENDING_REAL_SPREAD = (
    "VALID_RECONSTRUCTED_POSITIVE_CAPPED_AT_PROMISING_PENDING_REAL_SPREAD"
)

CAPITAL_GO_IMPOSSIBLE_CHARACTERIZATION = (
    "Capital GO is structurally impossible from this evidence class: reconstructed "
    "option chains are derived from real Deribit-history trade IV rather than "
    "continuous venue bid/ask quotes, and Tardis-free spread calibration is "
    "sample-based rather than a continuous historical Deribit spread record. "
    f"{PLAN_PROMISING_CONFIRMATION_TRIGGER}"
)

_ARTIFACT_ORDER: tuple[str, ...] = (
    "precalibration_artifact",
    "preregistration",
    "raw_history_manifest",
    "reconstructed_chain_manifest",
    "tardis_spread_calibration_manifest",
    "breakeven_report",
    "walk_forward_report",
    "stress_report",
    "cost_budget",
)


class VrpFreeFinalVerdictValue(StrEnum):
    """Final verdict vocabulary for free-data-native VRP research."""

    NO_GO = FREE_FINAL_VERDICT_NO_GO
    PROMISING_PENDING_REAL_SPREAD = FREE_FINAL_VERDICT_PROMISING
    INCONCLUSIVE = FREE_FINAL_VERDICT_INCONCLUSIVE


@dataclass(frozen=True, kw_only=True)
class VrpFreeFinalVerdictInputs:
    """Primitive facts that drive the final free-data verdict."""

    preregistration_valid: bool
    lineage_complete: bool
    precalibration_present: bool
    raw_history_manifest_present: bool
    reconstructed_chain_manifest_present: bool
    tardis_spread_calibration_manifest_present: bool
    breakeven_report_present: bool
    walk_forward_report_present: bool
    stress_report_present: bool
    cost_budget_present: bool
    upstream_lineage_valid: bool
    economics_positive: bool
    economic_failure: bool
    cost_budget_status: str | None
    stress_complete: bool
    stress_max_loss_ok: bool
    free_lineage_valid: bool

    def as_dict(self) -> dict[str, Any]:
        return {
            "preregistration_valid": self.preregistration_valid,
            "lineage_complete": self.lineage_complete,
            "precalibration_present": self.precalibration_present,
            "raw_history_manifest_present": self.raw_history_manifest_present,
            "reconstructed_chain_manifest_present": self.reconstructed_chain_manifest_present,
            "tardis_spread_calibration_manifest_present": (
                self.tardis_spread_calibration_manifest_present
            ),
            "breakeven_report_present": self.breakeven_report_present,
            "walk_forward_report_present": self.walk_forward_report_present,
            "stress_report_present": self.stress_report_present,
            "cost_budget_present": self.cost_budget_present,
            "upstream_lineage_valid": self.upstream_lineage_valid,
            "economics_positive": self.economics_positive,
            "economic_failure": self.economic_failure,
            "cost_budget_status": self.cost_budget_status,
            "stress_complete": self.stress_complete,
            "stress_max_loss_ok": self.stress_max_loss_ok,
            "free_lineage_valid": self.free_lineage_valid,
        }


@dataclass(frozen=True, kw_only=True)
class VrpFreeFinalVerdict:
    """Final VRP-free verdict artifact with deterministic lineage chaining."""

    schema_version: str
    scenario_id: str
    run_status: str
    verdict: str
    allowed_verdicts: tuple[str, ...]
    reason_codes: tuple[str, ...]
    inputs: VrpFreeFinalVerdictInputs
    lineage_chain: Mapping[str, Any]
    free_lineage: Mapping[str, Any]
    lineage_valid: bool
    lineage_mismatches: tuple[str, ...]
    authorizing: bool
    capital_go_allowed: bool
    non_authorizing_reason: str
    free_source_quality: str
    spread_source_quality: str
    characterization: str
    promising_confirmation_trigger: str

    def as_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "scenario_id": self.scenario_id,
            "run_status": self.run_status,
            "verdict": self.verdict,
            "allowed_verdicts": list(self.allowed_verdicts),
            "reason_codes": list(self.reason_codes),
            "inputs": self.inputs.as_dict(),
            "lineage_chain": _normalise_json(self.lineage_chain),
            "free_lineage": dict(self.free_lineage),
            "lineage_valid": self.lineage_valid,
            "lineage_mismatches": list(self.lineage_mismatches),
            "authorizing": self.authorizing,
            "capital_go_allowed": self.capital_go_allowed,
            "non_authorizing_reason": self.non_authorizing_reason,
            "free_source_quality": self.free_source_quality,
            "spread_source_quality": self.spread_source_quality,
            "characterization": self.characterization,
            "promising_confirmation_trigger": self.promising_confirmation_trigger,
        }


@dataclass(frozen=True, kw_only=True)
class _DecisionFacts:
    reasons: tuple[str, ...]
    preregistration_valid: bool
    lineage_complete: bool
    precalibration_present: bool
    raw_history_manifest_present: bool
    reconstructed_chain_manifest_present: bool
    tardis_spread_calibration_manifest_present: bool
    breakeven_report_present: bool
    walk_forward_report_present: bool
    stress_report_present: bool
    cost_budget_present: bool
    upstream_lineage_valid: bool
    economics_positive: bool
    economic_failure: bool
    cost_budget_status: str | None
    cost_budget_fail: bool
    cost_budget_inconclusive: bool
    stress_complete: bool
    stress_max_loss_ok: bool
    stress_breach: bool


def build_vrp_free_lineage_chain(
    *,
    precalibration_artifact: Any | None = None,
    preregistration: Any | None = None,
    raw_history_manifest: Any | None = None,
    reconstructed_chain_manifest: Any | None = None,
    tardis_spread_calibration_manifest: Any | None = None,
    breakeven_report: Any | None = None,
    walk_forward_report: Any | None = None,
    stress_report: Any | None = None,
    cost_budget: Any | None = None,
    hash_overrides: Mapping[str, str] | None = None,
) -> dict[str, Any]:
    """Return deterministic artifact hashes plus a final chain hash."""

    artifacts = {
        "precalibration_artifact": precalibration_artifact,
        "preregistration": preregistration,
        "raw_history_manifest": raw_history_manifest,
        "reconstructed_chain_manifest": reconstructed_chain_manifest,
        "tardis_spread_calibration_manifest": tardis_spread_calibration_manifest,
        "breakeven_report": breakeven_report,
        "walk_forward_report": walk_forward_report,
        "stress_report": stress_report,
        "cost_budget": cost_budget,
    }
    overrides = dict(hash_overrides or {})
    hashes: dict[str, str] = {}
    present: dict[str, bool] = {}
    for name in _ARTIFACT_ORDER:
        override = overrides.get(name) or overrides.get(f"{name}_sha256")
        sha = (
            override
            if isinstance(override, str) and override
            else _artifact_sha256(artifacts[name])
        )
        hashes[f"{name}_sha256"] = sha
        present[name] = sha != MISSING_SHA256 and _artifact_present(artifacts[name])

    chain_material = {"artifact_order": list(_ARTIFACT_ORDER), "hashes": hashes}
    return {
        "artifact_order": list(_ARTIFACT_ORDER),
        "hashes": hashes,
        "present": present,
        "complete": all(present.values()),
        "chain_sha256": _canonical_sha256(chain_material),
    }


def decide_vrp_free_final_verdict(
    *,
    precalibration_artifact: Any | None = None,
    preregistration: Mapping[str, Any] | None = None,
    preregistration_valid: bool = False,
    raw_history_manifest: Any | None = None,
    reconstructed_chain_manifest: Any | None = None,
    tardis_spread_calibration_manifest: Any | None = None,
    breakeven_report: Any | None = None,
    walk_forward_report: Any | None = None,
    stress_result: Any | None = None,
    cost_budget_status: Any | None = None,
    cost_budget_report: Any | None = None,
    scenario_id: str = DEFAULT_SCENARIO_ID,
    lineage_hash_overrides: Mapping[str, str] | None = None,
    free_lineage_overrides: Mapping[str, Any] | None = None,
    free_lineage_drop_fields: Sequence[str] = (),
) -> VrpFreeFinalVerdict:
    """Map Phase-5 free-data evidence to the capped final verdict vocabulary.

    Missing upstream artifacts fail closed to ``INCONCLUSIVE`` unless observed economics,
    cost budget, or stress evidence already force ``NO_GO``. A clean positive chain is
    capped by ``max_free_verdict_for_valid_reconstructed_evidence`` and never becomes a
    capital-authorizing GO.
    """

    walk_payload = _as_mapping(walk_forward_report)
    stress_artifact = stress_result if stress_result is not None else walk_payload.get("stress")
    stress_payload = _as_mapping(stress_artifact)
    cost_artifact = _cost_budget_artifact(cost_budget_status, cost_budget_report, walk_payload)
    cost_status = _cost_budget_status(cost_budget_status, cost_budget_report, walk_payload)
    chain = build_vrp_free_lineage_chain(
        precalibration_artifact=precalibration_artifact,
        preregistration=preregistration,
        raw_history_manifest=raw_history_manifest,
        reconstructed_chain_manifest=reconstructed_chain_manifest,
        tardis_spread_calibration_manifest=tardis_spread_calibration_manifest,
        breakeven_report=breakeven_report,
        walk_forward_report=walk_forward_report,
        stress_report=stress_artifact,
        cost_budget=cost_artifact,
        hash_overrides=lineage_hash_overrides,
    )

    facts = _decision_facts(
        preregistration_valid=preregistration_valid,
        lineage_chain=chain,
        precalibration_artifact=precalibration_artifact,
        raw_history_manifest=raw_history_manifest,
        reconstructed_chain_manifest=reconstructed_chain_manifest,
        tardis_spread_calibration_manifest=tardis_spread_calibration_manifest,
        breakeven_report=breakeven_report,
        walk_forward_report=walk_forward_report,
        stress_payload=stress_payload,
        cost_status=cost_status,
        cost_budget_payload=cost_artifact,
    )
    verdict = _candidate_verdict(facts)
    free_lineage = _free_final_lineage(
        verdict=verdict,
        overrides=free_lineage_overrides,
        drop_fields=free_lineage_drop_fields,
    )
    lineage_check = validate_free_lineage_payload(free_lineage)

    reasons = list(facts.reasons)
    if verdict == FREE_FINAL_VERDICT_PROMISING:
        reasons.append(REASON_PROMISING_PENDING_REAL_SPREAD)
    if not lineage_check.valid:
        reasons.append(REASON_FREE_LINEAGE_INVALID)
        reasons.extend(lineage_check.mismatches)
        if verdict != FREE_FINAL_VERDICT_NO_GO:
            verdict = FREE_FINAL_VERDICT_INCONCLUSIVE

    # The override-merged payload above is only used to DETECT attacks via
    # validate_free_lineage_payload. The serialized free_lineage must be authoritative
    # (final decided verdict + frozen bridge) and must never re-serialize a rejected
    # 'GO' token, so rebuild it without caller overrides and scrub any forbidden GO.
    serialized_free_lineage = _scrub_forbidden_go(
        _free_final_lineage(verdict=verdict, overrides=None, drop_fields=())
    )

    inputs = VrpFreeFinalVerdictInputs(
        preregistration_valid=facts.preregistration_valid,
        lineage_complete=facts.lineage_complete,
        precalibration_present=facts.precalibration_present,
        raw_history_manifest_present=facts.raw_history_manifest_present,
        reconstructed_chain_manifest_present=facts.reconstructed_chain_manifest_present,
        tardis_spread_calibration_manifest_present=(
            facts.tardis_spread_calibration_manifest_present
        ),
        breakeven_report_present=facts.breakeven_report_present,
        walk_forward_report_present=facts.walk_forward_report_present,
        stress_report_present=facts.stress_report_present,
        cost_budget_present=facts.cost_budget_present,
        upstream_lineage_valid=facts.upstream_lineage_valid,
        economics_positive=facts.economics_positive,
        economic_failure=facts.economic_failure,
        cost_budget_status=facts.cost_budget_status,
        stress_complete=facts.stress_complete,
        stress_max_loss_ok=facts.stress_max_loss_ok,
        free_lineage_valid=lineage_check.valid,
    )
    run_status = "valid" if preregistration_valid and facts.upstream_lineage_valid else "invalid"
    if not lineage_check.valid:
        run_status = "invalid"

    return VrpFreeFinalVerdict(
        schema_version=VRP_FREE_FINAL_VERDICT_SCHEMA_VERSION,
        scenario_id=scenario_id,
        run_status=run_status,
        verdict=verdict,
        allowed_verdicts=ALLOWED_FREE_FINAL_VERDICTS,
        reason_codes=tuple(dict.fromkeys(reasons)),
        inputs=inputs,
        lineage_chain=chain,
        free_lineage=serialized_free_lineage,
        lineage_valid=lineage_check.valid,
        lineage_mismatches=lineage_check.mismatches,
        authorizing=False,
        capital_go_allowed=False,
        non_authorizing_reason=PLAN_SOURCE_QUALITY_BRIDGE["non_authorizing_reason"],
        free_source_quality=PLAN_SOURCE_QUALITY_BRIDGE["free_source_quality"],
        spread_source_quality=PLAN_SOURCE_QUALITY_BRIDGE["spread_source_quality"],
        characterization=CAPITAL_GO_IMPOSSIBLE_CHARACTERIZATION,
        promising_confirmation_trigger=PLAN_PROMISING_CONFIRMATION_TRIGGER,
    )


def _candidate_verdict(facts: _DecisionFacts) -> str:
    if facts.economic_failure or facts.cost_budget_fail or facts.stress_breach:
        return FREE_FINAL_VERDICT_NO_GO
    if (
        not facts.preregistration_valid
        or not facts.lineage_complete
        or not facts.upstream_lineage_valid
        or facts.cost_budget_inconclusive
        or not facts.stress_complete
        or not facts.economics_positive
    ):
        return FREE_FINAL_VERDICT_INCONCLUSIVE
    return max_free_verdict_for_valid_reconstructed_evidence(
        economics_pass=True,
        inconclusive=False,
    )


def _decision_facts(
    *,
    preregistration_valid: bool,
    lineage_chain: Mapping[str, Any],
    precalibration_artifact: Any | None,
    raw_history_manifest: Any | None,
    reconstructed_chain_manifest: Any | None,
    tardis_spread_calibration_manifest: Any | None,
    breakeven_report: Any | None,
    walk_forward_report: Any | None,
    stress_payload: Mapping[str, Any],
    cost_status: str | None,
    cost_budget_payload: Any | None,
) -> _DecisionFacts:
    present = lineage_chain.get("present", {})
    present_map = present if isinstance(present, Mapping) else {}
    precalibration_present = bool(present_map.get("precalibration_artifact")) and _artifact_present(
        precalibration_artifact
    )
    raw_present = bool(present_map.get("raw_history_manifest")) and _artifact_present(
        raw_history_manifest
    )
    reconstructed_present = bool(
        present_map.get("reconstructed_chain_manifest")
    ) and _artifact_present(reconstructed_chain_manifest)
    calibration_present = bool(
        present_map.get("tardis_spread_calibration_manifest")
    ) and _artifact_present(tardis_spread_calibration_manifest)
    breakeven_present = bool(present_map.get("breakeven_report")) and _artifact_present(
        breakeven_report
    )
    walk_present = bool(present_map.get("walk_forward_report")) and _artifact_present(
        walk_forward_report
    )
    stress_present = bool(present_map.get("stress_report")) and _artifact_present(stress_payload)
    cost_present = bool(present_map.get("cost_budget")) and _artifact_present(cost_budget_payload)

    breakeven_payload = _as_mapping(breakeven_report)
    walk_payload = _as_mapping(walk_forward_report)
    upstream_lineage_valid = _upstream_lineage_valid(
        breakeven_payload, walk_payload, stress_payload
    )
    economics_positive = _economics_positive(breakeven_payload, walk_payload)
    economic_failure = _economic_failure(breakeven_payload, walk_payload)
    cost_budget_fail = cost_status == VrpFreeCostBudgetStatus.FAIL_BUDGET.value
    cost_budget_inconclusive = (
        cost_status is None or cost_status == VrpFreeCostBudgetStatus.INCONCLUSIVE.value
    )
    stress_max_loss_ok = _stress_max_loss_ok(stress_payload)
    stress_complete = stress_present and _stress_complete(stress_payload)
    stress_breach = stress_present and _stress_ran(stress_payload) and not stress_max_loss_ok

    reasons: list[str] = []
    if not preregistration_valid:
        reasons.append(REASON_PREREGISTRATION_INVALID)
    if not precalibration_present:
        reasons.append(REASON_PRECALIBRATION_MISSING)
    if not raw_present:
        reasons.append(REASON_RAW_HISTORY_MANIFEST_MISSING)
    if not reconstructed_present:
        reasons.append(REASON_RECONSTRUCTED_CHAIN_MANIFEST_MISSING)
    if not calibration_present:
        reasons.append(REASON_TARDIS_CALIBRATION_MANIFEST_MISSING)
    if not breakeven_present:
        reasons.append(REASON_BREAKEVEN_REPORT_MISSING)
    if not walk_present:
        reasons.append(REASON_WALK_FORWARD_REPORT_MISSING)
    if not stress_present:
        reasons.append(REASON_STRESS_MISSING)
    elif not stress_complete and not stress_breach:
        reasons.append(REASON_STRESS_INCOMPLETE)
    if not cost_present:
        reasons.append(REASON_COST_BUDGET_MISSING)
    elif cost_budget_fail:
        reasons.append(REASON_COST_BUDGET_FAIL)
    elif cost_budget_inconclusive:
        reasons.append(REASON_COST_BUDGET_INCONCLUSIVE)
    if stress_breach:
        reasons.append(REASON_STRESS_MAX_LOSS_BREACH)
    if economic_failure:
        reasons.append(REASON_ECONOMIC_FAILURE)
    elif walk_present and not economics_positive:
        reasons.append(REASON_ECONOMICS_NOT_POSITIVE)
    if not upstream_lineage_valid:
        reasons.append(REASON_UPSTREAM_LINEAGE_INVALID)

    return _DecisionFacts(
        reasons=tuple(reasons),
        preregistration_valid=preregistration_valid,
        lineage_complete=bool(lineage_chain.get("complete")),
        precalibration_present=precalibration_present,
        raw_history_manifest_present=raw_present,
        reconstructed_chain_manifest_present=reconstructed_present,
        tardis_spread_calibration_manifest_present=calibration_present,
        breakeven_report_present=breakeven_present,
        walk_forward_report_present=walk_present,
        stress_report_present=stress_present,
        cost_budget_present=cost_present,
        upstream_lineage_valid=upstream_lineage_valid,
        economics_positive=economics_positive,
        economic_failure=economic_failure,
        cost_budget_status=cost_status,
        cost_budget_fail=cost_budget_fail,
        cost_budget_inconclusive=cost_budget_inconclusive,
        stress_complete=stress_complete,
        stress_max_loss_ok=stress_max_loss_ok,
        stress_breach=stress_breach,
    )


def _free_final_lineage(
    *,
    verdict: str,
    overrides: Mapping[str, Any] | None,
    drop_fields: Sequence[str],
) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "verdict": verdict,
        "outcome": verdict,
        "source_quality": SourceQuality.FIXTURE.value,
        "free_source_quality": PLAN_SOURCE_QUALITY_BRIDGE["free_source_quality"],
        "spread_source_quality": PLAN_SOURCE_QUALITY_BRIDGE["spread_source_quality"],
        "authorizing": False,
        "capital_go_allowed": False,
        "non_authorizing_reason": PLAN_SOURCE_QUALITY_BRIDGE["non_authorizing_reason"],
        "uses_committed_authorizing_verdict": False,
    }
    if overrides:
        payload.update(dict(overrides))
    for field in drop_fields:
        payload.pop(str(field), None)
    return payload


def _cost_budget_artifact(
    status_input: Any | None,
    report_input: Any | None,
    walk_payload: Mapping[str, Any],
) -> Any | None:
    if report_input is not None:
        return report_input
    if (
        isinstance(status_input, Mapping)
        or hasattr(status_input, "as_dict")
        or is_dataclass(status_input)
    ):
        return status_input
    if status_input is not None:
        return {"status": _status_value(status_input)}
    if "cost_budget" in walk_payload:
        return walk_payload["cost_budget"]
    if "cost_budget_status" in walk_payload:
        return {"status": _status_value(walk_payload["cost_budget_status"])}
    return None


def _cost_budget_status(
    status_input: Any | None,
    report_input: Any | None,
    walk_payload: Mapping[str, Any],
) -> str | None:
    for candidate in (
        status_input,
        report_input,
        walk_payload.get("cost_budget_status"),
        walk_payload.get("cost_budget"),
    ):
        if candidate is None:
            continue
        mapping = _as_mapping(candidate)
        if mapping:
            for key in ("status", "cost_budget_status"):
                if key in mapping:
                    return _status_value(mapping[key])
        return _status_value(candidate)
    return None


def _upstream_lineage_valid(*reports: Mapping[str, Any]) -> bool:
    for payload in reports:
        if not payload:
            continue
        if payload.get("lineage_valid") is False:
            return False
        if payload.get("uses_committed_authorizing_verdict") is True:
            return False
        if payload.get("authorizing") is True or payload.get("capital_go_allowed") is True:
            return False
        if _top_level_verdict(payload) == "GO" or _contains_forbidden_go(
            payload.get("free_lineage")
        ):
            return False
        if _contains_venue_value(payload.get("free_lineage")):
            return False
    return True


def _economics_positive(breakeven: Mapping[str, Any], walk: Mapping[str, Any]) -> bool:
    if _top_level_verdict(walk) == FREE_FINAL_VERDICT_PROMISING:
        return True
    if walk.get("economics_pass") is True or walk.get("committed_clean_heldout_positive") is True:
        return not _economic_failure(breakeven, walk)
    return False


def _economic_failure(breakeven: Mapping[str, Any], walk: Mapping[str, Any]) -> bool:
    for payload in (breakeven, walk):
        if payload.get("economic_failure") is True or payload.get("economics_pass") is False:
            return True
        if _top_level_verdict(payload) == FREE_FINAL_VERDICT_NO_GO:
            return True
    return False


def _top_level_verdict(payload: Mapping[str, Any]) -> str | None:
    for key in ("verdict", "outcome", "branch_decision"):
        if key in payload:
            return _status_value(payload[key])
    return None


def _stress_complete(payload: Mapping[str, Any]) -> bool:
    return _stress_ran(payload) and _stress_max_loss_ok(payload)


def _stress_ran(payload: Mapping[str, Any]) -> bool:
    return (
        payload.get("ran") is True
        or _status_value(payload.get("status")) == VrpFreeStressStatus.RAN.value
    )


def _stress_max_loss_ok(payload: Mapping[str, Any]) -> bool:
    return payload.get("max_loss_ok") is True or payload.get("max_loss_invariant_ok") is True


def _as_mapping(value: Any) -> dict[str, Any]:
    normalised = _normalise_json(value)
    return dict(normalised) if isinstance(normalised, Mapping) else {}


def _artifact_present(value: Any) -> bool:
    if value is None:
        return False
    if isinstance(value, str):
        return value not in {"", MISSING_SHA256, "missing"}
    payload = _as_mapping(value)
    if payload:
        if payload.get("missing") is True:
            return False
        if payload.get("run_status") == "missing" or payload.get("status") == "missing":
            return False
    return True


def _artifact_sha256(value: Any) -> str:
    if not _artifact_present(value):
        return MISSING_SHA256
    return _canonical_sha256(_normalise_json(value))


def _canonical_sha256(value: Any) -> str:
    return hashlib.sha256(
        json.dumps(_normalise_json(value), sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()


def _normalise_json(value: Any) -> Any:
    if hasattr(value, "as_dict"):
        return _normalise_json(value.as_dict())
    if is_dataclass(value) and not isinstance(value, type):
        return _normalise_json(asdict(value))
    if isinstance(value, Mapping):
        return {
            str(key): _normalise_json(item)
            for key, item in sorted(value.items(), key=lambda kv: str(kv[0]))
        }
    if isinstance(value, (list, tuple)):
        return [_normalise_json(item) for item in value]
    if isinstance(value, set):
        return sorted(_normalise_json(item) for item in value)
    if isinstance(value, Enum):
        return _normalise_json(value.value)
    if isinstance(value, bytes):
        return value.decode("utf-8")
    return value


def _status_value(value: Any) -> str | None:
    normalised = _normalise_json(value)
    if normalised is None:
        return None
    return str(normalised)


def _contains_forbidden_go(value: Any) -> bool:
    normalised = _normalise_json(value)
    if isinstance(normalised, Mapping):
        return any(_contains_forbidden_go(item) for item in normalised.values())
    if isinstance(normalised, list):
        return any(_contains_forbidden_go(item) for item in normalised)
    return normalised == "GO"

def _scrub_forbidden_go(value: Any) -> Any:
    """Replace any forbidden ``GO`` scalar with a safe sentinel for serialization.

    The free final-verdict vocabulary excludes GO; a rejected malicious payload must
    never re-serialize a raw ``GO`` token into the emitted free report.
    """
    normalised = _normalise_json(value)
    if isinstance(normalised, Mapping):
        return {key: _scrub_forbidden_go(item) for key, item in normalised.items()}
    if isinstance(normalised, list):
        return [_scrub_forbidden_go(item) for item in normalised]
    if normalised == "GO":
        return "REJECTED_NON_FREE_VERDICT"
    return normalised


def _contains_venue_value(value: Any) -> bool:
    normalised = _normalise_json(value)
    venue_values = {SourceQuality.VENUE.value, SourceQuality.VENUE.name, "SourceQuality.VENUE"}
    if isinstance(normalised, Mapping):
        return any(_contains_venue_value(item) for item in normalised.values())
    if isinstance(normalised, list):
        return any(_contains_venue_value(item) for item in normalised)
    return normalised in venue_values


__all__ = [
    "ALLOWED_FREE_FINAL_VERDICTS",
    "CAPITAL_GO_IMPOSSIBLE_CHARACTERIZATION",
    "FREE_FINAL_VERDICT_INCONCLUSIVE",
    "FREE_FINAL_VERDICT_NO_GO",
    "FREE_FINAL_VERDICT_PROMISING",
    "MISSING_SHA256",
    "REASON_COST_BUDGET_FAIL",
    "REASON_ECONOMIC_FAILURE",
    "REASON_FREE_LINEAGE_INVALID",
    "REASON_PROMISING_PENDING_REAL_SPREAD",
    "REASON_STRESS_MAX_LOSS_BREACH",
    "VrpFreeFinalVerdict",
    "VrpFreeFinalVerdictInputs",
    "VrpFreeFinalVerdictValue",
    "VRP_FREE_FINAL_VERDICT_SCHEMA_VERSION",
    "build_vrp_free_lineage_chain",
    "decide_vrp_free_final_verdict",
]

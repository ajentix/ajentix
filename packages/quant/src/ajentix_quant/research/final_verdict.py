"""Strategy-v2 final-verdict aggregation (G004).

Pure, deterministic chaining of the pre-registration, breakeven, walk-forward, and
pivot-feasibility evidence into a single auditable GO / NO_GO / INCONCLUSIVE /
PIVOT_CANDIDATE_CLEARED verdict. No network, no filesystem, no clock, no randomness.

The ADR-0002 promotion gate is intentionally strict: the hard performance gate is
promoted ONLY when there is a clean, pre-registered held-out GO with valid lineage,
no fold collapse, no concentration failure, and no maker-only dependence.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

FINAL_VERDICT_SCHEMA_VERSION = "stratv2-final-verdict-v1"

VERDICT_GO = "GO"
VERDICT_NO_GO = "NO_GO"
VERDICT_INCONCLUSIVE = "INCONCLUSIVE"
VERDICT_PIVOT_CANDIDATE_CLEARED = "PIVOT_CANDIDATE_CLEARED"

# verdict reason codes
REASON_PREREGISTRATION_INVALID = "PREREGISTRATION_INVALID"
REASON_INVALID_LINEAGE = "INVALID_PREREGISTRATION_LINEAGE"
REASON_CLEAN_HELDOUT_GO = "CLEAN_HELDOUT_GO"
REASON_A1_GO_PRESENT = "A1_GO_PRESENT"
REASON_A1_NO_GO_ALL_SYMBOLS = "A1_NO_GO_ALL_SYMBOLS"
REASON_A1_MIXED_NO_HELDOUT_GO = "A1_DECISIONS_PRESENT_BUT_NO_HELDOUT_GO"
REASON_NO_WALK_FORWARD_HELDOUT_RUN = "NO_WALK_FORWARD_HELDOUT_RUN"
REASON_A2_PIVOT_CANDIDATE_CLEARS = "A2_PIVOT_CANDIDATE_CLEARS"
REASON_NO_A2_PIVOT_CANDIDATE_CLEARS = "NO_A2_PIVOT_CANDIDATE_CLEARS"

# ADR-0002 promotion-gate reason codes (why NOT promoted)
ADR_REASON_NO_CLEAN_HELDOUT_GO = "NO_CLEAN_HELDOUT_GO"
ADR_REASON_INVALID_LINEAGE = "INVALID_PREREGISTRATION_LINEAGE"
ADR_REASON_FOLD_COLLAPSE = "FOLD_COLLAPSE_PRESENT"
ADR_REASON_CONCENTRATION_FAILURE = "CONCENTRATION_FAILURE_PRESENT"
ADR_REASON_MAKER_ONLY_DEPENDENCE = "MAKER_ONLY_DEPENDENCE"


@dataclass(frozen=True)
class VerdictInputs:
    """Primitive facts extracted from the chained evidence artifacts."""

    preregistration_valid: bool
    lineage_consistent: bool
    a1_go_symbols: tuple[str, ...]
    a1_no_go_symbols: tuple[str, ...]
    a1_inconclusive_symbols: tuple[str, ...]
    walk_forward_ran: bool
    held_out_clean_go: bool
    pivot_ran: bool
    pivot_any_clears: bool
    fold_collapse: bool
    concentration_failure: bool
    maker_only_dependence: bool


def decide_final_verdict(inp: VerdictInputs) -> tuple[str, list[str]]:
    """Map the primitive facts to a single verdict + ordered reason codes."""

    if not inp.preregistration_valid:
        return VERDICT_INCONCLUSIVE, [REASON_PREREGISTRATION_INVALID]
    if not inp.lineage_consistent:
        return VERDICT_INCONCLUSIVE, [REASON_INVALID_LINEAGE]

    if inp.held_out_clean_go and inp.a1_go_symbols:
        return VERDICT_GO, [REASON_CLEAN_HELDOUT_GO, REASON_A1_GO_PRESENT]

    if inp.pivot_ran and inp.pivot_any_clears:
        return VERDICT_PIVOT_CANDIDATE_CLEARED, [REASON_A2_PIVOT_CANDIDATE_CLEARS]

    reasons: list[str] = []
    if inp.a1_no_go_symbols and not inp.a1_go_symbols:
        reasons.append(REASON_A1_NO_GO_ALL_SYMBOLS)
    elif inp.a1_go_symbols and not inp.held_out_clean_go:
        reasons.append(REASON_A1_MIXED_NO_HELDOUT_GO)
    if not inp.walk_forward_ran:
        reasons.append(REASON_NO_WALK_FORWARD_HELDOUT_RUN)
    if inp.pivot_ran and not inp.pivot_any_clears:
        reasons.append(REASON_NO_A2_PIVOT_CANDIDATE_CLEARS)
    return VERDICT_NO_GO, reasons


def should_promote_adr_0002(verdict: str, inp: VerdictInputs) -> tuple[bool, list[str]]:
    """Strict gate: promote ADR-0002 ONLY on a clean, pre-registered held-out GO."""

    reasons: list[str] = []
    if verdict != VERDICT_GO or not inp.held_out_clean_go or not inp.a1_go_symbols:
        reasons.append(ADR_REASON_NO_CLEAN_HELDOUT_GO)
    if not inp.preregistration_valid or not inp.lineage_consistent:
        reasons.append(ADR_REASON_INVALID_LINEAGE)
    if inp.fold_collapse:
        reasons.append(ADR_REASON_FOLD_COLLAPSE)
    if inp.concentration_failure:
        reasons.append(ADR_REASON_CONCENTRATION_FAILURE)
    if inp.maker_only_dependence:
        reasons.append(ADR_REASON_MAKER_ONLY_DEPENDENCE)
    return (not reasons, reasons)


def summarize_breakeven(reports: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    """Extract per-symbol A1 decisions and branch routing from breakeven reports.

    Every per-symbol breakeven report embeds the same shared ``branch_summary`` over all
    symbols, so the first report is authoritative for the routing picture.
    """

    if not reports:
        return {
            "ran": False,
            "a1_go_symbols": [],
            "a1_no_go_symbols": [],
            "a1_inconclusive_symbols": [],
            "branch_by_symbol": {},
            "reason_codes_by_symbol": {},
        }

    branch = dict(reports[0].get("branch_summary", {}))
    by_symbol: Mapping[str, Any] = branch.get("by_symbol", {})
    a1_go: list[str] = []
    a1_no_go: list[str] = []
    a1_inconclusive: list[str] = []
    branch_by_symbol: dict[str, Any] = {}
    reason_codes_by_symbol: dict[str, list[str]] = {}
    for symbol in sorted(by_symbol):
        row = by_symbol[symbol]
        decision = row.get("a1_decision")
        if decision == "GO":
            a1_go.append(symbol)
        elif decision == "NO_GO":
            a1_no_go.append(symbol)
        else:
            a1_inconclusive.append(symbol)
        branch_by_symbol[symbol] = row.get("branch_decision")
        reason_codes_by_symbol[symbol] = list(row.get("reason_codes", []))

    return {
        "ran": True,
        "a1_go_symbols": a1_go,
        "a1_no_go_symbols": a1_no_go,
        "a1_inconclusive_symbols": a1_inconclusive,
        "branch_by_symbol": branch_by_symbol,
        "reason_codes_by_symbol": reason_codes_by_symbol,
        "funding_rows_insample_by_scenario": {
            str(r.get("scenario_id")): r.get("breakeven", {}).get("funding_rows_insample")
            for r in reports
        },
    }


def summarize_pivot(report: Mapping[str, Any] | None) -> dict[str, Any]:
    """Extract A2 pivot-feasibility outcome + per-candidate concentration metrics."""

    if report is None:
        return {"ran": False, "any_candidate_clears": False, "candidates": []}

    overall = report.get("overall", {})
    candidates: list[dict[str, Any]] = []
    for cand in report.get("candidates", []):
        candidates.append(
            {
                "candidate_id": cand.get("candidate_id"),
                "candidate_type": cand.get("candidate_type"),
                "symbol": cand.get("symbol"),
                "venue": cand.get("venue"),
                "clears": bool(cand.get("clears", False)),
                "qualifying_24h_window_pct": cand.get("qualifying_24h_window_pct"),
                "qualifying_24h_window_count": cand.get("qualifying_24h_window_count"),
                "cluster_count": cand.get("cluster_count"),
                "max_single_week_share": cand.get("max_single_week_share"),
                "primary_slippage_bps_per_leg": cand.get("primary_slippage_bps_per_leg"),
                "reason_codes": list(cand.get("reason_codes", [])),
            }
        )
    return {
        "ran": True,
        "any_candidate_clears": bool(overall.get("any_candidate_clears", False)),
        "clearing_candidate_ids": list(overall.get("clearing_candidate_ids", [])),
        "conclusion": overall.get("conclusion"),
        "candidates": candidates,
    }


def build_verdict_inputs(
    *,
    preregistration_valid: bool,
    lineage_consistent: bool,
    breakeven_summary: Mapping[str, Any],
    walk_forward: Mapping[str, Any] | None,
    pivot_summary: Mapping[str, Any],
    maker_only_dependence: bool = False,
) -> VerdictInputs:
    """Assemble :class:`VerdictInputs` from chained-evidence summaries."""

    wf = dict(walk_forward or {})
    walk_forward_ran = bool(wf.get("ran", False))
    held_out_clean_go = bool(wf.get("clean_heldout_go", False))
    # Only an explicit True is a real failure: a "not_applicable" sentinel (no walk-forward
    # ran, so no fold could collapse) must NOT register as a fold-collapse / concentration block.
    fold_collapse = wf.get("fold_collapse") is True
    concentration_failure = wf.get("concentration_failure") is True

    return VerdictInputs(
        preregistration_valid=preregistration_valid,
        lineage_consistent=lineage_consistent,
        a1_go_symbols=tuple(breakeven_summary.get("a1_go_symbols", [])),
        a1_no_go_symbols=tuple(breakeven_summary.get("a1_no_go_symbols", [])),
        a1_inconclusive_symbols=tuple(breakeven_summary.get("a1_inconclusive_symbols", [])),
        walk_forward_ran=walk_forward_ran,
        held_out_clean_go=held_out_clean_go,
        pivot_ran=bool(pivot_summary.get("ran", False)),
        pivot_any_clears=bool(pivot_summary.get("any_candidate_clears", False)),
        fold_collapse=fold_collapse,
        concentration_failure=concentration_failure,
        maker_only_dependence=maker_only_dependence,
    )

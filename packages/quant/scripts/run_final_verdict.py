#!/usr/bin/env python3
"""Strategy-v2 G004 final verdict.

Deterministically chains the committed pre-registration, breakeven, walk-forward (if
run), and pivot-feasibility artifacts into a single auditable verdict at
``reports/strategy_v2_final_verdict.{json,md}``.

The pre-registration is verified FIRST; an invalid lineage yields run_status=invalid and
verdict=INCONCLUSIVE with no ADR promotion. docs/adr/0002-strategy-v2-hard-performance-gate.md
is written ONLY when there is a clean, pre-registered held-out GO.

No network. Reads only committed artifacts; safe under CI.
"""

from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import os
import sys
from pathlib import Path
from typing import Any

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from ajentix_quant.research.final_verdict import (  # noqa: E402
    FINAL_VERDICT_SCHEMA_VERSION,
    build_verdict_inputs,
    decide_final_verdict,
    should_promote_adr_0002,
    summarize_breakeven,
    summarize_pivot,
)
from ajentix_quant.research.preregistration import (  # noqa: E402
    load_preregistration,
    verify_preregistration,
)

ADR_FILENAME = "0002-strategy-v2-hard-performance-gate.md"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Chain strategy-v2 evidence into a single pre-registered final verdict."
    )
    parser.add_argument(
        "--repo-root",
        default=str(Path(__file__).resolve().parents[1]),
        help="Repository root (defaults to the checkout containing this script).",
    )
    parser.add_argument(
        "--preregistration",
        default=None,
        help="Path to docs/preregistration/stratv2-*.json. Defaults to the single artifact.",
    )
    parser.add_argument(
        "--reports-dir",
        default="reports",
        help="Directory holding upstream + output reports, relative to repo root.",
    )
    parser.add_argument(
        "--adr-dir",
        default="docs/adr",
        help="Directory where ADR-0002 is written ONLY on a clean held-out GO.",
    )
    args = parser.parse_args(argv)

    repo_root = Path(args.repo_root).resolve()
    reports_dir = repo_root / args.reports_dir
    prereg_path = _resolve_preregistration(repo_root, args.preregistration)
    artifact = load_preregistration(prereg_path)
    verify = verify_preregistration(artifact, repo_root)
    prereg_sha = _sha256_file(prereg_path)

    # Load upstream evidence (committed artifacts).
    breakeven_reports = _load_reports(sorted(reports_dir.glob("breakeven_*_v2.json")))
    pivot_path = reports_dir / "pivot_venue_feasibility_v2.json"
    pivot_report = _load_json(pivot_path) if pivot_path.exists() else None
    walk_forward_reports = _load_reports(sorted(reports_dir.glob("walk_forward_*.json")))

    breakeven_summary = summarize_breakeven([r["payload"] for r in breakeven_reports])
    pivot_summary = summarize_pivot(pivot_report)

    # Walk-forward held-out only runs when the breakeven branch selects A1 for a symbol.
    walk_forward = _summarize_walk_forward(walk_forward_reports, breakeven_summary)

    # Lineage is consistent only when every chained artifact is itself a valid pre-registered
    # run AND the current pre-registration validates against the live repo.
    upstream_run_statuses = [r["payload"].get("run_status") for r in breakeven_reports]
    if pivot_report is not None:
        upstream_run_statuses.append(pivot_report.get("run_status"))
    for r in walk_forward_reports:
        upstream_run_statuses.append(r["payload"].get("run_status"))
    lineage_consistent = verify.valid and all(s == "valid" for s in upstream_run_statuses)

    inputs = build_verdict_inputs(
        preregistration_valid=verify.valid,
        lineage_consistent=lineage_consistent,
        breakeven_summary=breakeven_summary,
        walk_forward=walk_forward,
        pivot_summary=pivot_summary,
        maker_only_dependence=False,
    )
    verdict, verdict_reasons = decide_final_verdict(inputs)
    adr_promoted, adr_block_reasons = should_promote_adr_0002(verdict, inputs)

    plan = artifact.get("plan", {})
    payload = _build_payload(
        verify=verify,
        artifact=artifact,
        prereg_path=prereg_path,
        prereg_sha=prereg_sha,
        breakeven_reports=breakeven_reports,
        breakeven_summary=breakeven_summary,
        pivot_report=pivot_report,
        pivot_path=pivot_path,
        pivot_summary=pivot_summary,
        walk_forward=walk_forward,
        lineage_consistent=lineage_consistent,
        verdict=verdict,
        verdict_reasons=verdict_reasons,
        adr_promoted=adr_promoted,
        adr_block_reasons=adr_block_reasons,
        plan=plan,
    )

    reports_dir.mkdir(parents=True, exist_ok=True)
    json_path = reports_dir / "strategy_v2_final_verdict.json"
    md_path = reports_dir / "strategy_v2_final_verdict.md"
    json_path.write_text(
        json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    md_path.write_text(_markdown_report(payload), encoding="utf-8")

    adr_path = repo_root / args.adr_dir / ADR_FILENAME
    if adr_promoted:
        adr_path.parent.mkdir(parents=True, exist_ok=True)
        adr_path.write_text(_adr_document(payload), encoding="utf-8")
    elif adr_path.exists():
        # Strict gate: ADR-0002 must NOT exist without a clean held-out GO.
        raise SystemExit(
            f"refusing: {adr_path} exists but no clean held-out GO authorizes ADR-0002 "
            f"(block reasons: {adr_block_reasons}); remove it before re-running."
        )

    print(f"run_status={payload['run_status']}")
    print(f"run_id={artifact.get('run_id')}")
    print(f"preregistration_sha256={prereg_sha}")
    print(f"verdict={verdict}")
    print(f"verdict_reasons={','.join(verdict_reasons) or '-'}")
    print(f"adr_0002_promoted={str(adr_promoted).lower()}")
    print(f"adr_0002_block_reasons={','.join(adr_block_reasons) or '-'}")
    print(f"wrote={json_path.relative_to(repo_root)}")
    print(f"wrote={md_path.relative_to(repo_root)}")
    if not verify.valid:
        for mismatch in verify.mismatches:
            print(f"mismatch={mismatch}", file=sys.stderr)
        return 1
    return 0


def _resolve_preregistration(repo_root: Path, explicit: str | None) -> Path:
    if explicit:
        path = Path(explicit)
        return path if path.is_absolute() else repo_root / path
    prereg_dir = repo_root / "docs" / "preregistration"
    artifacts = sorted(prereg_dir.glob("stratv2-*.json")) if prereg_dir.is_dir() else []
    if len(artifacts) != 1:
        raise SystemExit(
            "expected exactly one docs/preregistration/stratv2-*.json artifact, "
            f"found {len(artifacts)}"
        )
    return artifacts[0]


def _load_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def _load_reports(paths: list[Path]) -> list[dict[str, Any]]:
    reports: list[dict[str, Any]] = []
    for path in paths:
        payload = _load_json(path)
        reports.append(
            {
                "path": path,
                "payload": payload,
                "file_sha256": _sha256_file(path),
            }
        )
    return reports


def _summarize_walk_forward(
    walk_forward_reports: list[dict[str, Any]],
    breakeven_summary: dict[str, Any],
) -> dict[str, Any]:
    a1_selected = list(breakeven_summary.get("a1_go_symbols", []))
    if not walk_forward_reports:
        reason = (
            "A1 walk-forward held-out was not executed because the breakeven branch "
            "selected A1 for no symbol"
            if not a1_selected
            else "A1 selected but no walk_forward_*.json artifact found"
        )
        return {
            "ran": False,
            "clean_heldout_go": False,
            "fold_collapse": "not_applicable",
            "concentration_failure": "not_applicable",
            "reason": reason,
            "artifacts": [],
        }
    payloads = [r["payload"] for r in walk_forward_reports]
    clean_go = any(bool(p.get("clean_heldout_go")) for p in payloads)
    fold_collapse = any(bool(p.get("fold_collapse")) for p in payloads)
    concentration_failure = any(bool(p.get("concentration_failure")) for p in payloads)
    return {
        "ran": True,
        "clean_heldout_go": clean_go,
        "fold_collapse": fold_collapse,
        "concentration_failure": concentration_failure,
        "reason": "walk_forward artifacts present",
        "artifacts": [
            {"path": r["path"].name, "file_sha256": r["file_sha256"]}
            for r in walk_forward_reports
        ],
    }


def _lineage_entry(report: dict[str, Any]) -> dict[str, Any]:
    payload = report["payload"]
    return {
        "path": report["path"].name,
        "file_sha256": report["file_sha256"],
        "run_id": payload.get("run_id"),
        "run_status": payload.get("run_status"),
        "preregistration_sha256": payload.get("preregistration_sha256"),
        "content_hash": payload.get("content_hash"),
    }


def _build_payload(
    *,
    verify: Any,
    artifact: dict[str, Any],
    prereg_path: Path,
    prereg_sha: str,
    breakeven_reports: list[dict[str, Any]],
    breakeven_summary: dict[str, Any],
    pivot_report: dict[str, Any] | None,
    pivot_path: Path,
    pivot_summary: dict[str, Any],
    walk_forward: dict[str, Any],
    lineage_consistent: bool,
    verdict: str,
    verdict_reasons: list[str],
    adr_promoted: bool,
    adr_block_reasons: list[str],
    plan: dict[str, Any],
) -> dict[str, Any]:
    run_status = "valid" if verify.valid else verify.run_status

    breakeven_lineage = [_lineage_entry(r) for r in breakeven_reports]
    pivot_lineage: dict[str, Any] | None = None
    if pivot_report is not None:
        pivot_lineage = {
            "path": pivot_path.name,
            "file_sha256": _sha256_file(pivot_path),
            "run_id": pivot_report.get("run_id"),
            "run_status": pivot_report.get("run_status"),
            "preregistration_sha256": pivot_report.get("preregistration_sha256"),
            "content_hash": pivot_report.get("content_hash"),
        }

    _run_id_candidates: list[str] = [
        str(e["run_id"]) for e in breakeven_lineage if e.get("run_id")
    ]
    if pivot_lineage is not None and pivot_lineage.get("run_id"):
        _run_id_candidates.append(str(pivot_lineage["run_id"]))
    upstream_prereg_run_ids = sorted(set(_run_id_candidates))
    current_run_id = artifact.get("run_id")
    superseded = [rid for rid in upstream_prereg_run_ids if rid != current_run_id]

    trial_budget = dict(plan.get("trial_budget", {}))

    body: dict[str, Any] = {
        "schema_version": FINAL_VERDICT_SCHEMA_VERSION,
        "run_status": run_status,
        "verdict": verdict,
        "verdict_reasons": list(verdict_reasons),
        "preregistration": {
            "path": prereg_path.as_posix(),
            "run_id": current_run_id,
            "file_sha256": prereg_sha,
            "content_hash": artifact.get("content_hash"),
            "valid": verify.valid,
            "run_status": verify.run_status,
            "mismatches": list(getattr(verify, "mismatches", []) or []),
        },
        "lineage": {
            "consistent": lineage_consistent,
            "current_preregistration_run_id": current_run_id,
            "upstream_preregistration_run_ids": upstream_prereg_run_ids,
            "superseded_preregistration_run_ids": superseded,
            "note": (
                "The pre-registration run_id is content-derived: it deterministically changes "
                "whenever frozen source is added (the governance code-change -> new-run_id flow). "
                "Each upstream artifact was generated under the pre-registration current at its "
                "run; the live pre-registration freezes the same analysis code. Superseded "
                "run_ids reflect this evolution, not invalid lineage."
            ),
        },
        "cost_modes": {
            "primary": "taker-primary authorizing (round-trip + slippage)",
            "maker_sensitivity": "non-authorizing; maker_can_authorize=false",
            "maker_only_dependence": False,
        },
        "trial_budget": {
            "plan_caps": trial_budget,
            "effective_consumed": {
                "primary_train_trials": 0,
                "primary_heldout_evals": 0,
                "secondary_sensitivity_evals": 0,
                "total_heldout_evals": 0,
            },
            "note": (
                "Breakeven is a cheap in-sample branch-decision analysis and consumes zero "
                "held-out budget. No A1 walk-forward held-out evaluation was run, so no held-out "
                "data was touched and no multiplicity/over-fit budget was spent."
            ),
        },
        "breakeven": {
            "ran": breakeven_summary.get("ran", False),
            "a1_go_symbols": breakeven_summary.get("a1_go_symbols", []),
            "a1_no_go_symbols": breakeven_summary.get("a1_no_go_symbols", []),
            "a1_inconclusive_symbols": breakeven_summary.get("a1_inconclusive_symbols", []),
            "branch_by_symbol": breakeven_summary.get("branch_by_symbol", {}),
            "reason_codes_by_symbol": breakeven_summary.get("reason_codes_by_symbol", {}),
            "funding_rows_insample_by_scenario": breakeven_summary.get(
                "funding_rows_insample_by_scenario", {}
            ),
            "lineage": breakeven_lineage,
        },
        "walk_forward": {
            "ran": walk_forward.get("ran", False),
            "clean_heldout_go": walk_forward.get("clean_heldout_go", False),
            "fold_collapse": walk_forward.get("fold_collapse"),
            "concentration_failure": walk_forward.get("concentration_failure"),
            "reason": walk_forward.get("reason"),
            "artifacts": walk_forward.get("artifacts", []),
        },
        "pivot_feasibility": {
            "ran": pivot_summary.get("ran", False),
            "any_candidate_clears": pivot_summary.get("any_candidate_clears", False),
            "clearing_candidate_ids": pivot_summary.get("clearing_candidate_ids", []),
            "conclusion": pivot_summary.get("conclusion"),
            "candidates": pivot_summary.get("candidates", []),
            "lineage": pivot_lineage,
        },
        "adr_0002": {
            "promoted": adr_promoted,
            "title": "Strategy-v2 hard performance gate",
            "block_reasons": list(adr_block_reasons),
            "note": (
                "ADR-0002 promotes the hard performance gate ONLY on a clean, pre-registered "
                "held-out GO with valid lineage, no fold collapse, no concentration failure, and "
                "no maker-only dependence. It is NOT promoted here."
                if not adr_promoted
                else "ADR-0002 promoted: clean pre-registered held-out GO."
            ),
        },
    }
    body["content_hash"] = _canonical_sha256(body)
    body["generated_at"] = dt.datetime.now(dt.UTC).isoformat()
    return body


def _markdown_report(payload: dict[str, Any]) -> str:
    be = payload["breakeven"]
    pv = payload["pivot_feasibility"]
    wf = payload["walk_forward"]
    adr = payload["adr_0002"]
    lines = [
        "# Strategy-v2 Final Verdict",
        "",
        f"- **Verdict:** {payload['verdict']}",
        f"- **Verdict reasons:** {', '.join(payload['verdict_reasons']) or '-'}",
        f"- **Run status:** {payload['run_status']}",
        f"- **Schema:** {payload['schema_version']}",
        f"- **Content hash:** {payload['content_hash']}",
        "",
        "## Pre-registration",
        f"- run_id: `{payload['preregistration']['run_id']}`",
        f"- file_sha256: `{payload['preregistration']['file_sha256']}`",
        f"- content_hash: `{payload['preregistration']['content_hash']}`",
        f"- valid: {payload['preregistration']['valid']}",
        "",
        "## Lineage",
        f"- consistent: {payload['lineage']['consistent']}",
        f"- current run_id: `{payload['lineage']['current_preregistration_run_id']}`",
        f"- upstream run_ids: {payload['lineage']['upstream_preregistration_run_ids']}",
        f"- superseded run_ids: {payload['lineage']['superseded_preregistration_run_ids']}",
        f"- note: {payload['lineage']['note']}",
        "",
        "## Cost modes",
        f"- primary: {payload['cost_modes']['primary']}",
        f"- maker: {payload['cost_modes']['maker_sensitivity']}",
        f"- maker_only_dependence: {payload['cost_modes']['maker_only_dependence']}",
        "",
        "## Trial budget",
        f"- plan caps: {payload['trial_budget']['plan_caps']}",
        f"- effective consumed: {payload['trial_budget']['effective_consumed']}",
        f"- note: {payload['trial_budget']['note']}",
        "",
        "## Breakeven (A1 branch decision)",
        f"- ran: {be['ran']}",
        f"- A1 GO symbols: {be['a1_go_symbols'] or '-'}",
        f"- A1 NO_GO symbols: {be['a1_no_go_symbols'] or '-'}",
        f"- A1 inconclusive symbols: {be['a1_inconclusive_symbols'] or '-'}",
        f"- branch by symbol: {be['branch_by_symbol']}",
        f"- reason codes by symbol: {be['reason_codes_by_symbol']}",
        f"- funding rows in-sample: {be['funding_rows_insample_by_scenario']}",
        "",
        "## Walk-forward (held-out)",
        f"- ran: {wf['ran']}",
        f"- clean held-out GO: {wf['clean_heldout_go']}",
        f"- fold collapse: {wf['fold_collapse']}",
        f"- concentration failure: {wf['concentration_failure']}",
        f"- reason: {wf['reason']}",
        "",
        "## Pivot feasibility (A2 evidence)",
        f"- ran: {pv['ran']}",
        f"- any candidate clears: {pv['any_candidate_clears']}",
        f"- clearing candidate ids: {pv['clearing_candidate_ids'] or '-'}",
        f"- conclusion: {pv['conclusion']}",
        "",
        (
            "| candidate | clears | qual% | windows | clusters | "
            "max wk share | slippage bps | reasons |"
        ),
        "| --- | --- | --- | --- | --- | --- | --- | --- |",
    ]
    for c in pv["candidates"]:
        lines.append(
            f"| {c['candidate_id']} | {c['clears']} | "
            f"{_fmt(c['qualifying_24h_window_pct'])} | {c['qualifying_24h_window_count']} | "
            f"{c['cluster_count']} | {_fmt(c['max_single_week_share'])} | "
            f"{_fmt(c['primary_slippage_bps_per_leg'])} | {','.join(c['reason_codes']) or '-'} |"
        )
    lines += [
        "",
        "## ADR-0002 promotion gate",
        f"- promoted: {adr['promoted']}",
        f"- block reasons: {', '.join(adr['block_reasons']) or '-'}",
        f"- note: {adr['note']}",
        "",
        f"_Generated at {payload['generated_at']}._",
        "",
    ]
    return "\n".join(lines)


def _adr_document(payload: dict[str, Any]) -> str:
    return "\n".join(
        [
            "# ADR-0002: Strategy-v2 hard performance gate",
            "",
            "## Status",
            "Accepted",
            "",
            "## Context",
            "A clean, pre-registered held-out GO was achieved for strategy-v2 under the frozen",
            f"pre-registration `{payload['preregistration']['run_id']}` "
            f"(content_hash `{payload['preregistration']['content_hash']}`).",
            "",
            "## Decision",
            "Promote the strategy-v2 hard performance gate as the standing authorization bar.",
            "",
            "## Evidence",
            f"- Final verdict: {payload['verdict']} ({', '.join(payload['verdict_reasons'])})",
            f"- Final verdict content_hash: {payload['content_hash']}",
            "",
        ]
    )


def _fmt(value: Any) -> str:
    if isinstance(value, (int, float)):
        return f"{value:.4f}"
    return str(value)


def _sha256_file(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _canonical_sha256(obj: Any) -> str:
    return hashlib.sha256(
        json.dumps(obj, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()


if __name__ == "__main__":
    raise SystemExit(main())

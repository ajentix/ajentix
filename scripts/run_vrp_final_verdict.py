#!/usr/bin/env python3
"""Chain VRP preregistration and upstream reports into the final verdict."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
from collections.abc import Mapping
from pathlib import Path
from typing import Any

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from ajentix_quant.backtest.vrp_breakeven import VRP_BRANCH_INCONCLUSIVE  # noqa: E402
from ajentix_quant.research.vrp_final_verdict import (  # noqa: E402
    VERDICT_GO,
    build_vrp_final_verdict,
    load_verified_preregistration,
)
from ajentix_quant.research.vrp_preregistration import (  # noqa: E402
    PreregistrationError,
)

SCHEMA_VERSION = "vrp-final-verdict-runner-report-v1"
REPORT_STEM = "vrp_final_verdict"
ADR_FILENAME = "0002-vrp-defined-risk-short-vol-gate.md"


def _resolve(repo_root: Path, path: str | Path) -> Path:
    p = Path(path)
    return p if p.is_absolute() else repo_root / p


def _sha256_file(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _load_json(path: Path) -> dict[str, Any]:
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise ValueError(f"report must be a JSON object: {path}")
    return data


def _missing_breakeven() -> dict[str, Any]:
    return {
        "run_status": "missing",
        "branch_decision": VRP_BRANCH_INCONCLUSIVE,
        "reason_codes": ["BREAKEVEN_REPORT_MISSING"],
    }


def _missing_walk_forward() -> dict[str, Any]:
    return {
        "run_status": "missing",
        "ran": False,
        "verdict": "INCONCLUSIVE",
        "clean_heldout_go": False,
        "fold_ids": [],
        "source_quality_authorizing": False,
        "trial_budget_valid": False,
        "non_authorizing_dependence": False,
        "fold_collapse": False,
        "concentration_failure": False,
        "max_loss_invariant_ok": False,
        "reason_codes": ["WALK_FORWARD_REPORT_MISSING"],
        "source_quality": {},
        "stress": {"ran": False, "max_loss_invariant_ok": False},
    }


def _report_with_lineage(
    path: Path,
    default: dict[str, Any],
) -> tuple[dict[str, Any], dict[str, Any]]:
    if not path.is_file():
        return default, {
            "path": path.name,
            "file_sha256": "MISSING",
            "run_status": "missing",
        }
    try:
        payload = _load_json(path)
        return payload, {
            "path": path.name,
            "file_sha256": _sha256_file(path),
            "run_status": str(payload.get("run_status", "missing")),
            "run_id": payload.get("run_id"),
            "preregistration_sha256": payload.get("preregistration_sha256"),
        }
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        failed = {**default, "run_status": "invalid", "load_error": str(exc)}
        return failed, {
            "path": path.name,
            "file_sha256": "INVALID",
            "run_status": "invalid",
            "load_error": str(exc),
        }


def _stress_payload(
    walk_forward: Mapping[str, Any],
    reports_dir: Path,
) -> tuple[dict[str, Any], dict[str, Any] | None]:
    stress_path = reports_dir / "vrp_stress_eth.json"
    if stress_path.is_file():
        payload, lineage = _report_with_lineage(
            stress_path,
            {"run_status": "invalid", "ran": False, "max_loss_invariant_ok": False},
        )
        return payload, lineage
    embedded = walk_forward.get("stress")
    if isinstance(embedded, Mapping):
        return dict(embedded), None
    return {"ran": False, "max_loss_invariant_ok": False}, None


def _load_inputs(
    reports_dir: Path,
) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any], list[dict[str, Any]]]:
    breakeven, breakeven_lineage = _report_with_lineage(
        reports_dir / "vrp_breakeven_eth.json",
        _missing_breakeven(),
    )
    walk_forward, walk_lineage = _report_with_lineage(
        reports_dir / "vrp_walk_forward_eth.json",
        _missing_walk_forward(),
    )
    stress, stress_lineage = _stress_payload(walk_forward, reports_dir)
    lineage = [breakeven_lineage, walk_lineage]
    if stress_lineage is not None:
        lineage.append(stress_lineage)
    return breakeven, walk_forward, stress, lineage


def _load_prereg(
    prereg_path: Path,
    repo_root: Path,
) -> tuple[dict[str, Any], bool, str, list[str]]:
    prereg_sha = _sha256_file(prereg_path) if prereg_path.is_file() else "MISSING"
    try:
        artifact, verify, computed_sha = load_verified_preregistration(prereg_path, repo_root)
        return artifact, verify.valid, computed_sha, list(verify.mismatches)
    except (OSError, PreregistrationError) as exc:
        return {}, False, prereg_sha, [str(exc)]


def _build_payload(args: argparse.Namespace) -> dict[str, Any]:
    repo_root = Path(args.repo_root).resolve()
    reports_dir = _resolve(repo_root, args.reports_dir)
    prereg_path = _resolve(repo_root, args.preregistration)
    preregistration, prereg_valid, prereg_sha, prereg_mismatches = _load_prereg(
        prereg_path,
        repo_root,
    )
    breakeven, walk_forward, stress, upstream_lineage = _load_inputs(reports_dir)
    report = build_vrp_final_verdict(
        preregistration=preregistration,
        preregistration_sha256=prereg_sha,
        preregistration_valid=prereg_valid,
        breakeven=breakeven,
        walk_forward=walk_forward,
        stress=stress,
        upstream_lineage=upstream_lineage,
    )
    payload = {
        **report.as_dict(),
        "schema_version": SCHEMA_VERSION,
        "run_id": preregistration.get("run_id"),
        "content_hash": preregistration.get("content_hash"),
        "preregistration_path": prereg_path.as_posix(),
        "preregistration_valid": prereg_valid,
        "preregistration_mismatches": prereg_mismatches,
        "upstream_reports": upstream_lineage,
        "breakeven_summary": {
            "run_status": breakeven.get("run_status"),
            "branch_decision": breakeven.get("branch_decision"),
            "reason_codes": breakeven.get("reason_codes", []),
        },
        "walk_forward_summary": {
            "run_status": walk_forward.get("run_status"),
            "verdict": walk_forward.get("verdict"),
            "clean_heldout_go": walk_forward.get("clean_heldout_go"),
            "reason_codes": walk_forward.get("reason_codes", []),
        },
        "stress_summary": {
            "ran": stress.get("ran"),
            "max_loss_invariant_ok": stress.get("max_loss_invariant_ok"),
            "reason_codes": stress.get("reason_codes", []),
        },
    }
    return payload


def _adr_document(payload: Mapping[str, Any]) -> str:
    return "\n".join(
        [
            "# ADR-0002: VRP defined-risk short-vol hard performance gate",
            "",
            "## Status",
            "",
            "Promoted",
            "",
            "## Decision",
            "",
            "The pre-registered ETH Deribit defined-risk VRP harness produced a clean held-out "
            "GO with valid lineage, authorizing source quality, completed stress coverage, "
            "valid trial budget, no fold/concentration failure, no non-authorizing dependence, "
            "and intact max-loss invariants.",
            "",
            "## Evidence",
            "",
            f"- run_id: {payload.get('run_id')}",
            f"- preregistration_sha256: {payload.get('preregistration_sha256')}",
            f"- verdict: {payload.get('verdict')}",
            f"- reason_codes: {', '.join(payload.get('reason_codes', []))}",
            "",
        ]
    )


def _markdown(payload: Mapping[str, Any]) -> str:
    adr = payload.get("adr_0002", {})
    ready = adr.get("ready") if isinstance(adr, Mapping) else False
    block_reasons = adr.get("block_reasons", []) if isinstance(adr, Mapping) else []
    lines = [
        "# VRP final verdict",
        "",
        f"- run_status: {payload['run_status']}",
        f"- run_id: {payload.get('run_id')}",
        f"- preregistration_sha256: {payload['preregistration_sha256']}",
        f"- verdict: {payload['verdict']}",
        f"- reason_codes: {', '.join(payload['reason_codes']) or '-'}",
        f"- adr_0002_ready: {ready}",
        f"- adr_0002_block_reasons: {', '.join(block_reasons) or '-'}",
        "",
        "ADR-0002 is promoted only when `vrp_final_verdict` reports a clean held-out GO.",
        "",
        "## Upstream reports",
        "",
    ]
    for row in payload.get("upstream_reports", []):
        if isinstance(row, Mapping):
            lines.append(
                f"- {row.get('path')}: run_status={row.get('run_status')} "
                f"sha256={row.get('file_sha256')}"
            )
    return "\n".join(lines) + "\n"


def _write_reports(repo_root: Path, reports_dir: str | Path, payload: dict[str, Any]) -> list[Path]:
    out_dir = _resolve(repo_root, reports_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    json_path = out_dir / f"{REPORT_STEM}.json"
    md_path = out_dir / f"{REPORT_STEM}.md"
    json_path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    md_path.write_text(_markdown(payload), encoding="utf-8")
    return [json_path, md_path]


def _apply_adr_gate(repo_root: Path, payload: dict[str, Any]) -> Path | None:
    adr_path = repo_root / "docs" / "adr" / ADR_FILENAME
    adr = payload.get("adr_0002", {})
    ready = bool(adr.get("ready")) if isinstance(adr, Mapping) else False
    if ready:
        if payload.get("verdict") != VERDICT_GO:
            raise SystemExit("internal error: ADR ready without GO verdict")
        adr_path.parent.mkdir(parents=True, exist_ok=True)
        adr_path.write_text(_adr_document(payload), encoding="utf-8")
        return adr_path
    if adr_path.exists():
        block_reasons = adr.get("block_reasons", []) if isinstance(adr, Mapping) else []
        raise SystemExit(
            f"refusing: {adr_path} exists but no clean held-out GO authorizes ADR-0002 "
            f"(block reasons: {block_reasons})"
        )
    return None


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Build the final preregistered VRP verdict.")
    parser.add_argument("--preregistration", required=True)
    parser.add_argument("--reports-dir", default="reports")
    parser.add_argument("--json", action="store_true", help="Print the report payload as JSON.")
    parser.add_argument(
        "--repo-root",
        default=str(Path(__file__).resolve().parents[1]),
        help="Repository root (defaults to the checkout containing this script).",
    )
    args = parser.parse_args(argv)

    repo_root = Path(args.repo_root).resolve()
    payload = _build_payload(args)
    paths = _write_reports(repo_root, args.reports_dir, payload)
    adr_path = _apply_adr_gate(repo_root, payload)

    if args.json:
        print(json.dumps(payload, indent=2, sort_keys=True))
    else:
        adr = payload.get("adr_0002", {})
        ready = bool(adr.get("ready")) if isinstance(adr, Mapping) else False
        block_reasons = adr.get("block_reasons", []) if isinstance(adr, Mapping) else []
        print(f"run_status={payload['run_status']}")
        print(f"run_id={payload.get('run_id')}")
        print(f"preregistration_sha256={payload['preregistration_sha256']}")
        print(f"verdict={payload['verdict']}")
        print(f"reason_codes={','.join(payload['reason_codes']) or '-'}")
        print(f"adr_0002_promoted={str(ready).lower()}")
        print(f"adr_0002_block_reasons={','.join(block_reasons) or '-'}")
        for path in paths:
            print(f"wrote={path.relative_to(repo_root)}")
        if adr_path is not None:
            print(f"wrote={adr_path.relative_to(repo_root)}")
    return 0 if payload["run_status"] == "valid" else 1


if __name__ == "__main__":
    raise SystemExit(main())

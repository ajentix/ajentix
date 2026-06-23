#!/usr/bin/env python3
"""Build the honest final VRP-free verdict from local artifacts only."""

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

from ajentix_quant.research.vrp_free_final_verdict import (  # noqa: E402
    FREE_FINAL_VERDICT_INCONCLUSIVE,
    decide_vrp_free_final_verdict,
)
from ajentix_quant.research.vrp_free_preregistration import (  # noqa: E402
    DEFAULT_PRECALIBRATION_OUT_DIR,
    DEFAULT_RAW_SOURCE_ROOT,
    DEFAULT_RECONSTRUCTED_CACHE_ROOT,
    DEFAULT_SCENARIO_ID,
    DEFAULT_SPREAD_CALIBRATION_ROOT,
    PreregistrationError,
    load_preregistration,
    precalibration_config_sha256,
    verify_preregistration,
)

SCHEMA_VERSION = "aq-vrp-free-final-verdict-runner-report-v1"
REPORT_STEM = "vrp_free_final_verdict"


class _MissingPreregistrationError(Exception):
    """Raised when no Phase-3 VRP-free preregistration artifact is available."""


def _resolve(repo_root: Path, path: str | Path) -> Path:
    p = Path(path)
    return p if p.is_absolute() else repo_root / p


def _display(repo_root: Path, path: Path | None) -> str | None:
    if path is None:
        return None
    try:
        return path.relative_to(repo_root).as_posix()
    except ValueError:
        return path.as_posix()


def _sha256_file(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _load_json(path: Path, label: str) -> dict[str, Any]:
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise ValueError(f"{label} must be a JSON object: {path}")
    return data


def _latest(globs: list[str], repo_root: Path) -> Path | None:
    matches: list[Path] = []
    for pattern in globs:
        matches.extend(repo_root.glob(pattern))
    files = [path for path in matches if path.is_file()]
    if not files:
        return None
    return max(files, key=lambda path: (path.stat().st_mtime_ns, path.as_posix()))


def _default_preregistration(repo_root: Path) -> Path | None:
    candidates = [
        path
        for path in repo_root.glob("docs/preregistration/vrp-free-*.json")
        if path.is_file() and "precalibration" not in path.name
    ]
    if not candidates:
        return None
    return max(candidates, key=lambda path: (path.stat().st_mtime_ns, path.as_posix()))


def _default_precalibration(repo_root: Path) -> Path:
    return (
        repo_root
        / DEFAULT_PRECALIBRATION_OUT_DIR
        / f"vrp-free-precalibration-{precalibration_config_sha256()[:12]}.json"
    )


def _report_path(path_arg: str | None, default_globs: list[str], repo_root: Path) -> Path | None:
    if path_arg:
        return _resolve(repo_root, path_arg)
    return _latest(default_globs, repo_root)


def _load_optional_json(
    path: Path | None, label: str, repo_root: Path
) -> tuple[dict[str, Any] | None, dict[str, Any]]:
    if path is None:
        return None, {
            "label": label,
            "path": None,
            "file_sha256": "MISSING",
            "run_status": "missing",
        }
    if not path.is_file():
        return None, {
            "label": label,
            "path": _display(repo_root, path),
            "file_sha256": "MISSING",
            "run_status": "missing",
        }
    try:
        payload = _load_json(path, label)
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        return None, {
            "label": label,
            "path": _display(repo_root, path),
            "file_sha256": "INVALID",
            "run_status": "invalid",
            "load_error": str(exc),
        }
    return payload, {
        "label": label,
        "path": _display(repo_root, path),
        "file_sha256": _sha256_file(path),
        "run_status": str(payload.get("run_status", payload.get("status", "present"))),
        "run_id": payload.get("run_id"),
    }


def _load_preregistration(
    *,
    path: Path | None,
    repo_root: Path,
    args: argparse.Namespace,
) -> tuple[dict[str, Any] | None, bool, list[str], dict[str, Any]]:
    if path is None:
        error = "missing valid Phase-3 VRP-free preregistration artifact"
        return (
            None,
            False,
            [error],
            {
                "label": "preregistration",
                "path": None,
                "file_sha256": "MISSING",
                "run_status": "missing",
                "load_error": error,
            },
        )
    prereg_sha = _sha256_file(path) if path.is_file() else "MISSING"
    try:
        artifact = load_preregistration(path)
        verify = verify_preregistration(
            artifact,
            repo_root,
            raw_source_root=args.raw_source_root,
            reconstructed_cache_root=args.reconstructed_cache_root,
            spread_calibration_root=args.spread_calibration_root,
            scenario_id=args.scenario_id,
            stress_selector_input_path=args.stress_selector_input,
            precalibration_artifact_path=args.precalibration_artifact,
        )
    except (OSError, PreregistrationError) as exc:
        return (
            None,
            False,
            [str(exc)],
            {
                "label": "preregistration",
                "path": _display(repo_root, path),
                "file_sha256": prereg_sha,
                "run_status": "invalid",
                "load_error": str(exc),
            },
        )
    return (
        artifact,
        verify.valid,
        list(verify.mismatches),
        {
            "label": "preregistration",
            "path": _display(repo_root, path),
            "file_sha256": prereg_sha,
            "run_status": verify.run_status,
            "run_id": artifact.get("run_id"),
            "content_hash": artifact.get("content_hash"),
        },
    )


def _manifest(repo_root: Path, root: str | Path, scenario_id: str) -> Path:
    return _resolve(repo_root, root) / scenario_id / "manifest.json"


def _embedded_stress(walk_forward: Mapping[str, Any] | None) -> dict[str, Any] | None:
    if not isinstance(walk_forward, Mapping):
        return None
    stress = walk_forward.get("stress")
    return dict(stress) if isinstance(stress, Mapping) else None


def _embedded_cost_budget(walk_forward: Mapping[str, Any] | None) -> dict[str, Any] | None:
    if not isinstance(walk_forward, Mapping):
        return None
    if isinstance(walk_forward.get("cost_budget"), Mapping):
        return dict(walk_forward["cost_budget"])
    if "cost_budget_status" in walk_forward:
        return {
            "status": walk_forward.get("cost_budget_status"),
            "embedded_in_walk_forward": True,
            "evidence_count": len(walk_forward.get("cost_budget_evidence", [])),
        }
    return None


def _build_payload(args: argparse.Namespace) -> dict[str, Any]:
    repo_root = Path(args.repo_root).resolve()
    prereg_path = (
        _resolve(repo_root, args.preregistration)
        if args.preregistration
        else _default_preregistration(repo_root)
    )
    precalibration_path = (
        _resolve(repo_root, args.precalibration_artifact)
        if args.precalibration_artifact
        else _default_precalibration(repo_root)
    )

    preregistration, prereg_valid, prereg_mismatches, prereg_lineage = _load_preregistration(
        path=prereg_path,
        repo_root=repo_root,
        args=args,
    )
    precalibration, precalibration_lineage = _load_optional_json(
        precalibration_path,
        "precalibration_artifact",
        repo_root,
    )
    raw_manifest, raw_lineage = _load_optional_json(
        _manifest(repo_root, args.raw_source_root, args.scenario_id),
        "raw_history_manifest",
        repo_root,
    )
    reconstructed_manifest, reconstructed_lineage = _load_optional_json(
        _manifest(repo_root, args.reconstructed_cache_root, args.scenario_id),
        "reconstructed_chain_manifest",
        repo_root,
    )
    calibration_manifest, calibration_lineage = _load_optional_json(
        _manifest(repo_root, args.spread_calibration_root, args.scenario_id),
        "tardis_spread_calibration_manifest",
        repo_root,
    )

    breakeven_path = _report_path(
        args.breakeven_report,
        ["reports/vrp_free_breakeven_eth_*.json", "reports/vrp_free_breakeven_eth.json"],
        repo_root,
    )
    walk_path = _report_path(
        args.walk_forward_report,
        ["reports/vrp_free_walk_forward_eth_*.json", "reports/vrp_free_walk_forward_eth.json"],
        repo_root,
    )
    stress_path = _report_path(
        args.stress_report,
        ["reports/vrp_free_stress*.json"],
        repo_root,
    )
    cost_path = _report_path(
        args.cost_budget_report,
        ["reports/vrp_free_cost_budget*.json"],
        repo_root,
    )

    breakeven, breakeven_lineage = _load_optional_json(
        breakeven_path, "breakeven_report", repo_root
    )
    walk_forward, walk_lineage = _load_optional_json(walk_path, "walk_forward_report", repo_root)
    stress, stress_lineage = _load_optional_json(stress_path, "stress_report", repo_root)
    if stress is None:
        stress = _embedded_stress(walk_forward)
        if stress is not None:
            stress_lineage = {
                "label": "stress_report",
                "path": "embedded:walk_forward_report.stress",
                "file_sha256": "EMBEDDED",
                "run_status": str(stress.get("status", "present")),
            }
    cost_budget, cost_lineage = _load_optional_json(cost_path, "cost_budget", repo_root)
    if cost_budget is None:
        cost_budget = _embedded_cost_budget(walk_forward)
        if cost_budget is not None:
            cost_lineage = {
                "label": "cost_budget",
                "path": "embedded:walk_forward_report.cost_budget_status",
                "file_sha256": "EMBEDDED",
                "run_status": str(cost_budget.get("status", "present")),
            }

    raw_hash_overrides = {
        "precalibration_artifact": precalibration_lineage["file_sha256"],
        "preregistration": prereg_lineage["file_sha256"],
        "raw_history_manifest": raw_lineage["file_sha256"],
        "reconstructed_chain_manifest": reconstructed_lineage["file_sha256"],
        "tardis_spread_calibration_manifest": calibration_lineage["file_sha256"],
        "breakeven_report": breakeven_lineage["file_sha256"],
        "walk_forward_report": walk_lineage["file_sha256"],
        "stress_report": stress_lineage["file_sha256"],
        "cost_budget": cost_lineage["file_sha256"],
    }
    hash_overrides = {
        key: value
        for key, value in raw_hash_overrides.items()
        if isinstance(value, str) and value != "EMBEDDED"
    }
    report = decide_vrp_free_final_verdict(
        precalibration_artifact=precalibration,
        preregistration=preregistration,
        preregistration_valid=prereg_valid,
        raw_history_manifest=raw_manifest,
        reconstructed_chain_manifest=reconstructed_manifest,
        tardis_spread_calibration_manifest=calibration_manifest,
        breakeven_report=breakeven,
        walk_forward_report=walk_forward,
        stress_result=stress,
        cost_budget_report=cost_budget,
        scenario_id=args.scenario_id,
        lineage_hash_overrides=hash_overrides,
    )
    upstream_reports = [
        prereg_lineage,
        precalibration_lineage,
        raw_lineage,
        reconstructed_lineage,
        calibration_lineage,
        breakeven_lineage,
        walk_lineage,
        stress_lineage,
        cost_lineage,
    ]
    payload = {
        **report.as_dict(),
        "schema_version": SCHEMA_VERSION,
        "run_id": None if preregistration is None else preregistration.get("run_id"),
        "content_hash": None if preregistration is None else preregistration.get("content_hash"),
        "preregistration_path": _display(repo_root, prereg_path),
        "preregistration_valid": prereg_valid,
        "preregistration_mismatches": prereg_mismatches,
        "upstream_reports": upstream_reports,
        "network_attempted": False,
        "live_orders_attempted": False,
        "fabricated_data": False,
        "terminal_outcome": "pending_real_data_collection"
        if report.verdict == FREE_FINAL_VERDICT_INCONCLUSIVE
        else report.verdict,
    }
    return payload


def _markdown(payload: Mapping[str, Any]) -> str:
    lines = [
        "# VRP-free final verdict",
        "",
        f"- run_status: {payload['run_status']}",
        f"- run_id: {payload.get('run_id')}",
        f"- verdict: {payload['verdict']}",
        f"- allowed_verdicts: {', '.join(payload['allowed_verdicts'])}",
        f"- authorizing: {str(payload['authorizing']).lower()}",
        f"- capital_go_allowed: {str(payload['capital_go_allowed']).lower()}",
        f"- reason_codes: {', '.join(payload['reason_codes']) or '-'}",
        "",
        "## Characterization",
        "",
        str(payload["characterization"]),
        "",
        "## Upstream artifacts",
        "",
    ]
    for row in payload.get("upstream_reports", []):
        if isinstance(row, Mapping):
            lines.append(
                f"- {row.get('label')}: path={row.get('path')} "
                f"run_status={row.get('run_status')} sha256={row.get('file_sha256')}"
            )
    lines.extend(
        [
            "",
            "No live network fetch, order placement, or synthetic positive result "
            "is performed by this runner.",
        ]
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


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Build the final VRP-free verdict without network I/O."
    )
    parser.add_argument("--preregistration", help="Path to a Phase-3 vrp-free-*.json artifact.")
    parser.add_argument("--precalibration-artifact")
    parser.add_argument("--raw-source-root", default=DEFAULT_RAW_SOURCE_ROOT)
    parser.add_argument("--reconstructed-cache-root", default=DEFAULT_RECONSTRUCTED_CACHE_ROOT)
    parser.add_argument("--spread-calibration-root", default=DEFAULT_SPREAD_CALIBRATION_ROOT)
    parser.add_argument("--stress-selector-input")
    parser.add_argument("--scenario-id", default=DEFAULT_SCENARIO_ID)
    parser.add_argument("--breakeven-report")
    parser.add_argument("--walk-forward-report")
    parser.add_argument("--stress-report")
    parser.add_argument("--cost-budget-report")
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
    if args.json:
        print(json.dumps(payload, indent=2, sort_keys=True))
    else:
        print(f"run_status={payload['run_status']}")
        print(f"run_id={payload.get('run_id')}")
        print(f"verdict={payload['verdict']}")
        print(f"reason_codes={','.join(payload['reason_codes']) or '-'}")
        for path in paths:
            print(f"wrote={_display(repo_root, path)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

#!/usr/bin/env python3
"""Run preregistered TRAIN-only VRP breakeven analysis over a replay cache."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from ajentix_quant.backtest.vrp_breakeven import (  # noqa: E402
    VRP_BRANCH_INCONCLUSIVE,
    VRP_BRANCH_NO_GO,
    VRP_BRANCH_WALK_FORWARD,
    VRP_BREAKEVEN_CLEARS,
    VRP_BREAKEVEN_INCONCLUSIVE,
    VRP_BREAKEVEN_NO_GO,
    VrpBreakevenSample,
    analyze_vrp_breakeven,
)
from ajentix_quant.data.options_cache import (  # noqa: E402
    OptionsCacheValidationError,
    load_normalized_cache,
    load_normalized_manifest,
)
from ajentix_quant.options.types import OptionChainSnapshot  # noqa: E402
from ajentix_quant.research.vrp_preregistration import (  # noqa: E402
    DEFAULT_SCENARIO_ID,
    PLAN_FOLDS,
    PLAN_PRIMARY_EQUITY,
    PreregistrationError,
    load_preregistration,
    verify_preregistration,
)
from ajentix_quant.strategies.vrp_defined_risk import (  # noqa: E402
    construct_vrp_defined_risk_structures,
)

SCHEMA_VERSION = "vrp-breakeven-runner-report-v1"
REPORT_STEM = "vrp_breakeven_eth"
_MS_PER_WEEK = 7 * 86_400_000


def _parse_iso_ms(value: str) -> int:
    normalized = value[:-1] + "+00:00" if value.endswith("Z") else value
    dt = datetime.fromisoformat(normalized)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return int(dt.timestamp() * 1000)


def _sha256_file(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _resolve(repo_root: Path, path: str | Path) -> Path:
    p = Path(path)
    return p if p.is_absolute() else repo_root / p


def _quality_values(manifest: Mapping[str, Any] | None) -> dict[str, str]:
    if not manifest:
        return {}
    source_quality = manifest.get("source_quality")
    if not isinstance(source_quality, Mapping):
        return {}
    return {str(key): str(value) for key, value in source_quality.items()}


def _samples_for_fold(
    snapshots: Sequence[OptionChainSnapshot],
    *,
    symbol: str,
    fold_id: str,
    train_start_ms: int,
    train_end_ms: int,
) -> list[VrpBreakevenSample]:
    samples: list[VrpBreakevenSample] = []
    for snapshot in snapshots:
        if snapshot.underlying != symbol.upper():
            continue
        if not (train_start_ms <= snapshot.snapshot_ts_ms < train_end_ms):
            continue
        structures = construct_vrp_defined_risk_structures(snapshot)
        for structure in structures:
            week = snapshot.snapshot_ts_ms // _MS_PER_WEEK
            samples.append(
                VrpBreakevenSample(
                    timestamp_ms=snapshot.snapshot_ts_ms,
                    structure=structure,
                    fold_id=fold_id,
                    cluster_key=f"{fold_id}:expiry={structure.expiry_ms}:week={week}",
                    cost_mode="taker",
                )
            )
    return samples


def _run_fold_results(
    snapshots: Sequence[OptionChainSnapshot],
    *,
    symbol: str,
) -> list[dict[str, Any]]:
    fold_results: list[dict[str, Any]] = []
    for fold in PLAN_FOLDS:
        fold_id = str(fold["id"])
        train_start_ms = _parse_iso_ms(str(fold["train_start"]))
        train_end_ms = _parse_iso_ms(str(fold["train_end"]))
        samples = _samples_for_fold(
            snapshots,
            symbol=symbol,
            fold_id=fold_id,
            train_start_ms=train_start_ms,
            train_end_ms=train_end_ms,
        )
        result = analyze_vrp_breakeven(
            samples,
            train_start_ms=train_start_ms,
            train_end_ms=train_end_ms,
            equity_usd=PLAN_PRIMARY_EQUITY,
        )
        fold_results.append(
            {
                "fold_id": fold_id,
                "train_start_ms": train_start_ms,
                "train_end_ms": train_end_ms,
                "train_candidate_samples": len(samples),
                "result": result.as_dict(include_windows=False),
            }
        )
    return fold_results


def _overall_from_folds(fold_results: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    branches: list[str] = []
    decisions: list[str] = []
    selected: list[str] = []
    reasons: list[str] = []
    freeze_hashes: list[str] = []
    train_samples = 0
    total_samples = 0

    for fold in fold_results:
        result = fold.get("result")
        if not isinstance(result, Mapping):
            continue
        branches.append(str(result.get("branch_decision")))
        decisions.append(str(result.get("decision")))
        selected.extend(str(value) for value in result.get("selected_param_keys", []))
        reasons.extend(str(value) for value in result.get("reason_codes", []))
        if result.get("param_freeze_hash"):
            freeze_hashes.append(str(result["param_freeze_hash"]))
        train_samples += int(result.get("train_samples", 0))
        total_samples += int(result.get("total_samples", 0))

    if any(branch == VRP_BRANCH_WALK_FORWARD for branch in branches):
        branch = VRP_BRANCH_WALK_FORWARD
        decision = VRP_BREAKEVEN_CLEARS
    elif any(branch == VRP_BRANCH_INCONCLUSIVE for branch in branches):
        branch = VRP_BRANCH_INCONCLUSIVE
        decision = VRP_BREAKEVEN_INCONCLUSIVE
    else:
        branch = VRP_BRANCH_NO_GO
        decision = VRP_BREAKEVEN_NO_GO

    return {
        "decision": decision,
        "branch_decision": branch,
        "selected_param_keys": sorted(set(selected)),
        "reason_codes": list(dict.fromkeys(reasons)),
        "param_freeze_hashes": freeze_hashes,
        "train_samples": train_samples,
        "total_samples": total_samples,
    }


def _invalid_payload(
    *,
    args: argparse.Namespace,
    repo_root: Path,
    prereg_sha: str,
    run_id: str | None,
    mismatches: Sequence[str],
    error: str | None = None,
) -> dict[str, Any]:
    return {
        "schema_version": SCHEMA_VERSION,
        "run_status": "invalid",
        "run_id": run_id,
        "content_hash": None,
        "preregistration_path": _resolve(repo_root, args.preregistration).as_posix(),
        "preregistration_sha256": prereg_sha,
        "scenario_id": args.scenario_id,
        "symbol": args.symbol.upper(),
        "decision": VRP_BREAKEVEN_INCONCLUSIVE,
        "branch_decision": VRP_BRANCH_INCONCLUSIVE,
        "selected_param_keys": [],
        "param_freeze_hashes": [],
        "reason_codes": ["PREREGISTRATION_INVALID"],
        "mismatches": list(mismatches),
        "error": error,
        "fold_results": [],
    }


def _build_payload(args: argparse.Namespace) -> tuple[dict[str, Any], bool]:
    repo_root = Path(args.repo_root).resolve()
    prereg_path = _resolve(repo_root, args.preregistration)
    prereg_sha = _sha256_file(prereg_path) if prereg_path.is_file() else "MISSING"

    try:
        artifact = load_preregistration(prereg_path)
    except PreregistrationError as exc:
        return (
            _invalid_payload(
                args=args,
                repo_root=repo_root,
                prereg_sha=prereg_sha,
                run_id=None,
                mismatches=(str(exc),),
                error=str(exc),
            ),
            False,
        )

    verify = verify_preregistration(
        artifact,
        repo_root,
        cache_root=args.cache_root,
        scenario_id=args.scenario_id,
    )
    if not verify.valid:
        return (
            _invalid_payload(
                args=args,
                repo_root=repo_root,
                prereg_sha=prereg_sha,
                run_id=str(artifact.get("run_id")),
                mismatches=verify.mismatches,
            ),
            False,
        )

    cache_root = _resolve(repo_root, args.cache_root)
    cache_error: str | None = None
    manifest: dict[str, Any] | None = None
    fold_results: list[dict[str, Any]] = []
    try:
        manifest = load_normalized_manifest(cache_root, args.scenario_id)
        snapshots = load_normalized_cache(cache_root, args.scenario_id)
        fold_results = _run_fold_results(snapshots, symbol=args.symbol.upper())
        overall = _overall_from_folds(fold_results)
    except (OSError, OptionsCacheValidationError, ValueError) as exc:
        cache_error = str(exc)
        overall = {
            "decision": VRP_BREAKEVEN_INCONCLUSIVE,
            "branch_decision": VRP_BRANCH_INCONCLUSIVE,
            "selected_param_keys": [],
            "reason_codes": ["CACHE_VALIDATION_FAILED"],
            "param_freeze_hashes": [],
            "train_samples": 0,
            "total_samples": 0,
        }

    payload = {
        "schema_version": SCHEMA_VERSION,
        "run_status": "valid",
        "run_id": artifact.get("run_id"),
        "content_hash": artifact.get("content_hash"),
        "preregistration_path": prereg_path.as_posix(),
        "preregistration_sha256": prereg_sha,
        "source_hashes": artifact.get("source_hashes", {}),
        "raw_source_manifest_sha256": artifact.get("raw_source_manifest_sha256", {}),
        "normalized_cache_manifest_sha256": artifact.get("normalized_cache_manifest_sha256", {}),
        "scenario_id": args.scenario_id,
        "cache_root": cache_root.as_posix(),
        "symbol": args.symbol.upper(),
        "source_quality": _quality_values(manifest),
        "decision": overall["decision"],
        "branch_decision": overall["branch_decision"],
        "selected_param_keys": overall["selected_param_keys"],
        "param_freeze_hashes": overall["param_freeze_hashes"],
        "reason_codes": overall["reason_codes"],
        "train_samples": overall["train_samples"],
        "total_samples": overall["total_samples"],
        "fold_results": fold_results,
        "cache_error": cache_error,
    }
    return payload, True


def _markdown(payload: Mapping[str, Any]) -> str:
    lines = [
        f"# VRP breakeven report: {payload['symbol']}",
        "",
        f"- run_status: {payload['run_status']}",
        f"- run_id: {payload.get('run_id')}",
        f"- preregistration_sha256: {payload['preregistration_sha256']}",
        f"- scenario_id: {payload['scenario_id']}",
        f"- decision: {payload['decision']}",
        f"- branch_decision: {payload['branch_decision']}",
        f"- selected_param_keys: {', '.join(payload['selected_param_keys']) or '-'}",
        f"- reason_codes: {', '.join(payload['reason_codes']) or '-'}",
        "",
        "TRAIN-only fold results are produced by `ajentix_quant.backtest.vrp_breakeven`; "
        "invalid pre-registration lineage is refused with run_status=invalid.",
        "",
        "| Fold | Train samples | Branch | Reasons |",
        "|---|---:|---|---|",
    ]
    for fold in payload.get("fold_results", []):
        if not isinstance(fold, Mapping):
            continue
        result = fold.get("result", {})
        if not isinstance(result, Mapping):
            result = {}
        reasons = result.get("reason_codes", [])
        lines.append(
            f"| {fold.get('fold_id')} | {fold.get('train_candidate_samples')} | "
            f"{result.get('branch_decision')} | {', '.join(reasons) or '-'} |"
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
    parser = argparse.ArgumentParser(description="Run preregistered VRP breakeven analysis.")
    parser.add_argument("--preregistration", required=True)
    parser.add_argument("--cache-root", required=True)
    parser.add_argument("--scenario-id", default=DEFAULT_SCENARIO_ID)
    parser.add_argument("--reports-dir", default="reports")
    parser.add_argument("--symbol", default="ETH", choices=["ETH"])
    parser.add_argument("--json", action="store_true", help="Print the report payload as JSON.")
    parser.add_argument(
        "--repo-root",
        default=str(Path(__file__).resolve().parents[1]),
        help="Repository root (defaults to the checkout containing this script).",
    )
    args = parser.parse_args(argv)

    repo_root = Path(args.repo_root).resolve()
    payload, prereg_valid = _build_payload(args)
    paths = _write_reports(repo_root, args.reports_dir, payload)

    if args.json:
        print(json.dumps(payload, indent=2, sort_keys=True))
    else:
        print(f"run_status={payload['run_status']}")
        print(f"run_id={payload.get('run_id')}")
        print(f"preregistration_sha256={payload['preregistration_sha256']}")
        print(f"branch_decision={payload['branch_decision']}")
        print(f"reason_codes={','.join(payload['reason_codes']) or '-'}")
        for path in paths:
            print(f"wrote={path.relative_to(repo_root)}")
    return 0 if prereg_valid else 1


if __name__ == "__main__":
    raise SystemExit(main())

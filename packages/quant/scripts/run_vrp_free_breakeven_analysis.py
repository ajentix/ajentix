#!/usr/bin/env python3
"""Run network-free VRP-free TRAIN-only breakeven economics over reconstructed cache."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from ajentix_quant.backtest.vrp_free_walk_forward import (  # noqa: E402
    FREE_BREAKEVEN_SCHEMA_VERSION,
    FREE_OUTCOME_INCONCLUSIVE,
    VrpBreakevenSample,
    VrpDefinedRiskStrategy,
    free_non_authorizing_lineage,
    run_free_breakeven,
)
from ajentix_quant.data.options_cache import (  # noqa: E402
    OptionsCacheValidationError,
    load_normalized_cache,
    load_normalized_manifest,
)
from ajentix_quant.options.types import OptionChainSnapshot  # noqa: E402
from ajentix_quant.research.vrp_free_preregistration import (  # noqa: E402
    DEFAULT_SCENARIO_ID,
    PLAN_FOLDS,
    PLAN_PRIMARY_EQUITY,
    PreregistrationError,
    load_preregistration,
    validate_free_lineage_payload,
    verify_preregistration,
)

SCHEMA_VERSION = "aq-vrp-free-breakeven-runner-report-v1"
REPORT_STEM = "vrp_free_breakeven_eth"
_MS_PER_WEEK = 7 * 86_400_000


def _parse_iso_ms(value: str) -> int:
    normalized = value[:-1] + "+00:00" if value.endswith("Z") else value
    parsed = datetime.fromisoformat(normalized)
    return int(parsed.astimezone(UTC).timestamp() * 1000)


def _sha256_file(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _resolve(repo_root: Path, path: str | Path) -> Path:
    p = Path(path)
    return p if p.is_absolute() else repo_root / p


def _quality_values(manifest: dict[str, Any] | None) -> dict[str, str]:
    if not manifest:
        return {}
    raw = manifest.get("source_quality")
    if not isinstance(raw, dict):
        return {}
    return {str(key): str(value) for key, value in raw.items()}


def _samples_for_fold(
    snapshots: tuple[OptionChainSnapshot, ...],
    *,
    symbol: str,
    fold_id: str,
    train_start_ms: int,
    train_end_ms: int,
) -> list[VrpBreakevenSample]:
    strategy = VrpDefinedRiskStrategy(allow_diagnostic_greek_selection=True)
    samples: list[VrpBreakevenSample] = []
    for snapshot in snapshots:
        if snapshot.underlying != symbol.upper():
            continue
        if not (train_start_ms <= snapshot.snapshot_ts_ms < train_end_ms):
            continue
        for structure in strategy.construct_structures(snapshot):
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


def _invalid_payload(
    *,
    args: argparse.Namespace,
    repo_root: Path,
    prereg_sha: str,
    run_id: str | None,
    mismatches: tuple[str, ...],
    error: str | None = None,
) -> dict[str, Any]:
    lineage = free_non_authorizing_lineage(outcome=FREE_OUTCOME_INCONCLUSIVE)
    lineage_check = validate_free_lineage_payload(lineage)
    return {
        "schema_version": SCHEMA_VERSION,
        "economics_schema_version": FREE_BREAKEVEN_SCHEMA_VERSION,
        "run_status": "invalid",
        "outcome": FREE_OUTCOME_INCONCLUSIVE,
        "verdict": FREE_OUTCOME_INCONCLUSIVE,
        "authorizing": False,
        "capital_go_allowed": False,
        "run_id": run_id,
        "content_hash": None,
        "preregistration_path": _resolve(repo_root, args.preregistration).as_posix(),
        "preregistration_sha256": prereg_sha,
        "scenario_id": args.scenario_id,
        "symbol": args.symbol.upper(),
        "free_lineage": lineage,
        "lineage_valid": lineage_check.valid,
        "lineage_mismatches": list(lineage_check.mismatches),
        "reason_codes": ["PREREGISTRATION_INVALID"],
        "mismatches": list(mismatches),
        "error": error,
        "fold_reports": [],
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
        reconstructed_cache_root=args.reconstructed_cache_root,
        scenario_id=args.scenario_id,
        stress_selector_input_path=args.stress_selector_input,
        precalibration_artifact_path=args.precalibration_artifact,
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

    cache_root = _resolve(repo_root, args.reconstructed_cache_root)
    manifest: dict[str, Any] | None = None
    fold_reports: list[dict[str, Any]] = []
    data_error: str | None = None
    try:
        manifest = load_normalized_manifest(cache_root, args.scenario_id)
        snapshots = load_normalized_cache(cache_root, args.scenario_id)
        for fold in PLAN_FOLDS:
            fold_id = str(fold["id"])
            train_start_ms = _parse_iso_ms(str(fold["train_start"]))
            train_end_ms = _parse_iso_ms(str(fold["train_end"]))
            samples = _samples_for_fold(
                snapshots,
                symbol=args.symbol.upper(),
                fold_id=fold_id,
                train_start_ms=train_start_ms,
                train_end_ms=train_end_ms,
            )
            report = run_free_breakeven(
                samples,
                train_start_ms=train_start_ms,
                train_end_ms=train_end_ms,
                equity_usd=PLAN_PRIMARY_EQUITY,
            )
            fold_reports.append(
                {
                    "fold_id": fold_id,
                    "train_start_ms": train_start_ms,
                    "train_end_ms": train_end_ms,
                    "train_candidate_samples": len(samples),
                    "report": report.as_dict(include_windows=False),
                }
            )
    except (OSError, OptionsCacheValidationError, ValueError) as exc:
        data_error = str(exc)

    outcomes = [str(row["report"]["outcome"]) for row in fold_reports]
    if FREE_OUTCOME_INCONCLUSIVE in outcomes or data_error:
        outcome = FREE_OUTCOME_INCONCLUSIVE
    elif "PROMISING_PENDING_REAL_SPREAD" in outcomes:
        outcome = "PROMISING_PENDING_REAL_SPREAD"
    else:
        outcome = "NO_GO"
    lineage = free_non_authorizing_lineage(outcome=outcome)
    lineage_check = validate_free_lineage_payload(lineage)
    reasons = ["CACHE_VALIDATION_FAILED"] if data_error else []
    return (
        {
            "schema_version": SCHEMA_VERSION,
            "economics_schema_version": FREE_BREAKEVEN_SCHEMA_VERSION,
            "run_status": "valid",
            "outcome": outcome,
            "verdict": outcome,
            "authorizing": False,
            "capital_go_allowed": False,
            "run_id": artifact.get("run_id"),
            "content_hash": artifact.get("content_hash"),
            "preregistration_path": prereg_path.as_posix(),
            "preregistration_sha256": prereg_sha,
            "scenario_id": args.scenario_id,
            "reconstructed_cache_root": cache_root.as_posix(),
            "symbol": args.symbol.upper(),
            "source_quality": _quality_values(manifest),
            "free_lineage": lineage,
            "lineage_valid": lineage_check.valid,
            "lineage_mismatches": list(lineage_check.mismatches),
            "reason_codes": reasons,
            "fold_reports": fold_reports,
            "data_error": data_error,
        },
        True,
    )


def _write_report(repo_root: Path, reports_dir: str | Path, payload: dict[str, Any]) -> Path:
    out_dir = _resolve(repo_root, reports_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    run_id = payload.get("run_id") or "invalid"
    path = out_dir / f"{REPORT_STEM}_{run_id}.json"
    path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return path


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run VRP-free TRAIN-only breakeven economics.")
    parser.add_argument("--preregistration", required=True)
    parser.add_argument("--reconstructed-cache-root", required=True)
    parser.add_argument("--scenario-id", default=DEFAULT_SCENARIO_ID)
    parser.add_argument("--reports-dir", default="reports")
    parser.add_argument("--symbol", default="ETH", choices=["ETH"])
    parser.add_argument("--stress-selector-input")
    parser.add_argument("--precalibration-artifact")
    parser.add_argument("--json", action="store_true", help="Print the report payload as JSON.")
    parser.add_argument(
        "--repo-root",
        default=str(Path(__file__).resolve().parents[1]),
        help="Repository root (defaults to the checkout containing this script).",
    )
    args = parser.parse_args(argv)

    repo_root = Path(args.repo_root).resolve()
    payload, prereg_valid = _build_payload(args)
    path = _write_report(repo_root, args.reports_dir, payload)
    if args.json:
        print(json.dumps(payload, indent=2, sort_keys=True))
    else:
        print(f"run_status={payload['run_status']}")
        print(f"run_id={payload.get('run_id')}")
        print(f"preregistration_sha256={payload['preregistration_sha256']}")
        print(f"outcome={payload['outcome']}")
        print(f"reason_codes={','.join(payload['reason_codes']) or '-'}")
        print(f"wrote={path.relative_to(repo_root)}")
    return 0 if prereg_valid else 1


if __name__ == "__main__":
    raise SystemExit(main())

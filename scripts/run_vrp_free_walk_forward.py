#!/usr/bin/env python3
"""Run network-free VRP-free walk-forward economics over reconstructed/cache inputs."""

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
    FREE_OUTCOME_INCONCLUSIVE,
    FREE_WALK_FORWARD_SCHEMA_VERSION,
    VrpBacktestStep,
    VrpDefinedRiskStrategy,
    VrpFoldEvaluation,
    free_non_authorizing_lineage,
    plan_grid_hash,
    run_free_breakeven,
    run_free_walk_forward,
    run_vrp_backtest,
)
from ajentix_quant.data.options_cache import (  # noqa: E402
    OptionsCacheValidationError,
    load_normalized_cache,
    load_normalized_manifest,
)
from ajentix_quant.data.tardis_free_spread_calibration import (  # noqa: E402
    TardisFreeSpreadCalibrationError,
    load_spread_calibration_manifest,
    load_spread_calibration_rows,
    load_tardis_free_structure_samples,
)
from ajentix_quant.data.vrp_free_history_cache import (  # noqa: E402
    VrpFreeHistoryCacheValidationError,
    load_vrp_free_history_cache,
)
from ajentix_quant.options.types import (  # noqa: E402
    DefinedRiskStructure,
    OptionChainSnapshot,
    SourceQuality,
)
from ajentix_quant.research.vrp_free_preregistration import (  # noqa: E402
    DEFAULT_SCENARIO_ID,
    PLAN_FOLDS,
    PLAN_PRIMARY_EQUITY,
    PreregistrationError,
    load_preregistration,
    validate_free_lineage_payload,
    verify_preregistration,
)

SCHEMA_VERSION = "aq-vrp-free-walk-forward-runner-report-v1"
REPORT_STEM = "vrp_free_walk_forward_eth"
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


def _structures_for_range(
    snapshots: tuple[OptionChainSnapshot, ...],
    *,
    symbol: str,
    start_ms: int,
    end_ms: int,
) -> list[tuple[OptionChainSnapshot, DefinedRiskStructure]]:
    strategy = VrpDefinedRiskStrategy(allow_diagnostic_greek_selection=True)
    out: list[tuple[OptionChainSnapshot, DefinedRiskStructure]] = []
    for snapshot in snapshots:
        if snapshot.underlying != symbol.upper():
            continue
        if not (start_ms <= snapshot.snapshot_ts_ms < end_ms):
            continue
        out.extend((snapshot, structure) for structure in strategy.construct_structures(snapshot))
    return out


def _breakeven_samples(
    rows: list[tuple[OptionChainSnapshot, DefinedRiskStructure]], *, fold_id: str
):
    samples = []
    from ajentix_quant.backtest.vrp_free_walk_forward import VrpBreakevenSample  # noqa: PLC0415

    for snapshot, structure in rows:
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


def _fold_inputs(
    snapshots: tuple[OptionChainSnapshot, ...],
    *,
    symbol: str,
) -> tuple[
    dict[str, Any],
    dict[str, Any],
    dict[str, tuple[DefinedRiskStructure, ...]],
    list[VrpFoldEvaluation],
    list[dict[str, Any]],
]:
    branches: dict[str, Any] = {}
    test_backtests: dict[str, Any] = {}
    fold_structures: dict[str, tuple[DefinedRiskStructure, ...]] = {}
    evaluations: list[VrpFoldEvaluation] = []
    details: list[dict[str, Any]] = []
    grid_hash = plan_grid_hash()

    for fold in PLAN_FOLDS:
        fold_id = str(fold["id"])
        train_start_ms = _parse_iso_ms(str(fold["train_start"]))
        train_end_ms = _parse_iso_ms(str(fold["train_end"]))
        test_start_ms = _parse_iso_ms(str(fold["test_start"]))
        test_end_ms = _parse_iso_ms(str(fold["test_end"]))

        train_rows = _structures_for_range(
            snapshots,
            symbol=symbol,
            start_ms=train_start_ms,
            end_ms=train_end_ms,
        )
        breakeven = run_free_breakeven(
            _breakeven_samples(train_rows, fold_id=fold_id),
            train_start_ms=train_start_ms,
            train_end_ms=train_end_ms,
            equity_usd=PLAN_PRIMARY_EQUITY,
        )
        branch = breakeven.committed_result
        branches[fold_id] = branch
        selected = set(branch.selected_param_keys)

        test_rows = _structures_for_range(
            snapshots,
            symbol=symbol,
            start_ms=test_start_ms,
            end_ms=test_end_ms,
        )
        selected_rows = [
            (snapshot, structure)
            for snapshot, structure in test_rows
            if structure.frozen_param_key in selected
        ]
        fold_structures[fold_id] = tuple(structure for _, structure in selected_rows)
        steps = [
            VrpBacktestStep(
                entry_timestamp_ms=snapshot.snapshot_ts_ms,
                structure=structure,
                entry_snapshot=snapshot,
                settlement_price=snapshot.settlement_index_price or snapshot.index_price,
                cost_mode="taker",
            )
            for snapshot, structure in selected_rows
        ]
        backtest = run_vrp_backtest(steps, initial_equity_usd=PLAN_PRIMARY_EQUITY)
        test_backtests[fold_id] = backtest
        selected_key = branch.selected_param_keys[0] if branch.selected_param_keys else "-"
        evaluations.append(
            VrpFoldEvaluation(
                fold_id=fold_id,
                selected_param_key=selected_key,
                param_freeze_hash=branch.param_freeze_hash,
                grid_hash=grid_hash,
                train_trial_count=branch.train_samples,
                heldout_eval_count=1,
                test_rerun_count=1,
                entries=backtest.n_entries,
                pnl_usd=backtest.realized_pnl_usd,
                returns=(
                    (backtest.realized_pnl_usd / PLAN_PRIMARY_EQUITY,)
                    if backtest.n_entries
                    else ()
                ),
                max_drawdown=backtest.max_drawdown,
                stress_max_drawdown=backtest.max_drawdown_including_stress,
                source_quality={"option_chain": SourceQuality.FIXTURE},
                cost_modes=("taker",),
                non_authorizing_labels=(
                    "reconstructed_from_real_trade_iv",
                    "calibrated_spread_sample",
                    "fixture",
                ),
                cluster_pnl={fold_id: backtest.realized_pnl_usd},
                max_loss_invariant_ok=backtest.max_loss_invariant_ok,
                stress_evaluated=True,
            )
        )
        details.append(
            {
                "fold_id": fold_id,
                "train_start_ms": train_start_ms,
                "train_end_ms": train_end_ms,
                "test_start_ms": test_start_ms,
                "test_end_ms": test_end_ms,
                "train_candidate_samples": len(train_rows),
                "test_selected_structures": len(selected_rows),
                "breakeven": breakeven.as_dict(include_windows=False),
                "backtest": backtest.as_dict(),
            }
        )
    return branches, test_backtests, fold_structures, evaluations, details


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
        "economics_schema_version": FREE_WALK_FORWARD_SCHEMA_VERSION,
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
        "fold_details": [],
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
        raw_source_root=args.raw_source_root,
        reconstructed_cache_root=args.reconstructed_cache_root,
        tardis_sample_root=args.tardis_sample_root,
        spread_calibration_root=args.spread_calibration_root,
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

    reconstructed_root = _resolve(repo_root, args.reconstructed_cache_root)
    raw_source_root = _resolve(repo_root, args.raw_source_root)
    spread_calibration_root = _resolve(repo_root, args.spread_calibration_root)
    manifest: dict[str, Any] | None = None
    calibration_manifest: dict[str, Any] | None = None
    calibration_rows: tuple[dict[str, str], ...] = ()
    fold_details: list[dict[str, Any]] = []
    data_error: str | None = None

    try:
        manifest = load_normalized_manifest(reconstructed_root, args.scenario_id)
        snapshots = load_normalized_cache(reconstructed_root, args.scenario_id)
        dataset = load_vrp_free_history_cache(raw_source_root, args.scenario_id)
        calibration_manifest = load_spread_calibration_manifest(
            spread_calibration_root, args.scenario_id
        )
        calibration_rows = load_spread_calibration_rows(spread_calibration_root, args.scenario_id)
        calibration_samples = load_tardis_free_structure_samples(
            tuple(_resolve(repo_root, path) for path in args.tardis_sample_csv)
        )
        branches, test_backtests, fold_structures, evaluations, fold_details = _fold_inputs(
            snapshots,
            symbol=args.symbol.upper(),
        )
        report = run_free_walk_forward(
            train_clearing_branches=branches,
            fold_evaluations=evaluations,
            test_backtests=test_backtests,
            fold_structures=fold_structures,
            calibration_samples=calibration_samples,
            reconstructed_chains=snapshots,
            index_path=dataset.index_path,
            equity_usd=PLAN_PRIMARY_EQUITY,
            scenario_id=args.scenario_id,
        )
    except (
        OSError,
        OptionsCacheValidationError,
        VrpFreeHistoryCacheValidationError,
        TardisFreeSpreadCalibrationError,
        ValueError,
    ) as exc:
        data_error = str(exc)
        lineage = free_non_authorizing_lineage(outcome=FREE_OUTCOME_INCONCLUSIVE)
        lineage_check = validate_free_lineage_payload(lineage)
        return (
            {
                "schema_version": SCHEMA_VERSION,
                "economics_schema_version": FREE_WALK_FORWARD_SCHEMA_VERSION,
                "run_status": "valid",
                "outcome": FREE_OUTCOME_INCONCLUSIVE,
                "verdict": FREE_OUTCOME_INCONCLUSIVE,
                "authorizing": False,
                "capital_go_allowed": False,
                "run_id": artifact.get("run_id"),
                "content_hash": artifact.get("content_hash"),
                "preregistration_path": prereg_path.as_posix(),
                "preregistration_sha256": prereg_sha,
                "scenario_id": args.scenario_id,
                "symbol": args.symbol.upper(),
                "free_lineage": lineage,
                "lineage_valid": lineage_check.valid,
                "lineage_mismatches": list(lineage_check.mismatches),
                "reason_codes": ["DATA_VALIDATION_FAILED"],
                "fold_details": fold_details,
                "data_error": data_error,
            },
            True,
        )

    payload = {
        **report.as_dict(),
        "schema_version": SCHEMA_VERSION,
        "economics_schema_version": FREE_WALK_FORWARD_SCHEMA_VERSION,
        "run_id": artifact.get("run_id"),
        "content_hash": artifact.get("content_hash"),
        "preregistration_path": prereg_path.as_posix(),
        "preregistration_sha256": prereg_sha,
        "scenario_id": args.scenario_id,
        "reconstructed_cache_root": reconstructed_root.as_posix(),
        "raw_source_root": raw_source_root.as_posix(),
        "spread_calibration_root": spread_calibration_root.as_posix(),
        "symbol": args.symbol.upper(),
        "source_quality": _quality_values(manifest),
        "spread_calibration_manifest_sha256": (
            calibration_manifest.get("content_hash") if calibration_manifest else None
        ),
        "spread_calibration_rows": len(calibration_rows),
        "fold_details": fold_details,
        "data_error": data_error,
    }
    return payload, True


def _write_report(repo_root: Path, reports_dir: str | Path, payload: dict[str, Any]) -> Path:
    out_dir = _resolve(repo_root, reports_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    run_id = payload.get("run_id") or "invalid"
    path = out_dir / f"{REPORT_STEM}_{run_id}.json"
    path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return path


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run VRP-free walk-forward economics.")
    parser.add_argument("--preregistration", required=True)
    parser.add_argument("--reconstructed-cache-root", required=True)
    parser.add_argument("--raw-source-root", required=True)
    parser.add_argument("--spread-calibration-root", required=True)
    parser.add_argument("--tardis-sample-root")
    parser.add_argument(
        "--tardis-sample-csv",
        action="append",
        required=True,
        help="Local Tardis-free options_chain CSV used to rebuild real calibration samples.",
    )
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

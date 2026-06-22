#!/usr/bin/env python3
"""Run preregistered VRP walk-forward verdict over PLAN_FOLDS."""

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

from ajentix_quant.adapters.base import SourceQuality  # noqa: E402
from ajentix_quant.backtest.vrp_breakeven import (  # noqa: E402
    VRP_BRANCH_WALK_FORWARD,
    VrpBreakevenSample,
    analyze_vrp_breakeven,
)
from ajentix_quant.backtest.vrp_engine import VrpBacktestStep, run_vrp_backtest  # noqa: E402
from ajentix_quant.backtest.vrp_verdict import (  # noqa: E402
    VrpFoldEvaluation,
    decide_vrp_walk_forward,
    plan_grid_hash,
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

SCHEMA_VERSION = "vrp-walk-forward-runner-report-v1"
REPORT_STEM = "vrp_walk_forward_eth"
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


def _source_quality_for_eval(manifest: Mapping[str, Any] | None) -> dict[str, SourceQuality | str]:
    values = _quality_values(manifest)
    return values if values else {"option_chain": SourceQuality.ABSENT}


def _samples_for_range(
    snapshots: Sequence[OptionChainSnapshot],
    *,
    symbol: str,
    fold_id: str,
    start_ms: int,
    end_ms: int,
) -> list[VrpBreakevenSample]:
    samples: list[VrpBreakevenSample] = []
    for snapshot in snapshots:
        if snapshot.underlying != symbol.upper():
            continue
        if not (start_ms <= snapshot.snapshot_ts_ms < end_ms):
            continue
        for structure in construct_vrp_defined_risk_structures(snapshot):
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


def _test_steps(
    snapshots: Sequence[OptionChainSnapshot],
    *,
    symbol: str,
    selected_param_key: str,
    start_ms: int,
    end_ms: int,
    stress_prices: tuple[float, ...],
) -> list[VrpBacktestStep]:
    steps: list[VrpBacktestStep] = []
    for snapshot in snapshots:
        if snapshot.underlying != symbol.upper():
            continue
        if not (start_ms <= snapshot.snapshot_ts_ms < end_ms):
            continue
        for structure in construct_vrp_defined_risk_structures(snapshot):
            if structure.frozen_param_key != selected_param_key:
                continue
            steps.append(
                VrpBacktestStep(
                    entry_timestamp_ms=snapshot.snapshot_ts_ms,
                    structure=structure,
                    entry_snapshot=snapshot,
                    settlement_price=snapshot.settlement_index_price or snapshot.index_price,
                    stress_settlement_prices=stress_prices,
                    cost_mode="taker",
                )
            )
    return steps


def _stress_prices(artifact: Mapping[str, Any]) -> tuple[float, ...]:
    prices: list[float] = []
    windows = artifact.get("stress_windows", [])
    if not isinstance(windows, Sequence) or isinstance(windows, str | bytes):
        return ()
    for window in windows:
        if not isinstance(window, Mapping):
            continue
        for key in (
            "settlement_price",
            "settlement_index_price",
            "index_price",
            "underlying_price",
        ):
            value = window.get(key)
            if value is None:
                continue
            try:
                price = float(value)
            except (TypeError, ValueError):
                continue
            if price > 0.0:
                prices.append(price)
                break
    return tuple(prices)


def _placeholder_evaluations(
    *,
    source_quality: Mapping[str, SourceQuality | str] | None = None,
    stress_evaluated: bool = False,
) -> list[VrpFoldEvaluation]:
    quality = dict(source_quality or {"option_chain": SourceQuality.ABSENT})
    return [
        VrpFoldEvaluation(
            fold_id=str(fold["id"]),
            selected_param_key="-",
            param_freeze_hash="MISSING",
            grid_hash=plan_grid_hash(),
            train_trial_count=0,
            heldout_eval_count=1,
            test_rerun_count=1,
            entries=0,
            pnl_usd=0.0,
            returns=(),
            max_drawdown=0.0,
            stress_max_drawdown=0.0,
            source_quality=quality,
            cost_modes=("taker",),
            cluster_pnl={str(fold["id"]): 0.0},
            max_loss_invariant_ok=True,
            stress_evaluated=stress_evaluated,
        )
        for fold in PLAN_FOLDS
    ]


def _fold_evaluations(
    snapshots: Sequence[OptionChainSnapshot],
    *,
    symbol: str,
    source_quality: Mapping[str, SourceQuality | str],
    stress_prices: tuple[float, ...],
) -> tuple[list[VrpFoldEvaluation], list[dict[str, Any]]]:
    evaluations: list[VrpFoldEvaluation] = []
    details: list[dict[str, Any]] = []
    grid_hash = plan_grid_hash()
    for fold in PLAN_FOLDS:
        fold_id = str(fold["id"])
        train_start_ms = _parse_iso_ms(str(fold["train_start"]))
        train_end_ms = _parse_iso_ms(str(fold["train_end"]))
        test_start_ms = _parse_iso_ms(str(fold["test_start"]))
        test_end_ms = _parse_iso_ms(str(fold["test_end"]))
        train_samples = _samples_for_range(
            snapshots,
            symbol=symbol,
            fold_id=fold_id,
            start_ms=train_start_ms,
            end_ms=train_end_ms,
        )
        branch = analyze_vrp_breakeven(
            train_samples,
            train_start_ms=train_start_ms,
            train_end_ms=train_end_ms,
            equity_usd=PLAN_PRIMARY_EQUITY,
        )
        selected_key = branch.selected_param_keys[0] if branch.selected_param_keys else "-"
        steps = (
            _test_steps(
                snapshots,
                symbol=symbol,
                selected_param_key=selected_key,
                start_ms=test_start_ms,
                end_ms=test_end_ms,
                stress_prices=stress_prices,
            )
            if branch.branch_decision == VRP_BRANCH_WALK_FORWARD
            else []
        )
        result = run_vrp_backtest(steps, initial_equity_usd=PLAN_PRIMARY_EQUITY)
        fold_return = (
            (result.realized_pnl_usd / PLAN_PRIMARY_EQUITY,) if result.n_entries > 0 else ()
        )
        stress_evaluated = bool(stress_prices) and result.n_entries > 0
        evaluation = VrpFoldEvaluation(
            fold_id=fold_id,
            selected_param_key=selected_key,
            param_freeze_hash=branch.param_freeze_hash,
            grid_hash=grid_hash,
            train_trial_count=branch.train_samples,
            heldout_eval_count=1,
            test_rerun_count=1,
            entries=result.n_entries,
            pnl_usd=result.realized_pnl_usd,
            returns=fold_return,
            max_drawdown=result.max_drawdown,
            stress_max_drawdown=result.max_drawdown_including_stress,
            source_quality=source_quality,
            cost_modes=("taker",),
            cluster_pnl={fold_id: result.realized_pnl_usd},
            max_loss_invariant_ok=result.max_loss_invariant_ok,
            stress_evaluated=stress_evaluated,
        )
        evaluations.append(evaluation)
        details.append(
            {
                "fold_id": fold_id,
                "train_start_ms": train_start_ms,
                "train_end_ms": train_end_ms,
                "test_start_ms": test_start_ms,
                "test_end_ms": test_end_ms,
                "train_candidate_samples": len(train_samples),
                "selected_param_key": selected_key,
                "branch_decision": branch.branch_decision,
                "branch_reason_codes": list(branch.reason_codes),
                "heldout_steps": len(steps),
                "backtest": result.as_dict(),
            }
        )
    return evaluations, details


def _invalid_payload(
    *,
    args: argparse.Namespace,
    repo_root: Path,
    prereg_sha: str,
    run_id: str | None,
    mismatches: Sequence[str],
    error: str | None = None,
) -> dict[str, Any]:
    report = decide_vrp_walk_forward(_placeholder_evaluations())
    payload = {
        **report.as_dict(),
        "schema_version": SCHEMA_VERSION,
        "run_status": "invalid",
        "preregistration_path": _resolve(repo_root, args.preregistration).as_posix(),
        "preregistration_sha256": prereg_sha,
        "run_id": run_id,
        "scenario_id": args.scenario_id,
        "symbol": args.symbol.upper(),
        "source_quality": {"option_chain": SourceQuality.ABSENT.value},
        "mismatches": list(mismatches),
        "error": error,
        "fold_details": [],
        "stress": {"ran": False, "max_loss_invariant_ok": False},
    }
    payload["reason_codes"] = ["PREREGISTRATION_INVALID", *payload["reason_codes"]]
    return payload


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
    data_error: str | None = None
    manifest: dict[str, Any] | None = None
    fold_details: list[dict[str, Any]] = []
    stress_prices = _stress_prices(artifact)
    try:
        manifest = load_normalized_manifest(cache_root, args.scenario_id)
        snapshots = load_normalized_cache(cache_root, args.scenario_id)
        evaluations, fold_details = _fold_evaluations(
            snapshots,
            symbol=args.symbol.upper(),
            source_quality=_source_quality_for_eval(manifest),
            stress_prices=stress_prices,
        )
    except (OSError, OptionsCacheValidationError, ValueError) as exc:
        data_error = str(exc)
        evaluations = _placeholder_evaluations(stress_evaluated=False)

    report = decide_vrp_walk_forward(evaluations)
    report_payload = report.as_dict()
    payload = {
        **report_payload,
        "schema_version": SCHEMA_VERSION,
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
        "stress": {
            "ran": bool(stress_prices) and any(row.entries > 0 for row in evaluations),
            "stress_price_count": len(stress_prices),
            "max_loss_invariant_ok": report.max_loss_invariant_ok,
            "non_authorizing_dependence": report.non_authorizing_dependence,
            "reason_codes": [] if stress_prices else ["STRESS_SETTLEMENT_PRICES_UNAVAILABLE"],
        },
        "fold_details": fold_details,
        "data_error": data_error,
    }
    if data_error:
        payload["reason_codes"] = ["CACHE_VALIDATION_FAILED", *payload["reason_codes"]]
    return payload, True


def _markdown(payload: Mapping[str, Any]) -> str:
    lines = [
        f"# VRP walk-forward verdict: {payload['symbol']}",
        "",
        f"- run_status: {payload['run_status']}",
        f"- run_id: {payload.get('run_id')}",
        f"- preregistration_sha256: {payload['preregistration_sha256']}",
        f"- verdict: {payload['verdict']}",
        f"- clean_heldout_go: {payload['clean_heldout_go']}",
        f"- reason_codes: {', '.join(payload['reason_codes']) or '-'}",
        f"- stress_ran: {payload['stress']['ran']}",
        "",
        "Each PLAN_FOLD is evaluated once through `vrp_engine` and aggregated by "
        "`vrp_verdict`; invalid pre-registration lineage is refused with run_status=invalid.",
        "",
        "| Fold | Selected param | Heldout steps | Branch |",
        "|---|---|---:|---|",
    ]
    for fold in payload.get("fold_details", []):
        if not isinstance(fold, Mapping):
            continue
        lines.append(
            f"| {fold.get('fold_id')} | {fold.get('selected_param_key')} | "
            f"{fold.get('heldout_steps')} | {fold.get('branch_decision')} |"
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
    parser = argparse.ArgumentParser(description="Run preregistered VRP walk-forward verdict.")
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
        print(f"verdict={payload['verdict']}")
        print(f"reason_codes={','.join(payload['reason_codes']) or '-'}")
        for path in paths:
            print(f"wrote={path.relative_to(repo_root)}")
    return 0 if prereg_valid else 1


if __name__ == "__main__":
    raise SystemExit(main())

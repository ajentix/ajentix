#!/usr/bin/env python3
"""Run the network-free VRP-free skew/effective-spread verdict from local caches."""

from __future__ import annotations

import argparse
import json
import math
import sys
from collections.abc import Mapping, Sequence
from contextlib import contextmanager
from copy import deepcopy
from dataclasses import asdict, is_dataclass
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path
from typing import Any

REPO_ROOT_DEFAULT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT_DEFAULT))
sys.path.insert(0, str(REPO_ROOT_DEFAULT / "src"))

import scripts.run_vrp_free_walk_forward as free_walk_runner  # noqa: E402

from ajentix_quant.backtest.vrp_free_cost_budget import (  # noqa: E402
    evaluate_vrp_free_cost_budget,
)
from ajentix_quant.backtest.vrp_free_stress import (  # noqa: E402
    evaluate_exact_underlying_stress,
)
from ajentix_quant.backtest.vrp_free_walk_forward import (  # noqa: E402
    _calibration_bin_for_structure,
    _structure_credit_width_usd,
    run_free_walk_forward,
)
from ajentix_quant.data.deribit_history_effective_spread_calibration import (  # noqa: E402
    EFFECTIVE_SPREAD_METHOD_VERSION,
    EFFECTIVE_SPREAD_SOURCE_BASIS,
    NO_FABRICATION_POLICY,
    SELECTION_BIAS_CAVEAT,
    effective_spread_structure_samples,
)
from ajentix_quant.data.options_cache import (  # noqa: E402
    load_normalized_cache,
    load_normalized_manifest,
)
from ajentix_quant.data.tardis_free_spread_calibration import (  # noqa: E402
    STATUS_RESOLVED,
    abs_log_moneyness_to_bucket,
    dte_days_to_bucket,
    nearest_rank_quantile,
    resolve_spread_quantiles,
)
from ajentix_quant.data.vrp_free_history_cache import (  # noqa: E402
    load_vrp_free_history_cache,
)
from ajentix_quant.options.types import DefinedRiskStructure  # noqa: E402
from ajentix_quant.research.vrp_free_final_verdict import (  # noqa: E402
    decide_vrp_free_final_verdict,
)
from ajentix_quant.research.vrp_free_preregistration import (  # noqa: E402
    DEFAULT_SCENARIO_ID,
    PLAN_PRIMARY_EQUITY,
)
from ajentix_quant.research.vrp_preregistration import PLAN_STRUCTURE_GRID  # noqa: E402
from ajentix_quant.strategies.vrp_defined_risk import VrpDefinedRiskStrategy  # noqa: E402

SCHEMA_VERSION = "aq-vrp-free-skew-verdict-runner-v1"
REPORT_STEM = "vrp_free_skew_verdict"
_MS_PER_DAY = 86_400_000
_DIAGNOSTIC_MAX_QUOTE_AGE_S = 10**9
_HOUR_MS = 3_600_000


def _resolve(repo_root: Path, path: str | Path) -> Path:
    p = Path(path)
    return p if p.is_absolute() else repo_root / p


def _iso_ms(ts_ms: int) -> str:
    return datetime.fromtimestamp(int(ts_ms) / 1000, tz=UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _parse_iso_ms(value: str) -> int:
    normalized = value[:-1] + "+00:00" if value.endswith("Z") else value
    parsed = datetime.fromisoformat(normalized)
    return int(parsed.astimezone(UTC).timestamp() * 1000)


def _quality_values(manifest: Mapping[str, Any] | None) -> dict[str, str]:
    if not manifest:
        return {}
    raw = manifest.get("source_quality")
    if not isinstance(raw, Mapping):
        return {}
    return {str(key): str(value) for key, value in raw.items()}


@contextmanager
def _patched_free_runner_folds(folds: Sequence[Mapping[str, str]]):
    original = free_walk_runner.PLAN_FOLDS
    free_walk_runner.PLAN_FOLDS = tuple(dict(fold) for fold in folds)
    try:
        yield
    finally:
        free_walk_runner.PLAN_FOLDS = original


def _fold_inputs_for_folds(
    snapshots: tuple[Any, ...], *, symbol: str, folds: Sequence[Mapping[str, str]]
):
    with _patched_free_runner_folds(folds):
        return free_walk_runner._fold_inputs(snapshots, symbol=symbol)


def _bounded_single_window_fold(
    *, coverage_start_ms: int, coverage_end_ms: int
) -> tuple[dict[str, str], ...]:
    if coverage_end_ms <= coverage_start_ms:
        raise ValueError("coverage_end_ms must be after coverage_start_ms")
    return (
        {
            "id": "BOUND_2024_11_REAL_CACHE",
            "train_start": _iso_ms(coverage_start_ms),
            "train_end": _iso_ms(coverage_end_ms),
            "test_start": _iso_ms(coverage_end_ms),
            "test_end": _iso_ms(coverage_end_ms + _MS_PER_DAY),
        },
    )


def _selected_structures(
    fold_structures: Mapping[str, Sequence[DefinedRiskStructure]],
) -> tuple[DefinedRiskStructure, ...]:
    return tuple(structure for rows in fold_structures.values() for structure in rows)


def _diagnostic_grid() -> dict[str, Any]:
    grid = deepcopy(PLAN_STRUCTURE_GRID)
    # The real reconstructed cache stores option premia in ETH units while the frozen
    # strategy grid's credit floor is USD-width based. Keep this non-gating diagnostic
    # grid explicit and low-credit so we can exercise stress/cost plumbing without
    # pretending the official branch-clearing gate passed.
    grid["structure_types"] = ["put_credit_spread", "call_credit_spread"]
    grid["dte_targets"] = [21, 30, 45]
    grid["short_leg_abs_delta"] = [0.16, 0.25]
    grid["width_usd"] = [100, 150, 200]
    grid["min_credit_to_width"] = [0.000001]
    return grid


def _diagnostic_candidate_structures(
    snapshots: Sequence[Any],
    *,
    symbol: str,
    index_path: Sequence[Any],
    coverage_start_ms: int,
    coverage_end_ms: int,
    max_structures: int,
) -> tuple[DefinedRiskStructure, ...]:
    if max_structures <= 0:
        return ()
    strategy = VrpDefinedRiskStrategy(
        max_quote_age_s=_DIAGNOSTIC_MAX_QUOTE_AGE_S,
        grid=_diagnostic_grid(),
        allow_diagnostic_greek_selection=True,
    )
    out: list[DefinedRiskStructure] = []
    seen: set[str] = set()
    for snapshot in snapshots:
        if snapshot.underlying != symbol.upper():
            continue
        if not (coverage_start_ms <= snapshot.snapshot_ts_ms <= coverage_end_ms):
            continue
        for structure in strategy.construct_structures(snapshot):
            if structure.structure_id in seen:
                continue
            if dte_days_to_bucket(structure.dte_days) == "out_of_grid":
                continue
            if _structure_moneyness_bucket(structure, index_path) == "out_of_grid":
                continue
            try:
                _calibration_bin_for_structure(
                    fold_id="diagnostic",
                    structure=structure,
                    index_path=index_path,
                    fold_bin_overrides={},
                )
            except (KeyError, TypeError, ValueError):
                continue
            out.append(structure)
            seen.add(structure.structure_id)
            break
        if len(out) >= max_structures:
            break
    return tuple(out)


def _structure_moneyness_bucket(structure: DefinedRiskStructure, index_path: Sequence[Any]) -> str:
    prior = [point for point in index_path if point.timestamp_ms <= structure.entry_quote_ts_ms]
    if not prior:
        return "out_of_grid"
    spot = float(max(prior, key=lambda point: point.timestamp_ms).index_price)
    short_leg = next(leg for leg in structure.legs if str(leg.side) == "short")
    return abs_log_moneyness_to_bucket(abs(math.log(float(short_leg.strike) / spot)))


def _effective_spread_lineage_manifest(
    *,
    raw_manifest: Mapping[str, Any],
    calibration_sample_count: int,
    coverage_trade_count: int,
) -> dict[str, Any]:
    return {
        "manifest_kind": "effective_spread_calibration_inline",
        "source_basis": EFFECTIVE_SPREAD_SOURCE_BASIS,
        "method_version": EFFECTIVE_SPREAD_METHOD_VERSION,
        "raw_history_manifest_sha256": raw_manifest.get("raw_manifest_sha256")
        or raw_manifest.get("content_hash")
        or raw_manifest.get("sha256"),
        "coverage_trade_count": coverage_trade_count,
        "structure_sample_count": calibration_sample_count,
        "selection_bias_caveat": SELECTION_BIAS_CAVEAT,
        "no_fabrication_policy": NO_FABRICATION_POLICY,
        "authorizing": False,
        "capital_go_allowed": False,
    }


def _diagnostic_cost_budget(
    *,
    structures: Sequence[DefinedRiskStructure],
    calibration_samples: Sequence[Any],
    index_path: Sequence[Any],
    coverage_end_ms: int,
) -> dict[str, Any]:
    rows: list[dict[str, Any]] = []
    resolved_observed = 0
    frozen_resolved = 0
    for structure in structures:
        try:
            bin_key = _calibration_bin_for_structure(
                fold_id="diagnostic",
                structure=structure,
                index_path=index_path,
                fold_bin_overrides={},
            )
            matching = tuple(
                sample
                for sample in calibration_samples
                if sample.sample_timestamp_ms <= coverage_end_ms
                and sample.option_type == bin_key.option_type
                and sample.dte_bucket == bin_key.dte_bucket
                and sample.moneyness_bucket == bin_key.moneyness_bucket
                and sample.regime_label == bin_key.regime_label
            )
            frozen_resolution = resolve_spread_quantiles(
                calibration_samples,
                option_type=bin_key.option_type,
                dte_bucket=bin_key.dte_bucket,
                moneyness_bucket=bin_key.moneyness_bucket,
                regime_label=bin_key.regime_label,
                train_end_ms=coverage_end_ms,
            )
            if frozen_resolution.status == STATUS_RESOLVED:
                frozen_resolved += 1
            values = [sample.round_trip_structure_spread_usd for sample in matching]
            p50 = nearest_rank_quantile(values, 0.50) if values else None
            p75 = nearest_rank_quantile(values, 0.75) if values else None
            cost_result = None
            if p50 is not None and p75 is not None:
                gross_credit_usd, width_usd = _structure_credit_width_usd(structure)
                cost_result = evaluate_vrp_free_cost_budget(
                    gross_credit_usd=gross_credit_usd,
                    width_usd=width_usd,
                    p50_spread_usd=p50,
                    p75_spread_usd=p75,
                    sample_count=len(matching),
                    distinct_months=len({sample.sample_month for sample in matching}),
                )
                resolved_observed += 1
            rows.append(
                {
                    "structure_id": structure.structure_id,
                    "frozen_param_key": structure.frozen_param_key,
                    "entry_quote_ts_ms": structure.entry_quote_ts_ms,
                    "dte_days": structure.dte_days,
                    "calibration_bin": _json_ready(bin_key),
                    "matching_sample_count": len(matching),
                    "matching_distinct_month_count": len(
                        {sample.sample_month for sample in matching}
                    ),
                    "observed_p50_round_trip_structure_spread_usd": p50,
                    "observed_p75_round_trip_structure_spread_usd": p75,
                    "observed_quantiles_status": (
                        "RESOLVED_OBSERVED_BIN"
                        if p50 is not None
                        else "NO_MATCHING_EFFECTIVE_SPREAD_SAMPLES"
                    ),
                    "frozen_resolution": _json_ready(frozen_resolution),
                    "cost_budget_result": None if cost_result is None else _json_ready(cost_result),
                }
            )
        except (KeyError, TypeError, ValueError) as exc:
            rows.append(
                {
                    "structure_id": structure.structure_id,
                    "frozen_param_key": structure.frozen_param_key,
                    "status": "not_applicable",
                    "reason": exc.__class__.__name__,
                }
            )
    return {
        "diagnostic": True,
        "authorizing": False,
        "capital_go_allowed": False,
        "gating": False,
        "source_basis": EFFECTIVE_SPREAD_SOURCE_BASIS,
        "selection_bias_caveat": SELECTION_BIAS_CAVEAT,
        "calibration_sample_count": len(calibration_samples),
        "candidate_structure_count": len(structures),
        "observed_quantile_rows_resolved": resolved_observed,
        "frozen_resolution_rows_resolved": frozen_resolved,
        "rows": rows,
    }


def _stress_grid_root_cause(index_path: Sequence[Any]) -> dict[str, Any]:
    """Explain why frozen exact-underlying stress could not run on free data.

    The frozen stress rule requires a regular hourly index grid (24h windows plus a
    trailing 30d hourly history). The free trade-derived index path is event-timestamped
    and in practice never lands on exact-hour boundaries, so no candidate window can be
    formed without separately-gated hourly resampling. Every value here is a real
    measurement of the supplied index path, not a fabricated number.
    """
    points = [int(point.timestamp_ms) for point in index_path]
    on_hour = sum(1 for ts in points if ts % _HOUR_MS == 0)
    if points and on_hour == 0:
        code = "FREE_INDEX_PATH_NOT_HOURLY_GRID"
        detail = (
            f"free trade-derived index path has no exact-hour points ({on_hour}/{len(points)}); "
            "frozen exact-underlying stress requires a regular hourly grid (24h windows + "
            "trailing 30d hourly coverage). running stress on free data would require "
            "separately-gated hourly index resampling, which this non-gating runner does not "
            "perform. this is a structural basis limitation, not a data-quantity shortfall"
        )
    else:
        code = "INSUFFICIENT_STRESS_WINDOW_COVERAGE"
        detail = (
            "fewer than the frozen k non-overlapping 24h windows have full trailing 30d "
            "hourly coverage in the supplied bounded index path"
        )
    return {
        "code": code,
        "detail": detail,
        "index_points": len(points),
        "on_hour_points": on_hour,
        "fabricated": False,
    }


def _diagnostic_stress(
    *,
    structures: Sequence[DefinedRiskStructure],
    index_path: Sequence[Any],
    reconstructed_chains: Sequence[Any],
    scenario_id: str,
) -> dict[str, Any]:
    if not structures:
        return {
            "diagnostic": True,
            "authorizing": False,
            "capital_go_allowed": False,
            "gating": False,
            "status": "not_applicable",
            "ran": False,
            "reason_codes": ["NO_DIAGNOSTIC_CANDIDATE_STRUCTURES"],
        }
    stress = evaluate_exact_underlying_stress(
        structures=tuple(structures),
        index_path=index_path,
        reconstructed_chains=reconstructed_chains,
        equity_usd=PLAN_PRIMARY_EQUITY,
        scenario_id=scenario_id,
    )
    payload = stress.as_dict()
    payload.update(
        {
            "diagnostic": True,
            "authorizing": False,
            "capital_go_allowed": False,
            "gating": False,
            "candidate_structure_count": len(structures),
        }
    )
    if not stress.ran:
        payload["status"] = "not_applicable"
        payload["root_cause"] = _stress_grid_root_cause(index_path)
    return payload


def _build_payload(args: argparse.Namespace) -> dict[str, Any]:
    repo_root = Path(args.repo_root).resolve()
    raw_source_root = _resolve(repo_root, args.raw_source_root)
    reconstructed_cache_root = _resolve(repo_root, args.reconstructed_cache_root)

    raw_dataset = load_vrp_free_history_cache(raw_source_root, args.scenario_id)
    reconstructed_manifest = load_normalized_manifest(reconstructed_cache_root, args.scenario_id)
    snapshots = load_normalized_cache(reconstructed_cache_root, args.scenario_id)

    date_range = raw_dataset.manifest.get("date_range", {})
    coverage_start_ms = int(date_range.get("coverage_start_ts_ms", date_range["start_ts_ms"]))
    coverage_end_ms = int(date_range["end_ts_ms"])
    coverage_trades = tuple(
        trade for trade in raw_dataset.trades if trade.timestamp_ms >= coverage_start_ms
    )
    calibration_samples = effective_spread_structure_samples(
        coverage_trades,
        raw_dataset.index_path,
    )

    if getattr(args, "use_frozen_folds", False):
        bounded_folds = tuple(dict(fold) for fold in free_walk_runner.PLAN_FOLDS)
        fold_mode = "frozen_plan_folds"
    else:
        bounded_folds = _bounded_single_window_fold(
            coverage_start_ms=coverage_start_ms,
            coverage_end_ms=coverage_end_ms,
        )
        fold_mode = "bounded_single_window"
    branches, test_backtests, fold_structures, evaluations, fold_details = _fold_inputs_for_folds(
        snapshots,
        symbol=args.symbol.upper(),
        folds=bounded_folds,
    )
    official_selected_structures = _selected_structures(fold_structures)
    official_stress_attempt = evaluate_exact_underlying_stress(
        structures=official_selected_structures,
        index_path=raw_dataset.index_path,
        reconstructed_chains=snapshots,
        equity_usd=PLAN_PRIMARY_EQUITY,
        scenario_id=args.scenario_id,
    )
    walk_report = run_free_walk_forward(
        train_clearing_branches=branches,
        fold_evaluations=evaluations,
        test_backtests=test_backtests,
        fold_structures=fold_structures,
        calibration_samples=calibration_samples,
        reconstructed_chains=snapshots,
        index_path=raw_dataset.index_path,
        equity_usd=PLAN_PRIMARY_EQUITY,
        scenario_id=args.scenario_id,
        stress_result=official_stress_attempt,
        expected_folds=bounded_folds,
        source_quality=_quality_values(reconstructed_manifest),
    )
    walk_payload = walk_report.as_dict()

    effective_spread_manifest = _effective_spread_lineage_manifest(
        raw_manifest=raw_dataset.manifest,
        calibration_sample_count=len(calibration_samples),
        coverage_trade_count=len(coverage_trades),
    )
    official_cost_budget_summary = {
        "status": walk_payload["cost_budget_status"],
        "evidence_count": len(walk_payload.get("cost_budget_evidence", [])),
        "calibration_sample_count": len(calibration_samples),
        "source_basis": EFFECTIVE_SPREAD_SOURCE_BASIS,
        "diagnostic": False,
        "authorizing": False,
        "capital_go_allowed": False,
        "missing_reason": (
            "NO_TRAIN_CLEARING_SELECTED_STRUCTURES"
            if not walk_payload.get("cost_budget_evidence")
            else None
        ),
    }
    breakeven_report = fold_details[0].get("breakeven") if fold_details else None
    final_verdict = decide_vrp_free_final_verdict(
        preregistration=None,
        preregistration_valid=False,
        raw_history_manifest=raw_dataset.manifest,
        reconstructed_chain_manifest=reconstructed_manifest,
        tardis_spread_calibration_manifest=effective_spread_manifest,
        breakeven_report=breakeven_report,
        walk_forward_report=walk_payload,
        stress_result=official_stress_attempt,
        cost_budget_status=walk_payload["cost_budget_status"],
        cost_budget_report=official_cost_budget_summary,
        scenario_id=args.scenario_id,
    )
    final_payload = final_verdict.as_dict()

    diagnostic_structures = _diagnostic_candidate_structures(
        snapshots,
        symbol=args.symbol.upper(),
        index_path=raw_dataset.index_path,
        coverage_start_ms=coverage_start_ms,
        coverage_end_ms=coverage_end_ms,
        max_structures=int(args.diagnostic_max_structures),
    )
    diagnostics = {
        "diagnostic": True,
        "authorizing": False,
        "capital_go_allowed": False,
        "gating": False,
        "note": (
            "Diagnostics intentionally do not feed the official verdict. They exercise "
            "exact-underlying stress and effective-spread cost-budget plumbing on bounded "
            "real reconstructed candidates only."
        ),
        "candidate_grid": _diagnostic_grid(),
        "candidate_max_quote_age_s": _DIAGNOSTIC_MAX_QUOTE_AGE_S,
        "candidate_structures": [
            _structure_summary(structure) for structure in diagnostic_structures
        ],
        "stress": _diagnostic_stress(
            structures=diagnostic_structures,
            index_path=raw_dataset.index_path,
            reconstructed_chains=snapshots,
            scenario_id=args.scenario_id,
        ),
        "cost_budget": _diagnostic_cost_budget(
            structures=diagnostic_structures,
            calibration_samples=calibration_samples,
            index_path=raw_dataset.index_path,
            coverage_end_ms=coverage_end_ms,
        ),
    }

    payload = {
        "schema_version": SCHEMA_VERSION,
        "run_status": "valid",
        "scenario_id": args.scenario_id,
        "symbol": args.symbol.upper(),
        "repo_root": repo_root.as_posix(),
        "raw_source_root": raw_source_root.as_posix(),
        "reconstructed_cache_root": reconstructed_cache_root.as_posix(),
        "outcome": final_payload["verdict"],
        "verdict": final_payload["verdict"],
        "reason_codes": final_payload["reason_codes"],
        "authorizing": False,
        "capital_go_allowed": False,
        "bounded_window": {
            "coverage_start_ts_ms": coverage_start_ms,
            "coverage_end_ts_ms": coverage_end_ms,
            "coverage_start": _iso_ms(coverage_start_ms),
            "coverage_end": _iso_ms(coverage_end_ms),
            "coverage_trade_count": len(coverage_trades),
            "snapshot_count": len(snapshots),
            "folds": list(bounded_folds),
            "fold_mode": fold_mode,
        },
        "spread_basis": {
            "source_basis": EFFECTIVE_SPREAD_SOURCE_BASIS,
            "method_version": EFFECTIVE_SPREAD_METHOD_VERSION,
            "selection_bias_caveat": SELECTION_BIAS_CAVEAT,
            "no_fabrication_policy": NO_FABRICATION_POLICY,
            "phase3_tardis_preregistration_required": False,
        },
        "effective_spread_calibration": {
            "coverage_trade_count": len(coverage_trades),
            "structure_sample_count": len(calibration_samples),
            "sample_months": sorted({sample.sample_month for sample in calibration_samples}),
        },
        "official": {
            "note": (
                "Official walk-forward remains fail-closed and does not bypass train-clearing "
                "branch selection. Stress/cost inputs are wired; structures feed the official "
                "verdict only when a fold's train window actually clears them."
            ),
            "walk_forward": walk_payload,
            "stress_attempt": official_stress_attempt.as_dict(),
            "cost_budget": official_cost_budget_summary,
            "fold_details": fold_details,
            "selected_structure_count": len(official_selected_structures),
        },
        "diagnostics": diagnostics,
        "final_verdict": final_payload,
        "lineage_note": (
            "The final mapper has a legacy calibration-manifest slot named for Tardis; this "
            "runner supplies the inline Deribit-history effective-spread lineage there and "
            "does not require a Tardis/Phase-3 preregistration artifact."
        ),
    }
    return _json_ready(payload)


def _structure_summary(structure: DefinedRiskStructure) -> dict[str, Any]:
    short_leg = next(leg for leg in structure.legs if str(leg.side) == "short")
    long_leg = next(leg for leg in structure.legs if str(leg.side) == "long")
    return {
        "structure_id": structure.structure_id,
        "frozen_param_key": structure.frozen_param_key,
        "structure_type": structure.structure_type.value,
        "entry_quote_ts_ms": structure.entry_quote_ts_ms,
        "expiry_ms": structure.expiry_ms,
        "dte_days": structure.dte_days,
        "short_leg": short_leg.instrument_name,
        "long_leg": long_leg.instrument_name,
        "net_credit": structure.net_credit,
        "width": structure.width,
        "max_quote_age_s": structure.max_quote_age_s,
    }


def _json_ready(value: Any) -> Any:
    if hasattr(value, "as_dict"):
        return _json_ready(value.as_dict())
    if isinstance(value, StrEnum):
        return value.value
    if is_dataclass(value) and not isinstance(value, type):
        return _json_ready(asdict(value))
    if isinstance(value, Mapping):
        return {str(key): _json_ready(item) for key, item in value.items()}
    if isinstance(value, Sequence) and not isinstance(value, str | bytes | bytearray):
        return [_json_ready(item) for item in value]
    return value


def _write_reports(
    repo_root: Path, reports_dir: str | Path, payload: Mapping[str, Any]
) -> tuple[Path, Path]:
    out_dir = _resolve(repo_root, reports_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    json_path = out_dir / f"{REPORT_STEM}.json"
    md_path = out_dir / f"{REPORT_STEM}.md"
    json_path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    md_path.write_text(_markdown_report(payload), encoding="utf-8")
    return json_path, md_path


def _markdown_report(payload: Mapping[str, Any]) -> str:
    if "official" not in payload:
        lines = [
            "# VRP Free Skew Verdict",
            "",
            f"- verdict: `{payload.get('verdict', 'INCONCLUSIVE')}`",
            f"- reason_codes: `{', '.join(payload.get('reason_codes', [])) or '-'}`",
            f"- run_status: `{payload.get('run_status', 'invalid')}`",
            f"- error_type: `{payload.get('error_type', '-')}`",
            f"- error: {payload.get('error', '-')}",
        ]
        return "\n".join(lines) + "\n"
    official = payload["official"]
    walk = official["walk_forward"]
    diagnostics = payload["diagnostics"]
    diag_stress = diagnostics["stress"]
    diag_cost = diagnostics["cost_budget"]
    bw = payload["bounded_window"]
    diag_reasons = ", ".join(diag_stress.get("reason_codes", [])) or "-"
    diag_root = (diag_stress.get("root_cause") or {}).get("code", "-")
    obs_rows = diag_cost["observed_quantile_rows_resolved"]
    frozen_rows = diag_cost["frozen_resolution_rows_resolved"]
    diag_note = (
        "Diagnostics are `authorizing: false`, `capital_go_allowed: false`, "
        "and do not feed the official verdict."
    )
    lines = [
        "# VRP Free Skew Verdict",
        "",
        f"- verdict: `{payload['verdict']}`",
        f"- reason_codes: `{', '.join(payload['reason_codes']) or '-'}`",
        f"- authorizing: `{payload['authorizing']}`",
        f"- capital_go_allowed: `{payload['capital_go_allowed']}`",
        f"- bounded_window: `{bw['coverage_start']}` to `{bw['coverage_end']}`",
        f"- spread_basis: `{payload['spread_basis']['source_basis']}`",
        f"- selection_bias_caveat: {payload['spread_basis']['selection_bias_caveat']}",
        "",
        "## Official fail-closed walk-forward",
        "",
        f"- outcome: `{walk['outcome']}`",
        f"- reason_codes: `{', '.join(walk['reason_codes']) or '-'}`",
        f"- stress_status: `{walk['stress_status']}`",
        f"- stress_ran: `{walk['stress_ran']}`",
        f"- cost_budget_status: `{walk['cost_budget_status']}`",
        f"- cost_budget_evidence_count: `{len(walk.get('cost_budget_evidence', []))}`",
        f"- selected_structure_count: `{official['selected_structure_count']}`",
        "",
        "## Diagnostics (non-gating)",
        "",
        f"- candidate_structure_count: `{len(diagnostics['candidate_structures'])}`",
        f"- diagnostic_stress_status: `{diag_stress.get('status')}`",
        f"- diagnostic_stress_ran: `{diag_stress.get('ran')}`",
        f"- diagnostic_stress_reason_codes: `{diag_reasons}`",
        f"- diagnostic_stress_root_cause: `{diag_root}`",
        f"- diagnostic_cost_observed_quantile_rows_resolved: `{obs_rows}`",
        f"- diagnostic_cost_frozen_resolution_rows_resolved: `{frozen_rows}`",
        "",
        diag_note,
    ]
    return "\n".join(lines) + "\n"


def _error_payload(args: argparse.Namespace, exc: BaseException) -> dict[str, Any]:
    return {
        "schema_version": SCHEMA_VERSION,
        "run_status": "invalid",
        "outcome": "INCONCLUSIVE",
        "verdict": "INCONCLUSIVE",
        "reason_codes": ["DATA_VALIDATION_FAILED"],
        "authorizing": False,
        "capital_go_allowed": False,
        "scenario_id": args.scenario_id,
        "symbol": args.symbol.upper(),
        "error_type": exc.__class__.__name__,
        "error": str(exc),
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Run a network-free VRP-free skew/effective-spread verdict from local caches."
    )
    parser.add_argument("--repo-root", default=str(REPO_ROOT_DEFAULT))
    parser.add_argument("--raw-source-root", default="data/realrun/raw")
    parser.add_argument("--reconstructed-cache-root", default="data/realrun/recon")
    parser.add_argument("--scenario-id", default=DEFAULT_SCENARIO_ID)
    parser.add_argument("--symbol", default="ETH", choices=["ETH"])
    parser.add_argument("--reports-dir", default="reports")
    parser.add_argument("--diagnostic-max-structures", type=int, default=6)
    parser.add_argument(
        "--use-frozen-folds",
        action="store_true",
        help=(
            "Run the real frozen PLAN_FOLDS (F1-F7) walk-forward instead of the single "
            "bounded coverage-window fold. Use for a full-coverage real cache."
        ),
    )
    parser.add_argument("--json", action="store_true", help="Print the report payload as JSON.")
    args = parser.parse_args(argv)

    repo_root = Path(args.repo_root).resolve()
    try:
        payload = _build_payload(args)
        exit_code = 0
    except Exception as exc:  # noqa: BLE001 - fail-closed report writer
        payload = _error_payload(args, exc)
        exit_code = 1
    json_path, md_path = _write_reports(repo_root, args.reports_dir, payload)
    if args.json:
        print(json.dumps(payload, indent=2, sort_keys=True))
    else:
        print(f"run_status={payload['run_status']}")
        print(f"outcome={payload['outcome']}")
        print(f"reason_codes={','.join(payload['reason_codes']) or '-'}")
        print(f"wrote={json_path.relative_to(repo_root)}")
        print(f"wrote={md_path.relative_to(repo_root)}")
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())

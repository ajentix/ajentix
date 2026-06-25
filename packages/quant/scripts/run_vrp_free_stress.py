#!/usr/bin/env python3
"""Run exact-underlying VRP-free tail stress from local caches only."""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from typing import Any

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from ajentix_quant.backtest.vrp_free_stress import (  # noqa: E402
    SCHEMA_VERSION,
    VrpFreeStressStatus,
    evaluate_exact_underlying_stress,
)
from ajentix_quant.data.options_cache import (  # noqa: E402
    OptionsCacheValidationError,
    load_normalized_cache,
)
from ajentix_quant.data.vrp_free_history_cache import (  # noqa: E402
    VrpFreeHistoryCacheValidationError,
    load_vrp_free_history_cache,
)
from ajentix_quant.research.vrp_free_preregistration import (  # noqa: E402
    DEFAULT_RAW_SOURCE_ROOT,
    DEFAULT_RECONSTRUCTED_CACHE_ROOT,
    DEFAULT_SCENARIO_ID,
    PLAN_PRIMARY_EQUITY,
    PLAN_SOURCE_QUALITY_BRIDGE,
)
from ajentix_quant.strategies.vrp_defined_risk import VrpDefinedRiskStrategy  # noqa: E402


def _resolve(repo_root: Path, path: str | Path) -> Path:
    p = Path(path)
    return p if p.is_absolute() else repo_root / p


def _display(repo_root: Path, path: Path) -> str:
    try:
        return path.relative_to(repo_root).as_posix()
    except ValueError:
        return path.as_posix()


def _base_payload(args: argparse.Namespace) -> dict[str, Any]:
    return {
        "schema_version": SCHEMA_VERSION,
        "scenario_id": args.scenario_id,
        "network_attempted": False,
        "live_fetch_attempted": False,
        "orders_attempted": False,
        "cache_fabricated": False,
        "free_lineage": {
            "free_source_quality": PLAN_SOURCE_QUALITY_BRIDGE["free_source_quality"],
            "authorizing": bool(PLAN_SOURCE_QUALITY_BRIDGE["authorizing"]),
            "capital_go_allowed": bool(PLAN_SOURCE_QUALITY_BRIDGE["capital_go_allowed"]),
            "non_authorizing_reason": PLAN_SOURCE_QUALITY_BRIDGE[
                "non_authorizing_reason"
            ],
        },
    }


def _inconclusive_payload(
    args: argparse.Namespace,
    *,
    reason_code: str,
    error: str,
) -> dict[str, Any]:
    payload = _base_payload(args)
    payload.update(
        {
            "run_status": "invalid",
            "status": VrpFreeStressStatus.INCONCLUSIVE.value,
            "ran": False,
            "max_loss_ok": False,
            "reason_codes": [reason_code],
            "error": error,
            "stress": {
                "schema_version": SCHEMA_VERSION,
                "scenario_id": args.scenario_id,
                "status": VrpFreeStressStatus.INCONCLUSIVE.value,
                "ran": False,
                "reason_codes": [reason_code],
                "max_loss_ok": False,
                "selected_windows": [],
                "max_loss_evidence": [],
                "lineage": payload["free_lineage"],
            },
        }
    )
    return payload


def _build_payload(args: argparse.Namespace) -> dict[str, Any]:
    repo_root = Path(args.repo_root).resolve()
    raw_root = _resolve(repo_root, args.raw_source_root)
    reconstructed_root = _resolve(repo_root, args.reconstructed_cache_root)
    try:
        dataset = load_vrp_free_history_cache(raw_root, args.scenario_id)
        snapshots = load_normalized_cache(reconstructed_root, args.scenario_id)
        strategy = VrpDefinedRiskStrategy(allow_diagnostic_greek_selection=True)
        structures = tuple(
            structure
            for snapshot in snapshots
            for structure in strategy.construct_structures(snapshot)
        )
        result = evaluate_exact_underlying_stress(
            structures=structures,
            index_path=dataset.index_path,
            reconstructed_chains=snapshots,
            equity_usd=args.equity_usd,
            scenario_id=args.scenario_id,
            cost_mode=args.cost_mode,
            taker_fee_bps=args.taker_fee_bps,
            usd_conversion_rate=args.usd_conversion_rate,
        )
    except (VrpFreeHistoryCacheValidationError, OptionsCacheValidationError, ValueError) as exc:
        return _inconclusive_payload(
            args,
            reason_code=exc.__class__.__name__,
            error=str(exc),
        )

    payload = _base_payload(args)
    stress_payload = result.as_dict()
    payload.update(
        {
            "run_status": "valid"
            if result.status is VrpFreeStressStatus.RAN and result.max_loss_ok
            else "invalid",
            "status": result.status.value,
            "ran": result.ran,
            "max_loss_ok": result.max_loss_ok,
            "reason_codes": list(result.reason_codes),
            "raw_source_root": _display(repo_root, raw_root),
            "reconstructed_cache_root": _display(repo_root, reconstructed_root),
            "equity_usd": float(args.equity_usd),
            "cost_mode": args.cost_mode,
            "taker_fee_bps": args.taker_fee_bps,
            "usd_conversion_rate": float(args.usd_conversion_rate),
            "index_point_count": len(dataset.index_path),
            "snapshot_count": len(snapshots),
            "structure_count": len(structures),
            "stress": stress_payload,
        }
    )
    return payload


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Run deterministic VRP-free exact-underlying stress from local caches."
    )
    parser.add_argument("--scenario-id", default=DEFAULT_SCENARIO_ID)
    parser.add_argument("--raw-source-root", default=DEFAULT_RAW_SOURCE_ROOT)
    parser.add_argument("--reconstructed-cache-root", default=DEFAULT_RECONSTRUCTED_CACHE_ROOT)
    parser.add_argument("--equity-usd", type=float, default=PLAN_PRIMARY_EQUITY)
    parser.add_argument("--cost-mode", default="taker")
    parser.add_argument("--taker-fee-bps", type=float, default=None)
    parser.add_argument("--usd-conversion-rate", type=float, default=1.0)
    parser.add_argument(
        "--output",
        help="Optional JSON report path. The same deterministic JSON is always printed.",
    )
    parser.add_argument(
        "--repo-root",
        default=str(Path(__file__).resolve().parents[1]),
        help="Repository root (defaults to the checkout containing this script).",
    )
    args = parser.parse_args(argv)

    payload = _build_payload(args)
    text = json.dumps(payload, indent=2, sort_keys=True) + "\n"
    if args.output:
        output_path = _resolve(Path(args.repo_root).resolve(), args.output)
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_text(text, encoding="utf-8")
    print(text, end="")
    return 0 if payload["run_status"] == "valid" else 1


if __name__ == "__main__":
    raise SystemExit(main())

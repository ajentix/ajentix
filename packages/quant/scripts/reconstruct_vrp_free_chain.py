#!/usr/bin/env python3
"""Reconstruct the VRP-free option-chain cache from a network-free raw history cache."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
from pathlib import Path
from typing import Any

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from ajentix_quant.data.vrp_free_history_cache import (  # noqa: E402
    VrpFreeHistoryCacheValidationError,
    load_vrp_free_history_cache,
)
from ajentix_quant.options.iv_surface_reconstruction import (  # noqa: E402
    LINEAGE_FILE,
    SCHEMA_VERSION,
    IVSurfaceReconstructionError,
    reconstruct_from_history_dataset,
    reconstructed_chains_sha256,
    write_reconstructed_chain_cache,
)
from ajentix_quant.research.vrp_free_preregistration import (  # noqa: E402
    DEFAULT_RAW_SOURCE_ROOT,
    DEFAULT_RECONSTRUCTED_CACHE_ROOT,
    DEFAULT_SCENARIO_ID,
)

STATUS_POPULATED = "POPULATED_VRP_FREE_RECONSTRUCTED_CHAIN_CACHE"
STATUS_INCONCLUSIVE = "INCONCLUSIVE_RECONSTRUCTION_COVERAGE"


def _resolve(repo_root: Path, path: str | Path) -> Path:
    p = Path(path)
    return p if p.is_absolute() else repo_root / p


def _sha256_file(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _base_payload(args: argparse.Namespace, status: str, reason_codes: list[str]) -> dict[str, Any]:
    return {
        "schema_version": SCHEMA_VERSION,
        "run_status": "valid" if status == STATUS_POPULATED else "invalid",
        "status": status,
        "reason_codes": list(dict.fromkeys(reason_codes)),
        "scenario_id": args.scenario_id,
        "network_attempted": False,
        "live_fetch_attempted": False,
        "cache_fabricated": False,
        "synthetic_model_prices": True,
        "fabricated_quotes_or_spreads": False,
        "cache_writes": [],
    }


def _run(args: argparse.Namespace, repo_root: Path) -> dict[str, Any]:
    raw_root = _resolve(repo_root, args.raw_source_root)
    out_root = _resolve(repo_root, args.reconstructed_cache_root)
    try:
        dataset = load_vrp_free_history_cache(raw_root, args.scenario_id)
        chains = reconstruct_from_history_dataset(
            dataset,
            snapshot_timestamps_ms=tuple(args.snapshot_timestamp_ms or ()) or None,
            stress_timestamps_ms=tuple(args.stress_timestamp_ms or ()),
            scenario_id=args.scenario_id,
        )
        raw_manifest_sha = _sha256_file(raw_root / args.scenario_id / "manifest.json")
        scenario_dir = write_reconstructed_chain_cache(
            out_root,
            args.scenario_id,
            reconstructed_chains=chains,
            raw_manifest_sha256=raw_manifest_sha,
        )
    except (VrpFreeHistoryCacheValidationError, IVSurfaceReconstructionError) as exc:
        payload = _base_payload(args, STATUS_INCONCLUSIVE, [exc.__class__.__name__])
        payload["error"] = str(exc)
        return payload

    manifest_path = scenario_dir / "manifest.json"
    payload = _base_payload(args, STATUS_POPULATED, ["RECONSTRUCTED_CHAIN_CACHE_WRITTEN"])
    payload.update(
        {
            "cache_writes": [scenario_dir.relative_to(repo_root).as_posix()],
            "snapshot_count": len({chain.snapshot.snapshot_ts_ms for chain in chains}),
            "chain_count": len(chains),
            "leg_count": sum(len(chain.snapshot.legs) for chain in chains),
            "reconstructed_chain_sha256": reconstructed_chains_sha256(chains),
            "manifest_sha256": _sha256_file(manifest_path),
            "lineage_file": LINEAGE_FILE,
        }
    )
    return payload


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Reconstruct non-authorizing VRP-free option chains from raw trade IV."
    )
    parser.add_argument("--scenario-id", default=DEFAULT_SCENARIO_ID)
    parser.add_argument("--raw-source-root", default=DEFAULT_RAW_SOURCE_ROOT)
    parser.add_argument("--reconstructed-cache-root", default=DEFAULT_RECONSTRUCTED_CACHE_ROOT)
    parser.add_argument(
        "--snapshot-timestamp-ms",
        action="append",
        type=int,
        help="Optional exact reconstruction timestamp; repeatable. Defaults to the frozen grid.",
    )
    parser.add_argument(
        "--stress-timestamp-ms",
        action="append",
        type=int,
        help="Optional stress timestamp to include when building the default frozen grid.",
    )
    parser.add_argument("--json", action="store_true", help="Print the full report payload.")
    parser.add_argument(
        "--repo-root",
        default=str(Path(__file__).resolve().parents[1]),
        help="Repository root (defaults to the checkout containing this script).",
    )
    args = parser.parse_args(argv)

    repo_root = Path(args.repo_root).resolve()
    payload = _run(args, repo_root)
    if args.json:
        print(json.dumps(payload, indent=2, sort_keys=True))
    else:
        print(f"run_status={payload['run_status']}")
        print(f"status={payload['status']}")
        print(f"reason_codes={','.join(payload['reason_codes']) or '-'}")
        for path in payload.get("cache_writes", []):
            print(f"wrote={path}")
    return 0 if payload["status"] == STATUS_POPULATED else 1


if __name__ == "__main__":
    raise SystemExit(main())

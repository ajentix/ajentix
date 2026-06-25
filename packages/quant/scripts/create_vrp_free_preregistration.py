#!/usr/bin/env python3
"""Create the immutable VRP-free pre-registration artifact, or dry-run schema mode.

Dry-run/schema mode builds and validates without writing. Full final emission is guarded:
raw, reconstructed, Tardis-sample, spread-calibration, pre-calibration, and stress-input
manifests must already exist, and the calibration output must cite the frozen
precalibration_config_sha256.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from ajentix_quant.research.vrp_free_preregistration import (  # noqa: E402
    DEFAULT_RAW_SOURCE_ROOT,
    DEFAULT_RECONSTRUCTED_CACHE_ROOT,
    DEFAULT_SCENARIO_ID,
    DEFAULT_SPREAD_CALIBRATION_ROOT,
    DEFAULT_TARDIS_SAMPLE_ROOT,
    PreregistrationError,
    build_preregistration,
    load_preregistration,
    verify_preregistration,
    write_preregistration,
)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Create the VRP-free pre-registration artifact.")
    parser.add_argument("--dry-run", action="store_true", help="Build and print; do not write.")
    parser.add_argument(
        "--schema-only",
        action="store_true",
        help="Alias for a dry-run schema build with MISSING manifests allowed.",
    )
    parser.add_argument("--raw-manifest", help="Raw Deribit history manifest path.")
    parser.add_argument("--reconstructed-manifest", help="Reconstructed chain manifest path.")
    parser.add_argument("--tardis-sample-manifest", help="Tardis free sample manifest path.")
    parser.add_argument("--spread-calibration-manifest", help="Spread calibration manifest path.")
    parser.add_argument("--precalibration-artifact", help="Phase-0 pre-calibration artifact path.")
    parser.add_argument("--stress-selector-input", help="Stress selector-input manifest path.")
    parser.add_argument("--scenario-id", default=DEFAULT_SCENARIO_ID)
    parser.add_argument("--out", help="Output directory for final Phase-3 artifact emission.")
    parser.add_argument("--raw-source-root", default=DEFAULT_RAW_SOURCE_ROOT)
    parser.add_argument("--reconstructed-cache-root", default=DEFAULT_RECONSTRUCTED_CACHE_ROOT)
    parser.add_argument("--tardis-sample-root", default=DEFAULT_TARDIS_SAMPLE_ROOT)
    parser.add_argument("--spread-calibration-root", default=DEFAULT_SPREAD_CALIBRATION_ROOT)
    parser.add_argument(
        "--repo-root",
        default=str(Path(__file__).resolve().parents[1]),
        help="Repository root (defaults to the checkout containing this script).",
    )
    args = parser.parse_args(argv)

    if args.dry_run or args.schema_only:
        first = build_preregistration(
            args.repo_root,
            raw_source_root=args.raw_source_root,
            reconstructed_cache_root=args.reconstructed_cache_root,
            tardis_sample_root=args.tardis_sample_root,
            spread_calibration_root=args.spread_calibration_root,
            scenario_id=args.scenario_id,
            stress_selector_input_path=args.stress_selector_input,
            precalibration_artifact_path=args.precalibration_artifact,
        )
        second = build_preregistration(
            args.repo_root,
            raw_source_root=args.raw_source_root,
            reconstructed_cache_root=args.reconstructed_cache_root,
            tardis_sample_root=args.tardis_sample_root,
            spread_calibration_root=args.spread_calibration_root,
            scenario_id=args.scenario_id,
            stress_selector_input_path=args.stress_selector_input,
            precalibration_artifact_path=args.precalibration_artifact,
        )
        if first != second:
            print("VRP-free pre-registration build is not deterministic", file=sys.stderr)
            return 1
        result = verify_preregistration(
            first,
            args.repo_root,
            scenario_id=args.scenario_id,
            stress_selector_input_path=args.stress_selector_input,
            precalibration_artifact_path=args.precalibration_artifact,
        )
        if not result.valid:
            print(json.dumps(result.as_dict(), indent=2, sort_keys=True), file=sys.stderr)
            return 1
        print(json.dumps(first, indent=2, sort_keys=True))
        return 0

    missing = [
        flag
        for flag, value in (
            ("--raw-manifest", args.raw_manifest),
            ("--reconstructed-manifest", args.reconstructed_manifest),
            ("--tardis-sample-manifest", args.tardis_sample_manifest),
            ("--spread-calibration-manifest", args.spread_calibration_manifest),
            ("--precalibration-artifact", args.precalibration_artifact),
            ("--stress-selector-input", args.stress_selector_input),
            ("--out", args.out),
        )
        if not value
    ]
    if missing:
        parser.error(f"full VRP-free artifact emission requires {', '.join(missing)}")

    try:
        dest = write_preregistration(
            args.repo_root,
            raw_manifest_path=args.raw_manifest,
            reconstructed_manifest_path=args.reconstructed_manifest,
            tardis_sample_manifest_path=args.tardis_sample_manifest,
            spread_calibration_manifest_path=args.spread_calibration_manifest,
            precalibration_artifact_path=args.precalibration_artifact,
            scenario_id=args.scenario_id,
            stress_selector_input_path=args.stress_selector_input,
            out_dir=args.out,
        )
        artifact = load_preregistration(dest)
        result = verify_preregistration(
            artifact,
            args.repo_root,
            scenario_id=args.scenario_id,
            stress_selector_input_path=args.stress_selector_input,
            precalibration_artifact_path=args.precalibration_artifact,
        )
    except PreregistrationError as exc:
        print(f"VRP-free pre-registration error: {exc}", file=sys.stderr)
        return 1

    print(f"wrote VRP-free pre-registration: {dest}")
    print(f"run_id: {artifact['run_id']}")
    print(f"content_hash: {artifact['content_hash']}")
    print(json.dumps(result.as_dict(), indent=2, sort_keys=True))
    return 0 if result.valid else 1


if __name__ == "__main__":
    raise SystemExit(main())

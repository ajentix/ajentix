#!/usr/bin/env python3
"""Verify a VRP-free pre-registration artifact against the current repo state."""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from ajentix_quant.research.vrp_free_preregistration import (  # noqa: E402
    DEFAULT_SCENARIO_ID,
    PreregistrationError,
    load_preregistration,
    verify_preregistration,
)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Verify a VRP-free pre-registration artifact.")
    parser.add_argument("--path", required=True, help="Path to the vrp-free-*.json artifact")
    parser.add_argument("--raw-source-root", help="Override raw-source cache root.")
    parser.add_argument("--reconstructed-cache-root", help="Override reconstructed cache root.")
    parser.add_argument("--tardis-sample-root", help="Override Tardis sample root.")
    parser.add_argument("--spread-calibration-root", help="Override spread calibration root.")
    parser.add_argument("--scenario-id", default=DEFAULT_SCENARIO_ID)
    parser.add_argument("--stress-selector-input", help="Override stress selector-input path.")
    parser.add_argument("--precalibration-artifact", help="Override pre-calibration artifact path.")
    parser.add_argument(
        "--repo-root",
        default=str(Path(__file__).resolve().parents[1]),
        help="Repository root (defaults to the checkout containing this script).",
    )
    args = parser.parse_args(argv)

    try:
        artifact = load_preregistration(args.path)
    except PreregistrationError as exc:
        print(json.dumps({"error": str(exc)}, indent=2, sort_keys=True), file=sys.stderr)
        return 1

    result = verify_preregistration(
        artifact,
        args.repo_root,
        raw_source_root=args.raw_source_root,
        reconstructed_cache_root=args.reconstructed_cache_root,
        tardis_sample_root=args.tardis_sample_root,
        spread_calibration_root=args.spread_calibration_root,
        scenario_id=args.scenario_id,
        stress_selector_input_path=args.stress_selector_input,
        precalibration_artifact_path=args.precalibration_artifact,
    )
    print(json.dumps(result.as_dict(), indent=2, sort_keys=True))
    return 0 if result.valid else 1


if __name__ == "__main__":
    raise SystemExit(main())

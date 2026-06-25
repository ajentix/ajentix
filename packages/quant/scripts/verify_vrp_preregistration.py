#!/usr/bin/env python3
"""Verify a VRP pre-registration artifact against the current repo state.

Recomputes source hashes, settings, frozen VRP plan constants, raw-source manifests,
normalized cache manifests, stress selector inputs, content_hash, and run_id. Exit code 0
only when every frozen field still matches.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from ajentix_quant.research.vrp_preregistration import (  # noqa: E402
    DEFAULT_SCENARIO_ID,
    PreregistrationError,
    load_preregistration,
    verify_preregistration,
)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Verify a VRP pre-registration artifact.")
    parser.add_argument("--path", required=True, help="Path to docs/preregistration/vrp-*.json")
    parser.add_argument("--raw-cache-root", help="Override raw-source cache root for verification.")
    parser.add_argument("--cache-root", help="Override normalized option-cache root.")
    parser.add_argument("--scenario-id", default=DEFAULT_SCENARIO_ID)
    parser.add_argument(
        "--stress-selector-input",
        help="Override stress selector-input manifest path for verification.",
    )
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
        raw_cache_root=args.raw_cache_root,
        cache_root=args.cache_root,
        scenario_id=args.scenario_id,
        stress_selector_input_path=args.stress_selector_input,
    )
    print(json.dumps(result.as_dict(), indent=2, sort_keys=True))
    return 0 if result.valid else 1


if __name__ == "__main__":
    raise SystemExit(main())

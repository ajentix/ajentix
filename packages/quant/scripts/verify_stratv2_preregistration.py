#!/usr/bin/env python3
"""Verify a strategy-v2 pre-registration artifact against the current repo state.

Recomputes every frozen hash (source code, settings, plan constants, cache manifests,
content_hash, run_id) and reports run_status=valid|invalid. Exit code 0 if valid, 1 if any
frozen field drifted. Downstream runners use the same verify path to refuse a GO on drift.

Usage:
    python scripts/verify_stratv2_preregistration.py --path docs/preregistration/<run_id>.json
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from ajentix_quant.research.preregistration import (  # noqa: E402
    load_preregistration,
    verify_preregistration,
)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Verify a strategy-v2 pre-registration artifact.")
    parser.add_argument("--path", required=True, help="Path to docs/preregistration/<run_id>.json")
    parser.add_argument(
        "--repo-root",
        default=str(Path(__file__).resolve().parents[1]),
        help="Repository root (defaults to the checkout containing this script).",
    )
    args = parser.parse_args(argv)

    artifact = load_preregistration(args.path)
    result = verify_preregistration(artifact, args.repo_root)
    print(f"run_id: {artifact.get('run_id')}")
    print(f"run_status: {result.run_status}")
    if not result.valid:
        for m in result.mismatches:
            print(f"  mismatch: {m}", file=sys.stderr)
        return 1
    print("pre-registration is valid: all frozen fields match the current repo state.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

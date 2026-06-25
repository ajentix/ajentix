#!/usr/bin/env python3
"""Create the deterministic VRP-free pre-calibration governance artifact.

This Phase-0 artifact is intentionally emittable before any spread calibration output
exists. It freezes the cost-budget constants that later calibration must cite by hash.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from ajentix_quant.research.vrp_free_preregistration import (  # noqa: E402
    DEFAULT_PRECALIBRATION_OUT_DIR,
    build_precalibration_artifact,
    write_precalibration_artifact,
)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Create the VRP-free pre-calibration governance artifact."
    )
    parser.add_argument("--dry-run", action="store_true", help="Build and print; do not write.")
    parser.add_argument("--out", default=DEFAULT_PRECALIBRATION_OUT_DIR)
    parser.add_argument(
        "--repo-root",
        default=str(Path(__file__).resolve().parents[1]),
        help="Repository root (defaults to the checkout containing this script).",
    )
    args = parser.parse_args(argv)

    first = build_precalibration_artifact()
    second = build_precalibration_artifact()
    if first != second:
        print("VRP-free pre-calibration build is not deterministic", file=sys.stderr)
        return 1

    if args.dry_run:
        print(json.dumps(first, indent=2, sort_keys=True))
        return 0

    dest = write_precalibration_artifact(args.repo_root, out_dir=args.out)
    print(f"wrote VRP-free pre-calibration artifact: {dest}")
    print(f"precalibration_config_sha256: {first['precalibration_config_sha256']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

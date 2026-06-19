#!/usr/bin/env python3
"""Create + commit the immutable strategy-v2 pre-registration artifact.

Run BEFORE any breakeven / walk-forward / held-out / pivot-feasibility output. Writes
docs/preregistration/<run_id>.json where run_id is derived from a hash over every frozen
field (code, settings, folds, grid, caches, thresholds). Deterministic + read-only.

Usage:
    python scripts/create_stratv2_preregistration.py [--cache-root data/cache/bybit]
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
    write_preregistration,
)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Create the strategy-v2 pre-registration artifact."
    )
    parser.add_argument("--cache-root", default="data/cache/bybit")
    parser.add_argument(
        "--repo-root",
        default=str(Path(__file__).resolve().parents[1]),
        help="Repository root (defaults to the checkout containing this script).",
    )
    args = parser.parse_args(argv)

    dest = write_preregistration(args.repo_root, cache_root=args.cache_root)
    artifact = load_preregistration(dest)
    result = verify_preregistration(artifact, args.repo_root)

    print(f"wrote pre-registration: {dest}")
    print(f"run_id: {artifact['run_id']}")
    print(f"content_hash: {artifact['content_hash']}")
    print(f"self-verify run_status: {result.run_status}")
    if not result.valid:
        for m in result.mismatches:
            print(f"  mismatch: {m}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

#!/usr/bin/env python3
"""Create the immutable VRP pre-registration artifact, or dry-run schema mode.

Phase 0 supports deterministic schema-only builds without writing any file. Final emission is
reserved for Phase 2 and requires completed raw-source and normalized cache manifests.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from ajentix_quant.research.vrp_preregistration import (  # noqa: E402
    DEFAULT_CACHE_ROOT,
    DEFAULT_RAW_CACHE_ROOT,
    DEFAULT_SCENARIO_ID,
    PreregistrationError,
    build_preregistration,
    load_preregistration,
    verify_preregistration,
    write_preregistration,
)


def _resolve(repo_root: str | Path, path: str | Path) -> Path:
    p = Path(path)
    return p if p.is_absolute() else Path(repo_root) / p


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Create the VRP pre-registration artifact.")
    parser.add_argument("--dry-run", action="store_true", help="Build and print; do not write.")
    parser.add_argument(
        "--schema-only",
        action="store_true",
        help="Alias for a dry-run schema build with MISSING manifests allowed.",
    )
    parser.add_argument(
        "--raw-manifest",
        help="Phase 2 raw manifest path: <root>/<scenario>/manifest.json",
    )
    parser.add_argument(
        "--normalized-manifest",
        help="Phase 2 normalized cache manifest path: <root>/<scenario>/manifest.json",
    )
    parser.add_argument("--scenario-id", default=DEFAULT_SCENARIO_ID)
    parser.add_argument("--stress-rule", help="Phase 2 stress selector/rule manifest path.")
    parser.add_argument(
        "--folds-config",
        help="Optional external folds config path for Phase 2 operators.",
    )
    parser.add_argument("--out", help="Output directory for final Phase 2 artifact emission.")
    parser.add_argument("--raw-cache-root", default=DEFAULT_RAW_CACHE_ROOT)
    parser.add_argument("--cache-root", default=DEFAULT_CACHE_ROOT)
    parser.add_argument(
        "--repo-root",
        default=str(Path(__file__).resolve().parents[1]),
        help="Repository root (defaults to the checkout containing this script).",
    )
    args = parser.parse_args(argv)

    if args.dry_run or args.schema_only:
        first = build_preregistration(
            args.repo_root,
            raw_cache_root=args.raw_cache_root,
            cache_root=args.cache_root,
            scenario_id=args.scenario_id,
        )
        second = build_preregistration(
            args.repo_root,
            raw_cache_root=args.raw_cache_root,
            cache_root=args.cache_root,
            scenario_id=args.scenario_id,
        )
        if first != second:
            print("VRP pre-registration build is not deterministic", file=sys.stderr)
            return 1
        result = verify_preregistration(
            first,
            args.repo_root,
            raw_cache_root=args.raw_cache_root,
            cache_root=args.cache_root,
            scenario_id=args.scenario_id,
        )
        if not result.valid:
            print(
                json.dumps(result.as_dict(), indent=2, sort_keys=True), file=sys.stderr
            )
            return 1
        print(json.dumps(first, indent=2, sort_keys=True))
        return 0

    missing = [
        flag
        for flag, value in (
            ("--raw-manifest", args.raw_manifest),
            ("--normalized-manifest", args.normalized_manifest),
            ("--stress-rule", args.stress_rule),
            ("--out", args.out),
        )
        if not value
    ]
    if missing:
        parser.error(f"full VRP artifact emission requires {', '.join(missing)}")

    stress_rule = _resolve(args.repo_root, args.stress_rule)
    if not stress_rule.is_file():
        parser.error(f"--stress-rule does not exist: {stress_rule}")

    try:
        dest = write_preregistration(
            args.repo_root,
            raw_manifest_path=args.raw_manifest,
            normalized_manifest_path=args.normalized_manifest,
            scenario_id=args.scenario_id,
            stress_selector_input_path=args.stress_rule,
            out_dir=args.out,
        )
        artifact = load_preregistration(dest)
        result = verify_preregistration(
            artifact, args.repo_root, scenario_id=args.scenario_id
        )
    except PreregistrationError as exc:
        print(f"VRP pre-registration error: {exc}", file=sys.stderr)
        return 1

    print(f"wrote VRP pre-registration: {dest}")
    print(f"run_id: {artifact['run_id']}")
    print(f"content_hash: {artifact['content_hash']}")
    print(json.dumps(result.as_dict(), indent=2, sort_keys=True))
    return 0 if result.valid else 1


if __name__ == "__main__":
    raise SystemExit(main())

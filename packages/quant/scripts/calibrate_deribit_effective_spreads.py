#!/usr/bin/env python3
"""Calibrate VRP-free effective spreads from local Deribit-history raw cache."""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from ajentix_quant.data.deribit_history_effective_spread_calibration import (  # noqa: E402
    DEFAULT_EFFECTIVE_SPREAD_CALIBRATION_ROOT,
    EFFECTIVE_SPREAD_SCHEMA_VERSION,
    SPREAD_BINS_FILE,
    load_effective_spread_calibration_manifest,
    sha256_text,
    write_effective_spread_calibration_cache,
)
from ajentix_quant.data.vrp_free_history_cache import load_vrp_free_history_cache  # noqa: E402
from ajentix_quant.research.vrp_free_preregistration import (  # noqa: E402
    DEFAULT_RAW_SOURCE_ROOT,
    DEFAULT_SCENARIO_ID,
    precalibration_config_sha256,
)


def _resolve(repo_root: Path, path: str | Path) -> Path:
    p = Path(path)
    return p if p.is_absolute() else repo_root / p


def _optional_int(value: str | None) -> int | None:
    if value is None or value == "":
        return None
    return int(value)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Calibrate VRP-free effective spread quantiles from the local G002 "
            "Deribit-history raw cache. No network is used."
        )
    )
    parser.add_argument("--raw-source-root", default=DEFAULT_RAW_SOURCE_ROOT)
    parser.add_argument(
        "--effective-spread-calibration-root",
        default=DEFAULT_EFFECTIVE_SPREAD_CALIBRATION_ROOT,
    )
    parser.add_argument("--scenario-id", default=DEFAULT_SCENARIO_ID)
    parser.add_argument(
        "--min-sample-timestamp-ms",
        help=(
            "Optional explicit warmup cutoff. Trades earlier than this observed timestamp are "
            "excluded before requiring 30d index lookback for included calibration samples."
        ),
    )
    parser.add_argument("--json", action="store_true", help="Print a JSON result payload.")
    parser.add_argument(
        "--repo-root",
        default=str(Path(__file__).resolve().parents[1]),
        help="Repository root (defaults to the checkout containing this script).",
    )
    args = parser.parse_args(argv)

    repo_root = Path(args.repo_root).resolve()
    raw_source_root = _resolve(repo_root, args.raw_source_root)
    cache_root = _resolve(repo_root, args.effective_spread_calibration_root)
    min_timestamp_ms = _optional_int(args.min_sample_timestamp_ms)

    dataset = load_vrp_free_history_cache(raw_source_root, args.scenario_id)
    scenario_dir = write_effective_spread_calibration_cache(
        cache_root,
        args.scenario_id,
        trades=dataset.trades,
        index_path=dataset.index_path,
        raw_source_manifest=dataset.manifest,
        precalibration_config_sha256=precalibration_config_sha256(),
        min_timestamp_ms=min_timestamp_ms,
    )
    manifest = load_effective_spread_calibration_manifest(cache_root, args.scenario_id)
    manifest_text = (scenario_dir / "manifest.json").read_text(encoding="utf-8")
    payload = {
        "schema_version": EFFECTIVE_SPREAD_SCHEMA_VERSION,
        "scenario_id": args.scenario_id,
        "effective_spread_calibration_dir": scenario_dir.as_posix(),
        "spread_bins_file": (scenario_dir / SPREAD_BINS_FILE).as_posix(),
        "manifest_path": (scenario_dir / "manifest.json").as_posix(),
        "manifest_sha256": sha256_text(manifest_text),
        "precalibration_config_sha256": manifest["precalibration_config_sha256"],
        "row_counts": manifest["row_counts"],
        "spread_basis": manifest["spread_basis"],
        "effective_spread_source_quality": manifest["effective_spread_source_quality"],
        "authorizing": manifest["authorizing"],
        "capital_go_allowed": manifest["capital_go_allowed"],
        "network_attempted": False,
        "cache_fabricated": False,
    }

    if args.json:
        print(json.dumps(payload, indent=2, sort_keys=True))
    else:
        print(f"wrote={scenario_dir / 'manifest.json'}")
        print(f"precalibration_config_sha256={payload['precalibration_config_sha256']}")
        print(f"spread_basis={payload['spread_basis']}")
        print(f"resolved_bins={manifest['row_counts']['resolved_bins']}")
        print(f"inconclusive_bins={manifest['row_counts']['inconclusive_bins']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

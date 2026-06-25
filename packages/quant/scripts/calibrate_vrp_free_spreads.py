#!/usr/bin/env python3
"""Calibrate VRP-free spread bins from local Tardis FREE options_chain CSV samples."""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from typing import Any

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from ajentix_quant.data.tardis_free_spread_calibration import (  # noqa: E402
    SCHEMA_VERSION,
    SPREAD_BINS_FILE,
    load_spread_calibration_manifest,
    sha256_text,
    write_spread_calibration_cache,
)
from ajentix_quant.research.vrp_free_preregistration import (  # noqa: E402
    DEFAULT_PRECALIBRATION_OUT_DIR,
    DEFAULT_SCENARIO_ID,
    DEFAULT_SPREAD_CALIBRATION_ROOT,
    PreregistrationError,
    load_precalibration_artifact,
    precalibration_config_sha256,
)


def _resolve(repo_root: Path, path: str | Path) -> Path:
    p = Path(path)
    return p if p.is_absolute() else repo_root / p


def _default_precalibration_artifact(repo_root: Path) -> Path:
    return (
        repo_root
        / DEFAULT_PRECALIBRATION_OUT_DIR
        / f"vrp-free-precalibration-{precalibration_config_sha256()[:12]}.json"
    )


def require_matching_precalibration_artifact(path: str | Path) -> dict[str, Any]:
    """Load the Phase-0 artifact and require its frozen config hash to match code."""

    artifact = load_precalibration_artifact(path)
    expected = precalibration_config_sha256()
    observed = artifact.get("precalibration_config_sha256")
    if observed != expected:
        raise PreregistrationError(
            "VRP-free spread calibration refused: pre-calibration config hash "
            f"{observed!r} != {expected!r}"
        )
    return artifact


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Calibrate VRP-free spread quantiles from local Tardis FREE options_chain CSV "
            "samples. No network is used."
        )
    )
    parser.add_argument(
        "--input-csv",
        action="append",
        required=True,
        help="Local Tardis FREE Deribit options_chain CSV sample. Repeat for many files.",
    )
    parser.add_argument(
        "--precalibration-artifact",
        help="Path to vrp-free-precalibration-<hash>.json. Defaults to the current hash name.",
    )
    parser.add_argument("--spread-calibration-root", default=DEFAULT_SPREAD_CALIBRATION_ROOT)
    parser.add_argument("--scenario-id", default=DEFAULT_SCENARIO_ID)
    parser.add_argument("--json", action="store_true", help="Print a JSON result payload.")
    parser.add_argument(
        "--repo-root",
        default=str(Path(__file__).resolve().parents[1]),
        help="Repository root (defaults to the checkout containing this script).",
    )
    args = parser.parse_args(argv)

    repo_root = Path(args.repo_root).resolve()
    artifact_path = (
        _resolve(repo_root, args.precalibration_artifact)
        if args.precalibration_artifact
        else _default_precalibration_artifact(repo_root)
    )
    require_matching_precalibration_artifact(artifact_path)

    input_paths = [_resolve(repo_root, value) for value in args.input_csv]
    cache_root = _resolve(repo_root, args.spread_calibration_root)
    scenario_dir = write_spread_calibration_cache(
        cache_root,
        args.scenario_id,
        csv_paths=input_paths,
        precalibration_config_sha256=precalibration_config_sha256(),
    )
    manifest = load_spread_calibration_manifest(cache_root, args.scenario_id)
    manifest_text = (scenario_dir / "manifest.json").read_text(encoding="utf-8")
    payload = {
        "schema_version": SCHEMA_VERSION,
        "scenario_id": args.scenario_id,
        "spread_calibration_dir": scenario_dir.as_posix(),
        "spread_bins_file": (scenario_dir / SPREAD_BINS_FILE).as_posix(),
        "manifest_path": (scenario_dir / "manifest.json").as_posix(),
        "manifest_sha256": sha256_text(manifest_text),
        "precalibration_config_sha256": manifest["precalibration_config_sha256"],
        "row_counts": manifest["row_counts"],
        "network_attempted": False,
        "cache_fabricated": False,
    }

    if args.json:
        print(json.dumps(payload, indent=2, sort_keys=True))
    else:
        print(f"wrote={scenario_dir / 'manifest.json'}")
        print(f"precalibration_config_sha256={payload['precalibration_config_sha256']}")
        print(f"resolved_bins={manifest['row_counts']['resolved_bins']}")
        print(f"inconclusive_bins={manifest['row_counts']['inconclusive_bins']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

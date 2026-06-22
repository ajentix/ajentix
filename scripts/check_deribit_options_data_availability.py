#!/usr/bin/env python3
"""Inspect Deribit option cache readiness without touching economics."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from ajentix_quant.adapters.base import SourceQuality  # noqa: E402
from ajentix_quant.data.options_cache import (  # noqa: E402
    OptionsCacheValidationError,
    load_normalized_manifest,
    load_raw_source_manifest,
)
from ajentix_quant.research.vrp_preregistration import (  # noqa: E402
    DEFAULT_SCENARIO_ID,
    PLAN_COVERAGE_WINDOW,
    PLAN_FOLDS,
)

SCHEMA_VERSION = "vrp-data-availability-report-v1"
STATUS_READY = "READY_FOR_PREREGISTRATION"
STATUS_BLOCKED = "INCONCLUSIVE_DATA_BLOCKER"
REPORT_STEM = "vrp_data_availability"


def _parse_iso_ms(value: str) -> int:
    normalized = value[:-1] + "+00:00" if value.endswith("Z") else value
    dt = datetime.fromisoformat(normalized)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return int(dt.timestamp() * 1000)


def _sha256_file(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _resolve(repo_root: Path, path: str | Path) -> Path:
    p = Path(path)
    return p if p.is_absolute() else repo_root / p


def _manifest_path(root: Path, scenario_id: str) -> Path:
    return root / scenario_id / "manifest.json"


def _load_manifest_pair(
    raw_root: Path,
    normalized_root: Path,
    scenario_id: str,
) -> tuple[dict[str, Any] | None, dict[str, Any] | None, list[str], dict[str, str]]:
    reasons: list[str] = []
    errors: dict[str, str] = {}
    raw_manifest: dict[str, Any] | None = None
    normalized_manifest: dict[str, Any] | None = None

    try:
        raw_manifest = load_raw_source_manifest(raw_root, scenario_id)
    except (OSError, OptionsCacheValidationError, ValueError) as exc:
        reasons.append("RAW_MANIFEST_INVALID_OR_MISSING")
        errors["raw_manifest"] = str(exc)

    try:
        normalized_manifest = load_normalized_manifest(normalized_root, scenario_id)
    except (OSError, OptionsCacheValidationError, ValueError) as exc:
        reasons.append("NORMALIZED_CACHE_INVALID_OR_MISSING")
        errors["normalized_manifest"] = str(exc)

    return raw_manifest, normalized_manifest, reasons, errors


def _quality_values(manifest: Mapping[str, Any] | None) -> dict[str, str]:
    if not manifest:
        return {}
    source_quality = manifest.get("source_quality")
    if not isinstance(source_quality, Mapping):
        return {}
    return {str(key): str(value) for key, value in source_quality.items()}


def _source_quality_authorizing(*manifests: Mapping[str, Any] | None) -> bool:
    values: list[str] = []
    for manifest in manifests:
        values.extend(_quality_values(manifest).values())
    return bool(values) and all(value == SourceQuality.VENUE.value for value in values)


def _date_range_covers(
    manifest: Mapping[str, Any] | None,
    start_ms: int,
    end_ms: int,
) -> bool:
    if not manifest:
        return False
    date_range = manifest.get("date_range")
    if not isinstance(date_range, Mapping):
        return False
    try:
        return int(date_range["start_ts_ms"]) <= start_ms and int(date_range["end_ts_ms"]) >= end_ms
    except (KeyError, TypeError, ValueError):
        return False


def _timestamps_cover(
    timestamps: Sequence[Any],
    start_ms: int,
    end_ms: int,
) -> bool:
    try:
        values = [int(value) for value in timestamps]
    except (TypeError, ValueError):
        return False
    return bool(values) and min(values) <= start_ms and max(values) >= end_ms


def _coverage_values(manifest: Mapping[str, Any] | None, symbol: str) -> list[Any]:
    if not manifest:
        return []
    coverage = manifest.get("coverage_timestamps_ms")
    if not isinstance(coverage, Mapping):
        return []
    raw = coverage.get(symbol.upper()) or coverage.get(symbol)
    return list(raw) if isinstance(raw, Sequence) and not isinstance(raw, str | bytes) else []


def _fold_coverage_ok(manifest: Mapping[str, Any] | None, symbol: str) -> bool:
    timestamps = _coverage_values(manifest, symbol)
    if not timestamps:
        return False
    for fold in PLAN_FOLDS:
        train_start = _parse_iso_ms(str(fold["train_start"]))
        test_end = _parse_iso_ms(str(fold["test_end"]))
        if not _timestamps_cover(timestamps, train_start, test_end):
            return False
    return True


def _stress_input_coverage_ok(manifest: Mapping[str, Any] | None) -> bool:
    if not manifest:
        return False
    coverage = manifest.get("stress_selector_input_coverage")
    if not isinstance(coverage, Mapping) or not coverage:
        return False
    for value in coverage.values():
        if isinstance(value, Sequence) and not isinstance(value, str | bytes) and value:
            return True
    return False


def _manifest_sha(root: Path, scenario_id: str) -> str | None:
    path = _manifest_path(root, scenario_id)
    return _sha256_file(path) if path.is_file() else None


def _raw_manifest_summary(
    manifest: Mapping[str, Any] | None,
    raw_root: Path,
    scenario_id: str,
) -> dict[str, Any]:
    if not manifest:
        return {"path": _manifest_path(raw_root, scenario_id).as_posix(), "present": False}
    return {
        "path": _manifest_path(raw_root, scenario_id).as_posix(),
        "present": True,
        "sha256": _manifest_sha(raw_root, scenario_id),
        "schema_version": manifest.get("schema_version"),
        "manifest_kind": manifest.get("manifest_kind"),
        "currency": manifest.get("currency"),
        "date_range": manifest.get("date_range"),
        "download_timestamp_ms": manifest.get("download_timestamp_ms"),
        "source_ids": manifest.get("source_ids", []),
        "source_uri_ids": manifest.get("source_uri_ids", []),
        "license_budget_note": manifest.get("license_budget_note"),
        "acquisition_tool_version": manifest.get("acquisition_tool_version"),
        "source_quality": _quality_values(manifest),
        "file_sizes": manifest.get("file_sizes", {}),
        "row_counts": manifest.get("row_counts", {}),
    }


def _normalized_manifest_summary(
    manifest: Mapping[str, Any] | None,
    cache_root: Path,
    scenario_id: str,
) -> dict[str, Any]:
    if not manifest:
        return {"path": _manifest_path(cache_root, scenario_id).as_posix(), "present": False}
    return {
        "path": _manifest_path(cache_root, scenario_id).as_posix(),
        "present": True,
        "sha256": _manifest_sha(cache_root, scenario_id),
        "schema_version": manifest.get("schema_version"),
        "manifest_kind": manifest.get("manifest_kind"),
        "scenario_id": manifest.get("scenario_id"),
        "exchange": manifest.get("exchange"),
        "date_range": manifest.get("date_range"),
        "source_ids": manifest.get("source_ids", []),
        "source_quality": _quality_values(manifest),
        "non_authorizing_source_quality_keys": manifest.get(
            "non_authorizing_source_quality_keys", []
        ),
        "row_counts": manifest.get("row_counts", {}),
        "required_column_coverage": manifest.get("required_column_coverage", {}),
        "coverage_timestamps_ms": manifest.get("coverage_timestamps_ms", {}),
        "fold_coverage_timestamps_ms": manifest.get("fold_coverage_timestamps_ms", {}),
        "settlement_index_coverage": manifest.get("settlement_index_coverage", {}),
        "min_ticket_metadata": manifest.get("min_ticket_metadata", {}),
        "bid_ask_availability_stats": manifest.get("bid_ask_availability_stats", {}),
        "stress_selector_input_coverage": manifest.get("stress_selector_input_coverage", {}),
        "transform_version": manifest.get("transform_version"),
        "raw_manifest_sha256": manifest.get("raw_manifest_sha256"),
    }


def _build_payload(args: argparse.Namespace) -> dict[str, Any]:
    repo_root = Path(args.repo_root).resolve()
    raw_root = _resolve(repo_root, args.raw_cache_root)
    cache_root = _resolve(repo_root, args.cache_root)
    symbol = "ETH"

    raw_manifest, normalized_manifest, reasons, errors = _load_manifest_pair(
        raw_root,
        cache_root,
        str(args.scenario_id),
    )
    coverage_start = _parse_iso_ms(PLAN_COVERAGE_WINDOW[0])
    coverage_end = _parse_iso_ms(PLAN_COVERAGE_WINDOW[1])

    if not _source_quality_authorizing(raw_manifest, normalized_manifest):
        reasons.append("SOURCE_QUALITY_NOT_FULL_REAL_CHAIN")
    if not _date_range_covers(raw_manifest, coverage_start, coverage_end):
        reasons.append("RAW_DATE_RANGE_INCOMPLETE")
    if not _date_range_covers(normalized_manifest, coverage_start, coverage_end):
        reasons.append("NORMALIZED_DATE_RANGE_INCOMPLETE")
    if not _fold_coverage_ok(normalized_manifest, symbol):
        reasons.append("FOLD_TIMESTAMP_COVERAGE_INCOMPLETE")
    if not _stress_input_coverage_ok(normalized_manifest):
        reasons.append("STRESS_SELECTOR_INPUT_COVERAGE_MISSING")

    unique_reasons = tuple(dict.fromkeys(reasons))
    status = STATUS_READY if not unique_reasons else STATUS_BLOCKED
    return {
        "schema_version": SCHEMA_VERSION,
        "run_status": "valid",
        "status": status,
        "reason_codes": list(unique_reasons),
        "scenario_id": args.scenario_id,
        "cache_root": cache_root.as_posix(),
        "raw_cache_root": raw_root.as_posix(),
        "checks": {
            "raw_manifest_reproducible": raw_manifest is not None,
            "normalized_manifest_reproducible": normalized_manifest is not None,
            "source_quality_authorizing": _source_quality_authorizing(
                raw_manifest,
                normalized_manifest,
            ),
            "raw_date_range_covers_plan": _date_range_covers(
                raw_manifest,
                coverage_start,
                coverage_end,
            ),
            "normalized_date_range_covers_plan": _date_range_covers(
                normalized_manifest,
                coverage_start,
                coverage_end,
            ),
            "fold_timestamp_coverage_complete": _fold_coverage_ok(normalized_manifest, symbol),
            "stress_selector_input_coverage_present": _stress_input_coverage_ok(
                normalized_manifest
            ),
        },
        "errors": errors,
        "raw_manifest": _raw_manifest_summary(raw_manifest, raw_root, str(args.scenario_id)),
        "normalized_manifest": _normalized_manifest_summary(
            normalized_manifest,
            cache_root,
            str(args.scenario_id),
        ),
    }


def _markdown(payload: Mapping[str, Any]) -> str:
    lines = [
        "# VRP data availability",
        "",
        f"- run_status: {payload['run_status']}",
        f"- status: {payload['status']}",
        f"- scenario_id: {payload['scenario_id']}",
        f"- reason_codes: {', '.join(payload['reason_codes']) or '-'}",
        "",
        "## No-economics inspection boundary",
        "",
        "This report contains cache/schema/source-quality/coverage facts only. It does not "
        "compute or report premium, PnL, IV/RV edge, Sharpe, drawdown, branch decisions, "
        "structure ranking, or stress performance.",
        "",
        "## Checks",
        "",
    ]
    checks = payload.get("checks", {})
    if isinstance(checks, Mapping):
        for key, value in checks.items():
            lines.append(f"- {key}: {value}")
    return "\n".join(lines) + "\n"


def _write_reports(repo_root: Path, reports_dir: str | Path, payload: dict[str, Any]) -> list[Path]:
    out_dir = _resolve(repo_root, reports_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    json_path = out_dir / f"{REPORT_STEM}.json"
    md_path = out_dir / f"{REPORT_STEM}.md"
    json_path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    md_path.write_text(_markdown(payload), encoding="utf-8")
    return [json_path, md_path]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Inspect Deribit option-cache data availability without economics."
    )
    parser.add_argument("--cache-root", required=True)
    parser.add_argument("--raw-cache-root", required=True)
    parser.add_argument("--scenario-id", default=DEFAULT_SCENARIO_ID)
    parser.add_argument("--reports-dir", default="reports")
    parser.add_argument("--json", action="store_true", help="Print the report payload as JSON.")
    parser.add_argument(
        "--repo-root",
        default=str(Path(__file__).resolve().parents[1]),
        help="Repository root (defaults to the checkout containing this script).",
    )
    args = parser.parse_args(argv)

    repo_root = Path(args.repo_root).resolve()
    payload = _build_payload(args)
    paths = _write_reports(repo_root, args.reports_dir, payload)

    if args.json:
        print(json.dumps(payload, indent=2, sort_keys=True))
    else:
        print(f"run_status={payload['run_status']}")
        print(f"status={payload['status']}")
        print(f"reason_codes={','.join(payload['reason_codes']) or '-'}")
        for path in paths:
            print(f"wrote={path.relative_to(repo_root)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

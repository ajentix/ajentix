#!/usr/bin/env python3
"""Merge chunked free Deribit-history raw caches into one combined cache.

Chunked collection writes one cache per time window so a long (multi-month) collection
stays memory-bounded and resumable. This tool concatenates the *real observed* trade rows
from each chunk cache and re-writes a single combined cache through
:func:`write_vrp_free_history_cache`, which deduplicates by ``trade_id`` and re-runs every
fail-closed validation (sequence, index path, grid coverage, time gaps, manifest hashing).

No synthetic rows are ever introduced: every row written by this tool is a real observed
Deribit print already present in an input chunk cache. The combined window is derived from
the chunk manifests; source identifiers and source-quality are copied verbatim from the
first chunk manifest so the merged lineage stays faithful to the collected lineage.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from ajentix_quant.data.vrp_free_history_cache import (  # noqa: E402
    GENERATOR_VERSION,
    TRADES_FILE,
    write_vrp_free_history_cache,
)
from ajentix_quant.research.vrp_free_preregistration import DEFAULT_SCENARIO_ID  # noqa: E402


def _parse_iso_ms(value: str) -> int:
    from datetime import UTC, datetime

    normalized = value[:-1] + "+00:00" if value.endswith("Z") else value
    return int(datetime.fromisoformat(normalized).astimezone(UTC).timestamp() * 1000)


def _read_chunk_rows(chunk_dir: Path) -> list[dict[str, Any]]:
    trades_path = chunk_dir / TRADES_FILE
    if not trades_path.is_file():
        raise FileNotFoundError(f"missing chunk trades file: {trades_path}")
    rows: list[dict[str, Any]] = []
    with trades_path.open(encoding="utf-8") as handle:
        for line in handle:
            stripped = line.strip()
            if stripped:
                rows.append(json.loads(stripped))
    return rows


def _read_manifest(chunk_dir: Path) -> dict[str, Any]:
    manifest_path = chunk_dir / "manifest.json"
    if not manifest_path.is_file():
        raise FileNotFoundError(f"missing chunk manifest: {manifest_path}")
    return json.loads(manifest_path.read_text(encoding="utf-8"))


def merge_caches(
    *,
    chunk_roots: list[Path],
    out_root: Path,
    scenario_id: str,
    coverage_start_ts_ms: int,
    download_timestamp_ms: int,
) -> dict[str, Any]:
    """Concatenate chunk caches and re-write one combined cache. Returns a report dict."""

    if not chunk_roots:
        raise ValueError("at least one chunk root is required")
    all_rows: list[dict[str, Any]] = []
    starts: list[int] = []
    ends: list[int] = []
    first_manifest: dict[str, Any] | None = None
    per_chunk: list[dict[str, Any]] = []
    for root in chunk_roots:
        chunk_dir = root / scenario_id
        manifest = _read_manifest(chunk_dir)
        if first_manifest is None:
            first_manifest = manifest
        date_range = manifest["date_range"]
        starts.append(int(date_range["start_ts_ms"]))
        ends.append(int(date_range["end_ts_ms"]))
        rows = _read_chunk_rows(chunk_dir)
        all_rows.extend(rows)
        per_chunk.append({"root": root.as_posix(), "rows": len(rows)})

    assert first_manifest is not None
    overall_start = min(starts)
    overall_end = max(ends)
    source_quality = first_manifest.get(
        "source_quality", {"option_trades": "venue", "underlying_index": "venue"}
    )
    scenario_dir = write_vrp_free_history_cache(
        out_root,
        scenario_id,
        raw_rows=all_rows,
        currency="ETH",
        start_ts_ms=overall_start,
        coverage_start_ts_ms=coverage_start_ts_ms,
        end_ts_ms=overall_end,
        download_timestamp_ms=download_timestamp_ms,
        source_ids=list(first_manifest.get("source_ids", ["deribit-history:ETH:option-trades"])),
        source_url_ids=list(first_manifest.get("source_url_ids", [])),
        source_quality=source_quality,
        acquisition_tool_version=GENERATOR_VERSION,
    )
    out_manifest = _read_manifest(scenario_dir)
    return {
        "out_dir": scenario_dir.as_posix(),
        "input_rows_total": len(all_rows),
        "deduped_trade_rows": out_manifest["row_counts"]["trades"],
        "per_chunk": per_chunk,
        "date_range": out_manifest["date_range"],
        "cache_fabricated": out_manifest["cache_fabricated"],
    }


def main(argv: list[str] | None = None) -> int:
    from datetime import UTC, datetime

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--chunk-root",
        action="append",
        required=True,
        help="A chunk cache root (the directory containing <scenario_id>/). Repeatable.",
    )
    parser.add_argument("--out-root", required=True)
    parser.add_argument("--scenario-id", default=DEFAULT_SCENARIO_ID)
    parser.add_argument("--coverage-from", required=True, help="ISO8601 frozen coverage start.")
    args = parser.parse_args(argv)

    report = merge_caches(
        chunk_roots=[Path(root) for root in args.chunk_root],
        out_root=Path(args.out_root),
        scenario_id=args.scenario_id,
        coverage_start_ts_ms=_parse_iso_ms(args.coverage_from),
        download_timestamp_ms=int(datetime.now(tz=UTC).timestamp() * 1000),
    )
    print(json.dumps(report, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

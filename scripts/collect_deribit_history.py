#!/usr/bin/env python3
"""Collect free Deribit-history ETH option trades into aq-vrp-free-history-cache-v1."""

from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from ajentix_quant.adapters.base import SourceQuality  # noqa: E402
from ajentix_quant.adapters.deribit_history import DeribitHistoryTradeProvider  # noqa: E402
from ajentix_quant.data.vrp_free_history_cache import (  # noqa: E402
    GENERATOR_VERSION,
    SCHEMA_VERSION,
    sha256_text,
    write_vrp_free_history_cache,
)
from ajentix_quant.research.vrp_free_preregistration import DEFAULT_SCENARIO_ID  # noqa: E402

REPORT_SCHEMA_VERSION = "vrp-free-deribit-history-collect-report-v1"
STATUS_POPULATED = "POPULATED_DERIBIT_HISTORY_RAW_CACHE"
STATUS_CI_BLOCKED = "INCONCLUSIVE_CI_NETWORK_BLOCKED"
STATUS_DATA_BLOCKER = "INCONCLUSIVE_DATA_BLOCKER"
REPORT_STEM = "vrp_free_deribit_history_collection"


def _parse_iso_ms(value: str) -> int:
    normalized = value[:-1] + "+00:00" if value.endswith("Z") else value
    dt = datetime.fromisoformat(normalized)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return int(dt.astimezone(UTC).timestamp() * 1000)


def _now_ms() -> int:
    return int(datetime.now(tz=UTC).timestamp() * 1000)


def _resolve(repo_root: Path, path: str | Path) -> Path:
    p = Path(path)
    return p if p.is_absolute() else repo_root / p


def _base_payload(args: argparse.Namespace, status: str, reasons: list[str]) -> dict[str, Any]:
    return {
        "schema_version": REPORT_SCHEMA_VERSION,
        "cache_schema_version": SCHEMA_VERSION,
        "run_status": "valid" if status == STATUS_POPULATED else "invalid",
        "status": status,
        "reason_codes": list(dict.fromkeys(reasons)),
        "currency": args.currency.upper(),
        "scenario_id": args.scenario_id,
        "requested_date_range": {"from": args.start, "to": args.end},
        "network_attempted": False,
        "cache_fabricated": False,
        "cache_writes": [],
    }


def _blocked(
    args: argparse.Namespace,
    status: str,
    reasons: list[str],
    *,
    error: str | None = None,
    network_attempted: bool = False,
) -> dict[str, Any]:
    payload = _base_payload(args, status, reasons)
    payload["network_attempted"] = network_attempted
    if error:
        payload["error"] = error
    return payload


def _collect(args: argparse.Namespace, repo_root: Path) -> dict[str, Any]:
    if os.environ.get("CI"):
        return _blocked(
            args,
            STATUS_CI_BLOCKED,
            ["REFUSED_UNDER_CI"],
            error="collect_deribit_history is a manual network tool; run with env -u CI.",
        )

    start_ms = _parse_iso_ms(args.start)
    end_ms = _parse_iso_ms(args.end)
    if start_ms > end_ms:
        return _blocked(
            args,
            STATUS_DATA_BLOCKER,
            ["INVALID_DATE_RANGE"],
            error="--from must be <= --to",
        )

    provider = DeribitHistoryTradeProvider(rate_limit_s=args.rate_limit_s)
    try:
        rows = provider.fetch_option_trades(
            currency=args.currency.upper(),
            start_timestamp_ms=start_ms,
            end_timestamp_ms=end_ms,
            count=args.count,
            chunk_ms=int(args.chunk_hours * 60 * 60 * 1000),
        )
        raw_root = _resolve(repo_root, args.raw_source_root)
        scenario_dir = write_vrp_free_history_cache(
            raw_root,
            args.scenario_id,
            raw_rows=rows,
            currency=args.currency.upper(),
            start_ts_ms=start_ms,
            end_ts_ms=end_ms,
            download_timestamp_ms=_now_ms(),
            source_ids=[f"deribit-history:{args.currency.upper()}:option-trades"],
            source_url_ids=[provider.endpoint],
            source_quality={
                "option_trades": SourceQuality.VENUE,
                "underlying_index": SourceQuality.VENUE,
            },
            acquisition_tool_version=GENERATOR_VERSION,
        )
    except Exception as exc:  # noqa: BLE001 - collection boundary must fail closed
        return _blocked(
            args,
            STATUS_DATA_BLOCKER,
            ["DERIBIT_HISTORY_COLLECTION_FAILED"],
            error=str(exc),
            network_attempted=True,
        )

    manifest_text = (scenario_dir / "manifest.json").read_text(encoding="utf-8")
    payload = _base_payload(args, STATUS_POPULATED, ["DERIBIT_HISTORY_RAW_CACHE_WRITTEN"])
    payload.update(
        {
            "network_attempted": True,
            "trade_rows": len(rows),
            "cache_writes": [scenario_dir.as_posix()],
            "raw_manifest_sha256": sha256_text(manifest_text),
            "note": (
                "Cache contains observed Deribit public history trade rows only. Missing "
                "required IV/index/amount/grid coverage fails closed; no synthetic rows "
                "are written."
            ),
        }
    )
    return payload


def _markdown(payload: dict[str, Any]) -> str:
    lines = [
        "# VRP-free Deribit history collection",
        "",
        f"- run_status: {payload['run_status']}",
        f"- status: {payload['status']}",
        f"- currency: {payload['currency']}",
        f"- scenario_id: {payload['scenario_id']}",
        f"- network_attempted: {payload['network_attempted']}",
        f"- cache_fabricated: {payload['cache_fabricated']}",
        f"- reason_codes: {', '.join(payload['reason_codes']) or '-'}",
        "",
        "The collector refuses CI execution and never fabricates missing trades, IV, "
        "index prices, sizes, or grid coverage.",
        "",
    ]
    if payload.get("error"):
        lines.extend(["## Error", "", str(payload["error"]), ""])
    if payload.get("cache_writes"):
        lines.extend(["## Cache writes", ""])
        lines.extend(f"- {path}" for path in payload["cache_writes"])
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
        description="Collect free Deribit-history ETH option trades into a raw VRP cache."
    )
    parser.add_argument("--currency", default="ETH", choices=["ETH"])
    parser.add_argument("--from", dest="start", required=True, help="ISO8601 start timestamp")
    parser.add_argument("--to", dest="end", required=True, help="ISO8601 end timestamp")
    parser.add_argument("--scenario-id", default=DEFAULT_SCENARIO_ID)
    parser.add_argument("--raw-source-root", required=True)
    parser.add_argument("--reports-dir", default="reports")
    parser.add_argument("--count", type=int, default=1_000)
    parser.add_argument("--chunk-hours", type=float, default=1.0)
    parser.add_argument("--rate-limit-s", type=float, default=0.25)
    parser.add_argument("--json", action="store_true", help="Print report payload as JSON.")
    parser.add_argument(
        "--repo-root",
        default=str(Path(__file__).resolve().parents[1]),
        help="Repository root (defaults to the checkout containing this script).",
    )
    args = parser.parse_args(argv)

    repo_root = Path(args.repo_root).resolve()
    payload = _collect(args, repo_root)
    paths = _write_reports(repo_root, args.reports_dir, payload)

    if args.json:
        print(json.dumps(payload, indent=2, sort_keys=True))
    else:
        print(f"run_status={payload['run_status']}")
        print(f"status={payload['status']}")
        print(f"reason_codes={','.join(payload['reason_codes']) or '-'}")
        for path in paths:
            print(f"wrote={path.relative_to(repo_root)}")
    return 0 if payload["status"] == STATUS_POPULATED else 1


if __name__ == "__main__":
    raise SystemExit(main())

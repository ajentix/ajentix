#!/usr/bin/env python3
"""Populate Deribit option-cache data without ever fabricating missing history."""

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
from ajentix_quant.adapters.deribit_options import DeribitOptionsAdapter  # noqa: E402
from ajentix_quant.data.options_cache import (  # noqa: E402
    sha256_text,
    write_normalized_cache,
    write_raw_source_manifest,
)
from ajentix_quant.options.types import OptionChainSnapshot, OptionLeg  # noqa: E402
from ajentix_quant.research.vrp_preregistration import DEFAULT_SCENARIO_ID  # noqa: E402

SCHEMA_VERSION = "vrp-deribit-options-populate-report-v1"
STATUS_POPULATED = "POPULATED_NON_AUTHORIZING_CURRENT_PUBLIC_SNAPSHOT"
STATUS_BLOCKED = "INCONCLUSIVE_DATA_BLOCKER"
REPORT_STEM = "vrp_deribit_options_cache_population"
_SUPPORTED_PUBLIC_SOURCES = {"deribit", "deribit-public", "deribit_public"}


def _parse_iso_ms(value: str) -> int:
    normalized = value[:-1] + "+00:00" if value.endswith("Z") else value
    dt = datetime.fromisoformat(normalized)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return int(dt.timestamp() * 1000)


def _now_ms() -> int:
    return int(datetime.now(tz=UTC).timestamp() * 1000)


def _resolve(repo_root: Path, path: str | Path) -> Path:
    p = Path(path)
    return p if p.is_absolute() else repo_root / p


def _leg_payload(leg: OptionLeg) -> dict[str, Any]:
    return {
        "instrument_name": leg.instrument_name,
        "underlying": leg.underlying,
        "option_type": leg.option_type.value,
        "strike": leg.strike,
        "expiry_ms": leg.expiry_ms,
        "settlement_style": leg.settlement_style,
        "settlement_index": leg.settlement_index,
        "premium_currency": leg.premium_currency,
        "fee_currency": leg.fee_currency,
        "collateral_currency": leg.collateral_currency,
        "quote_ts_ms": leg.quote_ts_ms,
        "quote_age_s": leg.quote_age_s,
        "bid_price": leg.bid_price,
        "bid_amount": leg.bid_amount,
        "ask_price": leg.ask_price,
        "ask_amount": leg.ask_amount,
        "mark_price": leg.mark_price,
        "min_tick": leg.min_tick,
        "min_lot": leg.min_lot,
        "source_quality": leg.source_quality.value,
    }


def _snapshot_payload(snapshot: OptionChainSnapshot) -> dict[str, Any]:
    return {
        "underlying": snapshot.underlying,
        "exchange": snapshot.exchange,
        "snapshot_ts_ms": snapshot.snapshot_ts_ms,
        "source_ts_ms": snapshot.source_ts_ms,
        "source_id": snapshot.source_id,
        "scenario_id": snapshot.scenario_id,
        "settlement_index_price": snapshot.settlement_index_price,
        "index_price": snapshot.index_price,
        "usd_conversion_inputs": dict(snapshot.usd_conversion_inputs),
        "source_quality_map": {
            key: value.value for key, value in snapshot.source_quality_map.items()
        },
        "legs": [_leg_payload(leg) for leg in snapshot.legs],
    }


def _base_payload(args: argparse.Namespace, status: str, reasons: list[str]) -> dict[str, Any]:
    return {
        "schema_version": SCHEMA_VERSION,
        "run_status": "valid" if status == STATUS_POPULATED else "invalid",
        "status": status,
        "reason_codes": list(dict.fromkeys(reasons)),
        "source": args.source,
        "currency": args.currency.upper(),
        "scenario_id": args.scenario_id,
        "requested_date_range": {"from": args.start, "to": args.end},
        "cache_writes": [],
        "network_attempted": False,
        "cache_fabricated": False,
    }


def _blocked(
    args: argparse.Namespace,
    reasons: list[str],
    *,
    error: str | None = None,
    network_attempted: bool = False,
) -> dict[str, Any]:
    payload = _base_payload(args, STATUS_BLOCKED, reasons)
    payload["network_attempted"] = network_attempted
    if error:
        payload["error"] = error
    return payload


def _populate_public_current(args: argparse.Namespace, repo_root: Path) -> dict[str, Any]:
    start_ms = _parse_iso_ms(args.start)
    end_ms = _parse_iso_ms(args.end)
    if start_ms > end_ms:
        return _blocked(args, ["INVALID_DATE_RANGE"], error="--from must be <= --to")

    snapshot_ts_ms = _now_ms()
    if not (start_ms <= snapshot_ts_ms <= end_ms):
        return _blocked(
            args,
            ["PUBLIC_ADAPTER_IS_CURRENT_ONLY", "REQUESTED_HISTORICAL_WINDOW_NOT_FETCHABLE"],
            error=(
                "DeribitOptionsAdapter exposes current public option chains only; it cannot "
                "backfill the requested historical window, and this script will not fabricate it."
            ),
        )

    adapter = DeribitOptionsAdapter(
        source_id=f"{args.source}:{args.currency.upper()}",
        scenario_id=args.scenario_id,
        source_quality=SourceQuality.VENUE,
    )

    try:
        expiries = adapter.available_expiries(args.currency.upper())
        if not expiries:
            return _blocked(
                args,
                ["NO_DERIBIT_OPTION_EXPIRIES_RETURNED"],
                error="Deribit returned no option expiries for the requested currency.",
                network_attempted=True,
            )
        snapshots = tuple(
            adapter.chain_snapshot(args.currency.upper(), snapshot_ts_ms, expiry)
            for expiry in expiries
        )
    except Exception as exc:  # noqa: BLE001 - fail-closed network/tool boundary
        return _blocked(
            args,
            ["DERIBIT_PUBLIC_FETCH_FAILED"],
            error=str(exc),
            network_attempted=True,
        )

    raw_root = _resolve(repo_root, args.raw_cache_root)
    cache_root = _resolve(repo_root, args.cache_root)
    raw_text = json.dumps(
        {
            "source": args.source,
            "currency": args.currency.upper(),
            "snapshot_ts_ms": snapshot_ts_ms,
            "snapshots": [_snapshot_payload(snapshot) for snapshot in snapshots],
            "authorizing_note": (
                "Current public Deribit snapshot only; not full historical chain data and not "
                "sufficient for VRP pre-registration readiness."
            ),
        },
        sort_keys=True,
        separators=(",", ":"),
    ) + "\n"

    raw_dir = write_raw_source_manifest(
        raw_root,
        args.scenario_id,
        source_files={"deribit_public_current_options_snapshot.json": raw_text},
        source_ids=[f"{args.source}:{args.currency.upper()}"],
        currency=args.currency.upper(),
        start_ts_ms=snapshot_ts_ms,
        end_ts_ms=snapshot_ts_ms,
        download_timestamp_ms=snapshot_ts_ms,
        source_uri_ids=[f"deribit-public-current:{args.currency.upper()}:{snapshot_ts_ms}"],
        license_budget_note="Deribit public current-data endpoint; non-authorizing history gap.",
        acquisition_tool_version=SCHEMA_VERSION,
        source_quality={"option_chain": SourceQuality.VENUE},
    )
    raw_manifest_sha = sha256_text((raw_dir / "manifest.json").read_text(encoding="utf-8"))
    normalized_dir = write_normalized_cache(
        cache_root,
        args.scenario_id,
        snapshots=snapshots,
        source_ids=[f"{args.source}:{args.currency.upper()}"],
        raw_manifest_sha256=raw_manifest_sha,
        fold_coverage_timestamps_ms={},
        stress_selector_input_coverage={
            "underlying_index_timestamps_ms": [snapshot_ts_ms],
        },
    )

    payload = _base_payload(
        args,
        STATUS_POPULATED,
        ["PUBLIC_CURRENT_SNAPSHOT_ONLY", "FULL_HISTORICAL_PAID_DATA_NOT_PRESENT"],
    )
    payload.update(
        {
            "run_status": "valid",
            "network_attempted": True,
            "snapshot_ts_ms": snapshot_ts_ms,
            "expiries": list(expiries),
            "snapshot_count": len(snapshots),
            "leg_count": sum(len(snapshot.legs) for snapshot in snapshots),
            "cache_writes": [
                raw_dir.as_posix(),
                normalized_dir.as_posix(),
            ],
            "raw_manifest_sha256": raw_manifest_sha,
            "note": (
                "Cache was written from actual current public Deribit responses only. It is "
                "not a historical authorizing cache and must still block pre-registration."
            ),
        }
    )
    return payload


def _build_payload(args: argparse.Namespace, repo_root: Path) -> dict[str, Any]:
    if os.environ.get("CI"):
        return _blocked(
            args,
            ["REFUSED_UNDER_CI"],
            error="populate_deribit_options_cache is a manual network tool; run with env -u CI.",
        )

    source = args.source.lower()
    if source not in _SUPPORTED_PUBLIC_SOURCES:
        return _blocked(
            args,
            ["HISTORICAL_SOURCE_NOT_IMPLEMENTED", "NO_PAID_HISTORICAL_DATA_AVAILABLE"],
            error=(
                f"source {args.source!r} is not available through DeribitOptionsAdapter. "
                "No cache was written and no synthetic data was generated."
            ),
        )
    return _populate_public_current(args, repo_root)


def _markdown(payload: dict[str, Any]) -> str:
    lines = [
        "# Deribit options cache population",
        "",
        f"- run_status: {payload['run_status']}",
        f"- status: {payload['status']}",
        f"- source: {payload['source']}",
        f"- currency: {payload['currency']}",
        f"- scenario_id: {payload['scenario_id']}",
        f"- network_attempted: {payload['network_attempted']}",
        f"- cache_fabricated: {payload['cache_fabricated']}",
        f"- reason_codes: {', '.join(payload['reason_codes']) or '-'}",
        "",
        "The script refuses CI execution and never fills historical gaps with synthetic data. "
        "A current public snapshot, when explicitly requested inside the live time window, remains "
        "non-authorizing and must not be used as paid historical Deribit option-chain evidence.",
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
        description="Populate aq-options-cache-v1 from read-only Deribit option-chain data."
    )
    parser.add_argument("--source", required=True)
    parser.add_argument("--currency", default="ETH", choices=["ETH"])
    parser.add_argument("--from", dest="start", required=True, help="ISO8601 start timestamp")
    parser.add_argument("--to", dest="end", required=True, help="ISO8601 end timestamp")
    parser.add_argument("--scenario-id", default=DEFAULT_SCENARIO_ID)
    parser.add_argument("--raw-cache-root", required=True)
    parser.add_argument("--cache-root", required=True)
    parser.add_argument("--reports-dir", default="reports")
    parser.add_argument("--json", action="store_true", help="Print the report payload as JSON.")
    parser.add_argument(
        "--repo-root",
        default=str(Path(__file__).resolve().parents[1]),
        help="Repository root (defaults to the checkout containing this script).",
    )
    args = parser.parse_args(argv)

    repo_root = Path(args.repo_root).resolve()
    payload = _build_payload(args, repo_root)
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

"""Deterministic Deribit option-chain cache: ``aq-options-cache-v1``.

The module writes and loads two manifest-hashed cache concepts used by the VRP flow:
raw-source manifests and normalized option-chain manifests. Loading is network-free and
fail-closed; it validates only schema/coverage/provenance predicates, never economics.
"""

from __future__ import annotations

import csv
import io
import json
import math
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

from ajentix_quant.adapters.base import SourceQuality
from ajentix_quant.options.types import OptionChainSnapshot, OptionLeg, OptionType, Side

SCHEMA_VERSION = "aq-options-cache-v1"
TRANSFORM_VERSION = "ajentix-quant/phase1-g002-options-transform-v1"
RAW_MANIFEST_KIND = "raw_source"
NORMALIZED_MANIFEST_KIND = "normalized"
OPTION_CHAIN_FILE = "option_chains.csv"

_OPTION_CHAIN_HEADER = [
    "snapshot_ts_ms",
    "source_ts_ms",
    "underlying",
    "exchange",
    "source_id",
    "scenario_id",
    "expiry_ms",
    "settlement_index_price",
    "index_price",
    "usd_conversion_inputs_json",
    "instrument_name",
    "contract_multiplier",
    "option_type",
    "side",
    "strike",
    "settlement_style",
    "settlement_index",
    "premium_currency",
    "fee_currency",
    "collateral_currency",
    "usd_conversion_source",
    "quote_ts_ms",
    "quote_age_s",
    "bid_price",
    "bid_amount",
    "bid_iv",
    "ask_price",
    "ask_amount",
    "ask_iv",
    "mark_price",
    "greek_provenance_key",
    "min_tick",
    "min_lot",
    "source_quality",
]

_OPTION_CHAIN_REQUIRED_COLUMNS = tuple(_OPTION_CHAIN_HEADER)
_NON_AUTHORIZING_SOURCE_QUALITIES = {SourceQuality.FIXTURE, SourceQuality.PROXY}


class OptionsCacheValidationError(Exception):
    """Raised when an options cache fails fail-closed validation."""


def sha256_text(text: str) -> str:
    import hashlib

    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _require(condition: bool, message: str) -> None:
    if not condition:
        raise OptionsCacheValidationError(message)


def _require_mapping(value: object, message: str) -> Mapping[Any, Any]:
    if not isinstance(value, Mapping):
        raise OptionsCacheValidationError(message)
    return value


def _manifest_text(manifest: Mapping[str, Any]) -> str:
    return json.dumps(manifest, indent=2, sort_keys=True) + "\n"


def _scenario_dir(root: str | Path, scenario_id: str) -> Path:
    return Path(root) / scenario_id


def _fmt_num(value: float) -> str:
    out = float(value)
    _require(math.isfinite(out), f"numeric value must be finite, got {value!r}")
    return repr(out)


def _fmt_optional_num(value: float | None) -> str:
    return "" if value is None else _fmt_num(value)


def _require_present_num(value: float | None, label: str) -> float:
    if value is None:
        raise OptionsCacheValidationError(f"missing {label}")
    _fmt_num(value)
    return value

def _to_int(value: str, label: str, *, positive: bool = False) -> int:
    try:
        out = int(value)
    except ValueError as exc:
        raise OptionsCacheValidationError(f"{label} must be an integer: {value!r}") from exc
    if positive:
        _require(out > 0, f"{label} must be positive")
    else:
        _require(out >= 0, f"{label} must be non-negative")
    return out


def _to_float(value: str, label: str, *, positive: bool = False) -> float:
    try:
        out = float(value)
    except ValueError as exc:
        raise OptionsCacheValidationError(f"{label} must be a float: {value!r}") from exc
    _require(math.isfinite(out), f"{label} must be finite")
    if positive:
        _require(out > 0.0, f"{label} must be positive")
    else:
        _require(out >= 0.0, f"{label} must be non-negative")
    return out


def _to_optional_float(value: str, label: str) -> float | None:
    return None if value == "" else _to_float(value, label)


def _to_enum(enum_cls: type[Any], value: str, label: str) -> Any:
    try:
        return enum_cls(value)
    except ValueError as exc:
        raise OptionsCacheValidationError(
            f"{label} invalid {enum_cls.__name__}: {value!r}"
        ) from exc


def _coerce_source_quality(value: SourceQuality | str, label: str) -> SourceQuality:
    try:
        quality = value if isinstance(value, SourceQuality) else SourceQuality(value)
    except ValueError as exc:
        raise OptionsCacheValidationError(f"{label} invalid SourceQuality: {value!r}") from exc
    return quality


def _source_quality_manifest(
    source_quality: Mapping[str, SourceQuality | str]
) -> dict[str, str]:
    out: dict[str, str] = {}
    for key, value in source_quality.items():
        out[str(key)] = _coerce_source_quality(value, f"source_quality[{key}]").value
    _require(bool(out), "source_quality must be non-empty")
    return dict(sorted(out.items()))


def _validate_source_quality(manifest: Mapping[str, Any]) -> dict[str, SourceQuality]:
    raw = _require_mapping(
        manifest.get("source_quality"), "manifest source_quality must be an object"
    )
    out: dict[str, SourceQuality] = {}
    for key, value in raw.items():
        quality = _coerce_source_quality(str(value), f"source_quality[{key}]")
        _require(quality is not SourceQuality.ABSENT, f"source_quality[{key}] cannot be absent")
        out[str(key)] = quality
    _require(bool(out), "manifest source_quality must be non-empty")
    return out


def _read_json(path: Path) -> dict[str, Any]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise OptionsCacheValidationError(f"{path.name} is not valid JSON: {exc}") from exc
    _require(isinstance(data, dict), f"{path.name} must be a JSON object")
    return data


def _read_manifest(scenario_dir: Path, scenario_id: str, manifest_kind: str) -> dict[str, Any]:
    manifest_path = scenario_dir / "manifest.json"
    _require(manifest_path.is_file(), f"missing manifest.json in {scenario_dir}")
    manifest = _read_json(manifest_path)
    _require(
        manifest.get("schema_version") == SCHEMA_VERSION,
        f"schema_version must be {SCHEMA_VERSION}, got {manifest.get('schema_version')!r}",
    )
    _require(
        manifest.get("manifest_kind") == manifest_kind,
        f"manifest_kind must be {manifest_kind}, got {manifest.get('manifest_kind')!r}",
    )
    _require(
        manifest.get("scenario_id") == scenario_id,
        f"manifest scenario_id {manifest.get('scenario_id')!r} != requested {scenario_id!r}",
    )
    _validate_source_quality(manifest)
    return manifest


def _verify_sha(scenario_dir: Path, manifest: Mapping[str, Any], filename: str) -> str:
    path = scenario_dir / filename
    _require(path.is_file(), f"missing {filename}")
    text = path.read_text(encoding="utf-8")
    sha_map = _require_mapping(
        manifest.get("sha256_by_file"), "manifest sha256_by_file must be an object"
    )
    expected = sha_map.get(filename)
    _require(isinstance(expected, str), f"manifest sha256_by_file missing {filename}")
    actual = sha256_text(text)
    _require(actual == expected, f"sha256 mismatch for {filename}: {actual} != {expected}")
    size_map = manifest.get("file_sizes")
    if isinstance(size_map, Mapping) and filename in size_map:
        _require(
            size_map[filename] == len(text.encode("utf-8")),
            f"file_sizes[{filename}] != actual byte size",
        )
    return text


def _csv_text(header: Sequence[str], rows: Sequence[Sequence[str]]) -> str:
    buf = io.StringIO()
    writer = csv.writer(buf, lineterminator="\n")
    writer.writerow(header)
    for row in rows:
        writer.writerow(row)
    return buf.getvalue()


def _parse_csv(text: str, header: Sequence[str], filename: str) -> list[dict[str, str]]:
    rows = list(csv.reader(text.splitlines()))
    _require(len(rows) >= 1, f"{filename} has no header")
    _require(rows[0] == list(header), f"{filename} header mismatch: {rows[0]} != {list(header)}")
    out: list[dict[str, str]] = []
    for i, row in enumerate(rows[1:], start=2):
        _require(len(row) == len(header), f"{filename} row {i} width {len(row)} != {len(header)}")
        out.append(dict(zip(header, row, strict=True)))
    return out


def _json_dumps(value: Mapping[str, Any]) -> str:
    return json.dumps(dict(value), sort_keys=True, separators=(",", ":"))


def _json_loads_mapping(value: str, label: str) -> dict[str, Any]:
    try:
        parsed = json.loads(value)
    except json.JSONDecodeError as exc:
        raise OptionsCacheValidationError(f"{label} must be JSON: {exc}") from exc
    _require(isinstance(parsed, dict), f"{label} must be a JSON object")
    return parsed


def _assert_sorted_unique(values: Sequence[int], label: str) -> None:
    for i in range(1, len(values)):
        _require(values[i] > values[i - 1], f"{label} not strictly ascending")


def _sorted_unique(values: Sequence[int]) -> list[int]:
    out = sorted(set(values))
    _assert_sorted_unique(out, "timestamps")
    return out


def _expiry_ms_for_snapshot(snapshot: OptionChainSnapshot) -> int:
    expiries = {leg.expiry_ms for leg in snapshot.legs}
    _require(len(expiries) == 1, "each normalized OptionChainSnapshot must contain one expiry")
    return next(iter(expiries))


def _validate_leg_domains(leg: OptionLeg) -> None:
    _require(leg.bid_price <= leg.ask_price, f"{leg.instrument_name} bid > ask")
    _require(leg.bid_amount > 0.0, f"{leg.instrument_name} bid_amount must be positive")
    _require(leg.ask_amount > 0.0, f"{leg.instrument_name} ask_amount must be positive")
    _require(leg.source_quality is not SourceQuality.ABSENT, "leg source_quality cannot be absent")


def _snapshot_rows(snapshots: Sequence[OptionChainSnapshot], scenario_id: str) -> list[list[str]]:
    _require(bool(snapshots), "normalized cache requires at least one snapshot")
    rows: list[tuple[str, ...]] = []
    primary_keys: set[tuple[str, int, int, str]] = set()
    for snapshot in snapshots:
        _require(snapshot.scenario_id == scenario_id, "snapshot scenario_id mismatch")
        _require(snapshot.exchange == "deribit", "normalized options cache only supports deribit")
        settlement_index_price: float = _require_present_num(
            snapshot.settlement_index_price, "settlement_index_price"
        )
        index_price: float = _require_present_num(snapshot.index_price, "index_price")
        _require(settlement_index_price > 0.0, "settlement_index_price must be positive")
        _require(index_price > 0.0, "index_price must be positive")
        expiry_ms = _expiry_ms_for_snapshot(snapshot)
        for leg in sorted(snapshot.legs, key=lambda item: item.instrument_name):
            _validate_leg_domains(leg)
            key = (snapshot.underlying, snapshot.snapshot_ts_ms, expiry_ms, leg.instrument_name)
            _require(key not in primary_keys, f"duplicate option-chain primary key: {key}")
            primary_keys.add(key)
            rows.append(
                (
                    str(snapshot.snapshot_ts_ms),
                    str(snapshot.source_ts_ms),
                    snapshot.underlying,
                    snapshot.exchange,
                    snapshot.source_id,
                    scenario_id,
                    str(expiry_ms),
                    _fmt_num(settlement_index_price),
                    _fmt_num(index_price),
                    _json_dumps(snapshot.usd_conversion_inputs),
                    leg.instrument_name,
                    _fmt_num(leg.contract_multiplier),
                    leg.option_type.value,
                    leg.side.value,
                    _fmt_num(leg.strike),
                    leg.settlement_style,
                    leg.settlement_index,
                    leg.premium_currency,
                    leg.fee_currency,
                    leg.collateral_currency,
                    leg.usd_conversion_source,
                    str(leg.quote_ts_ms),
                    _fmt_num(leg.quote_age_s),
                    _fmt_num(leg.bid_price),
                    _fmt_num(leg.bid_amount),
                    _fmt_num(leg.bid_iv),
                    _fmt_num(leg.ask_price),
                    _fmt_num(leg.ask_amount),
                    _fmt_num(leg.ask_iv),
                    _fmt_optional_num(leg.mark_price),
                    leg.greek_provenance_key,
                    _fmt_num(leg.min_tick),
                    _fmt_num(leg.min_lot),
                    leg.source_quality.value,
                )
            )
    rows.sort(key=lambda r: (r[2], int(r[0]), int(r[6]), r[10]))
    return [list(row) for row in rows]


def _source_quality_from_snapshots(snapshots: Sequence[OptionChainSnapshot]) -> dict[str, str]:
    merged: dict[str, SourceQuality] = {}
    for snapshot in snapshots:
        for key, value in snapshot.source_quality_map.items():
            merged[str(key)] = _coerce_source_quality(value, f"source_quality_map[{key}]")
        for leg in snapshot.legs:
            merged.setdefault("option_chain", leg.source_quality)
            merged.setdefault("instrument_metadata", leg.source_quality)
            merged.setdefault("settlement_index", leg.source_quality)
    return _source_quality_manifest(merged)


def _group_snapshot_rows(
    records: Sequence[dict[str, str]],
) -> dict[tuple[str, int, int], list[dict[str, str]]]:
    grouped: dict[tuple[str, int, int], list[dict[str, str]]] = {}
    primary_keys: set[tuple[str, int, int, str]] = set()
    for rec in records:
        key = (
            rec["underlying"],
            _to_int(rec["snapshot_ts_ms"], "snapshot_ts_ms", positive=True),
            _to_int(rec["expiry_ms"], "expiry_ms", positive=True),
        )
        primary = (*key, rec["instrument_name"])
        _require(primary not in primary_keys, f"duplicate option-chain primary key: {primary}")
        primary_keys.add(primary)
        grouped.setdefault(key, []).append(rec)
    return grouped


def _validate_record_order(records: Sequence[dict[str, str]]) -> None:
    previous: tuple[str, int, int, str] | None = None
    for rec in records:
        key = (
            rec["underlying"],
            _to_int(rec["snapshot_ts_ms"], "snapshot_ts_ms", positive=True),
            _to_int(rec["expiry_ms"], "expiry_ms", positive=True),
            rec["instrument_name"],
        )
        if previous is not None:
            _require(key > previous, f"{OPTION_CHAIN_FILE} rows not strictly ascending")
        previous = key


def _build_lifecycle(records: Sequence[dict[str, str]]) -> list[dict[str, Any]]:
    by_expiry: dict[tuple[str, int], dict[str, Any]] = {}
    for rec in records:
        underlying = rec["underlying"]
        expiry_ms = _to_int(rec["expiry_ms"], "expiry_ms", positive=True)
        snapshot_ts = _to_int(rec["snapshot_ts_ms"], "snapshot_ts_ms", positive=True)
        item = by_expiry.setdefault(
            (underlying, expiry_ms),
            {
                "underlying": underlying,
                "expiry_ms": expiry_ms,
                "first_snapshot_ts_ms": snapshot_ts,
                "last_snapshot_ts_ms": snapshot_ts,
                "snapshot_timestamps_ms": set(),
                "instrument_names": set(),
                "leg_count": 0,
            },
        )
        item["first_snapshot_ts_ms"] = min(item["first_snapshot_ts_ms"], snapshot_ts)
        item["last_snapshot_ts_ms"] = max(item["last_snapshot_ts_ms"], snapshot_ts)
        item["snapshot_timestamps_ms"].add(snapshot_ts)
        item["instrument_names"].add(rec["instrument_name"])
        item["leg_count"] += 1
    lifecycle: list[dict[str, Any]] = []
    for item in by_expiry.values():
        timestamps = sorted(item["snapshot_timestamps_ms"])
        names = sorted(item["instrument_names"])
        lifecycle.append(
            {
                "underlying": item["underlying"],
                "expiry_ms": item["expiry_ms"],
                "first_snapshot_ts_ms": item["first_snapshot_ts_ms"],
                "last_snapshot_ts_ms": item["last_snapshot_ts_ms"],
                "snapshot_count": len(timestamps),
                "snapshot_timestamps_ms": timestamps,
                "instrument_count": len(names),
                "instrument_names": names,
                "leg_count": item["leg_count"],
            }
        )
    lifecycle.sort(key=lambda item: (item["underlying"], item["expiry_ms"]))
    return lifecycle


def _coverage_timestamps(records: Sequence[dict[str, str]]) -> dict[str, list[int]]:
    by_underlying: dict[str, list[int]] = {}
    for rec in records:
        by_underlying.setdefault(rec["underlying"], []).append(
            _to_int(rec["snapshot_ts_ms"], "snapshot_ts_ms", positive=True)
        )
    return {key: _sorted_unique(values) for key, values in sorted(by_underlying.items())}


def _min_ticket_metadata(records: Sequence[dict[str, str]]) -> dict[str, dict[str, Any]]:
    by_underlying: dict[str, list[dict[str, str]]] = {}
    for rec in records:
        by_underlying.setdefault(rec["underlying"], []).append(rec)
    out: dict[str, dict[str, Any]] = {}
    for underlying, rows in sorted(by_underlying.items()):
        ticks = [_to_float(row["min_tick"], "min_tick", positive=True) for row in rows]
        lots = [_to_float(row["min_lot"], "min_lot", positive=True) for row in rows]
        out[underlying] = {
            "instrument_count": len({row["instrument_name"] for row in rows}),
            "min_tick_min": min(ticks),
            "min_tick_max": max(ticks),
            "min_lot_min": min(lots),
            "min_lot_max": max(lots),
        }
    return out


def _bid_ask_stats(records: Sequence[dict[str, str]]) -> dict[str, Any]:
    total = len(records)
    rows_with_bid = 0
    rows_with_ask = 0
    rows_with_mark = 0
    for rec in records:
        bid = _to_float(rec["bid_price"], "bid_price")
        ask = _to_float(rec["ask_price"], "ask_price")
        bid_amount = _to_float(rec["bid_amount"], "bid_amount", positive=True)
        ask_amount = _to_float(rec["ask_amount"], "ask_amount", positive=True)
        _require(bid <= ask, f"{rec['instrument_name']} bid > ask")
        rows_with_bid += int(bid_amount > 0.0)
        rows_with_ask += int(ask_amount > 0.0)
        rows_with_mark += int(rec["mark_price"] != "")
    return {
        "total_rows": total,
        "rows_with_bid": rows_with_bid,
        "rows_with_ask": rows_with_ask,
        "rows_with_mark_price": rows_with_mark,
        "bid_ask_complete": rows_with_bid == total and rows_with_ask == total,
    }


def _settlement_index_coverage(records: Sequence[dict[str, str]]) -> dict[str, dict[str, Any]]:
    by_underlying: dict[str, list[dict[str, str]]] = {}
    for rec in records:
        by_underlying.setdefault(rec["underlying"], []).append(rec)
    out: dict[str, dict[str, Any]] = {}
    for underlying, rows in sorted(by_underlying.items()):
        timestamps = _sorted_unique([
            _to_int(row["snapshot_ts_ms"], "snapshot_ts_ms", positive=True) for row in rows
        ])
        with_settlement = 0
        with_index = 0
        for row in rows:
            _to_float(row["settlement_index_price"], "settlement_index_price", positive=True)
            _to_float(row["index_price"], "index_price", positive=True)
            with_settlement += 1
            with_index += 1
        out[underlying] = {
            "total_rows": len(rows),
            "rows_with_settlement_index_price": with_settlement,
            "rows_with_index_price": with_index,
            "timestamps_ms": timestamps,
        }
    return out


def _source_ids(records: Sequence[dict[str, str]]) -> list[str]:
    return sorted({rec["source_id"] for rec in records})


def _date_range(records: Sequence[dict[str, str]]) -> dict[str, int]:
    timestamps = [
        _to_int(row["snapshot_ts_ms"], "snapshot_ts_ms", positive=True)
        for row in records
    ]
    return {"start_ts_ms": min(timestamps), "end_ts_ms": max(timestamps)}


def _manifest_from_records(
    scenario_id: str,
    records: Sequence[dict[str, str]],
    option_chain_text: str,
    *,
    source_quality: Mapping[str, str],
    transform_version: str,
    raw_manifest_sha256: str | None,
    fold_coverage_timestamps_ms: Mapping[str, Sequence[int]] | None,
    stress_selector_input_coverage: Mapping[str, Any] | None,
) -> dict[str, Any]:
    lifecycle = _build_lifecycle(records)
    _require(bool(lifecycle), "expiry_lifecycle must be non-empty")
    folded = {
        str(name): _sorted_unique([int(value) for value in timestamps])
        for name, timestamps in (fold_coverage_timestamps_ms or {}).items()
    }
    non_authorizing = sorted(
        key
        for key, value in source_quality.items()
        if SourceQuality(value) in _NON_AUTHORIZING_SOURCE_QUALITIES
    )
    return {
        "schema_version": SCHEMA_VERSION,
        "manifest_kind": NORMALIZED_MANIFEST_KIND,
        "scenario_id": scenario_id,
        "exchange": "deribit",
        "transform_version": transform_version,
        "source_ids": _source_ids(records),
        "raw_manifest_sha256": raw_manifest_sha256,
        "date_range": _date_range(records),
        "source_quality": dict(sorted(source_quality.items())),
        "non_authorizing_source_quality_keys": non_authorizing,
        "row_counts": {
            OPTION_CHAIN_FILE: len(records),
            "snapshots": len(_group_snapshot_rows(records)),
            "legs": len(records),
        },
        "sha256_by_file": {OPTION_CHAIN_FILE: sha256_text(option_chain_text)},
        "file_sizes": {OPTION_CHAIN_FILE: len(option_chain_text.encode("utf-8"))},
        "required_column_coverage": {column: True for column in _OPTION_CHAIN_REQUIRED_COLUMNS},
        "coverage_timestamps_ms": _coverage_timestamps(records),
        "fold_coverage_timestamps_ms": folded,
        "expiry_lifecycle": lifecycle,
        "min_ticket_metadata": _min_ticket_metadata(records),
        "bid_ask_availability_stats": _bid_ask_stats(records),
        "settlement_index_coverage": _settlement_index_coverage(records),
        "stress_selector_input_coverage": dict(stress_selector_input_coverage or {}),
    }


def write_raw_source_manifest(
    raw_cache_root: str | Path,
    scenario_id: str,
    *,
    source_files: Mapping[str, str],
    source_ids: Sequence[str],
    currency: str,
    start_ts_ms: int,
    end_ts_ms: int,
    download_timestamp_ms: int,
    source_uri_ids: Sequence[str],
    license_budget_note: str,
    acquisition_tool_version: str,
    source_quality: Mapping[str, SourceQuality | str],
) -> Path:
    """Write a deterministic raw-source manifest directory and return it."""

    _require(bool(source_files), "raw source_files must be non-empty")
    _require(start_ts_ms <= end_ts_ms, "raw date range start_ts_ms must be <= end_ts_ms")
    scenario_dir = _scenario_dir(raw_cache_root, scenario_id)
    scenario_dir.mkdir(parents=True, exist_ok=True)

    sha_by_file: dict[str, str] = {}
    file_sizes: dict[str, int] = {}
    for filename, text in sorted(source_files.items()):
        _require(filename != "manifest.json", "raw source file cannot be manifest.json")
        path = scenario_dir / filename
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
        sha_by_file[filename] = sha256_text(text)
        file_sizes[filename] = len(text.encode("utf-8"))

    manifest = {
        "schema_version": SCHEMA_VERSION,
        "manifest_kind": RAW_MANIFEST_KIND,
        "scenario_id": scenario_id,
        "currency": currency.upper(),
        "date_range": {"start_ts_ms": start_ts_ms, "end_ts_ms": end_ts_ms},
        "download_timestamp_ms": download_timestamp_ms,
        "source_ids": sorted(set(map(str, source_ids))),
        "source_uri_ids": sorted(set(map(str, source_uri_ids))),
        "license_budget_note": license_budget_note,
        "acquisition_tool_version": acquisition_tool_version,
        "source_quality": _source_quality_manifest(source_quality),
        "sha256_by_file": sha_by_file,
        "file_sizes": file_sizes,
        "row_counts": {
            filename: text.count("\n") for filename, text in sorted(source_files.items())
        },
    }
    (scenario_dir / "manifest.json").write_text(_manifest_text(manifest), encoding="utf-8")
    return scenario_dir


def load_raw_source_manifest(raw_cache_root: str | Path, scenario_id: str) -> dict[str, Any]:
    """Load and hash-verify a raw-source manifest without reading network data."""

    scenario_dir = _scenario_dir(raw_cache_root, scenario_id)
    manifest = _read_manifest(scenario_dir, scenario_id, RAW_MANIFEST_KIND)
    sha_map = _require_mapping(manifest.get("sha256_by_file"), "raw manifest needs file hashes")
    _require(bool(sha_map), "raw manifest needs file hashes")
    for filename in sorted(sha_map):
        _verify_sha(scenario_dir, manifest, str(filename))
    return manifest


def write_normalized_cache(
    cache_root: str | Path,
    scenario_id: str,
    *,
    snapshots: Sequence[OptionChainSnapshot],
    source_ids: Sequence[str] = (),
    transform_version: str = TRANSFORM_VERSION,
    raw_manifest_sha256: str | None = None,
    fold_coverage_timestamps_ms: Mapping[str, Sequence[int]] | None = None,
    stress_selector_input_coverage: Mapping[str, Any] | None = None,
) -> Path:
    """Write a deterministic normalized option-chain cache and return the directory."""

    scenario_dir = _scenario_dir(cache_root, scenario_id)
    scenario_dir.mkdir(parents=True, exist_ok=True)
    rows = _snapshot_rows(snapshots, scenario_id)
    option_chain_text = _csv_text(_OPTION_CHAIN_HEADER, rows)
    (scenario_dir / OPTION_CHAIN_FILE).write_text(option_chain_text, encoding="utf-8")
    records = [dict(zip(_OPTION_CHAIN_HEADER, row, strict=True)) for row in rows]
    source_quality = _source_quality_from_snapshots(snapshots)
    for source_id in source_ids:
        source_quality.setdefault(f"source_id:{source_id}", next(iter(source_quality.values())))
    manifest = _manifest_from_records(
        scenario_id,
        records,
        option_chain_text,
        source_quality=source_quality,
        transform_version=transform_version,
        raw_manifest_sha256=raw_manifest_sha256,
        fold_coverage_timestamps_ms=fold_coverage_timestamps_ms,
        stress_selector_input_coverage=stress_selector_input_coverage,
    )
    if source_ids:
        manifest["source_ids"] = sorted(
            set([*manifest["source_ids"], *map(str, source_ids)])
        )
    (scenario_dir / "manifest.json").write_text(_manifest_text(manifest), encoding="utf-8")
    return scenario_dir


def _validate_manifest_coverage(
    manifest: Mapping[str, Any], records: Sequence[dict[str, str]]
) -> None:
    row_counts = _require_mapping(
        manifest.get("row_counts"), "manifest row_counts must be an object"
    )
    _require(
        row_counts.get(OPTION_CHAIN_FILE) == len(records),
        f"manifest row_counts[{OPTION_CHAIN_FILE}] != actual {len(records)}",
    )
    _require(row_counts.get("legs") == len(records), "manifest row_counts[legs] mismatch")
    _require(
        row_counts.get("snapshots") == len(_group_snapshot_rows(records)),
        "manifest row_counts[snapshots] mismatch",
    )
    required_columns = _require_mapping(
        manifest.get("required_column_coverage"),
        "required_column_coverage must be an object",
    )
    for column in _OPTION_CHAIN_REQUIRED_COLUMNS:
        _require(required_columns.get(column) is True, f"required column missing: {column}")
    _require(
        manifest.get("coverage_timestamps_ms") == _coverage_timestamps(records),
        "coverage_timestamps_ms mismatch",
    )
    lifecycle = manifest.get("expiry_lifecycle")
    _require(isinstance(lifecycle, list) and bool(lifecycle), "missing expiry_lifecycle")
    _require(lifecycle == _build_lifecycle(records), "expiry_lifecycle mismatch")
    _require(
        manifest.get("min_ticket_metadata") == _min_ticket_metadata(records),
        "min_ticket_metadata mismatch",
    )
    _require(
        manifest.get("bid_ask_availability_stats") == _bid_ask_stats(records),
        "bid_ask_availability_stats mismatch",
    )
    _require(
        manifest.get("settlement_index_coverage") == _settlement_index_coverage(records),
        "settlement_index_coverage mismatch",
    )
    coverage = _require_mapping(
        manifest.get("coverage_timestamps_ms"), "coverage_timestamps_ms missing"
    )
    _require(bool(coverage), "coverage_timestamps_ms missing")
    for underlying, timestamps in coverage.items():
        _require(
            isinstance(timestamps, list),
            f"coverage_timestamps_ms[{underlying}] must be a list",
        )
        _assert_sorted_unique([int(value) for value in timestamps], f"coverage[{underlying}]")


def _load_snapshots_from_records(
    records: Sequence[dict[str, str]],
    manifest: Mapping[str, Any],
    manifest_sha256: str,
) -> tuple[OptionChainSnapshot, ...]:
    source_quality_map = _validate_source_quality(manifest)
    scenario_id = str(manifest["scenario_id"])
    grouped = _group_snapshot_rows(records)
    snapshots: list[OptionChainSnapshot] = []
    for (underlying, snapshot_ts_ms, expiry_ms), group in sorted(grouped.items()):
        group.sort(key=lambda row: row["instrument_name"])
        first = group[0]
        source_ts_ms = _to_int(first["source_ts_ms"], "source_ts_ms", positive=True)
        source_id = first["source_id"]
        exchange = first["exchange"]
        settlement_index_price = _to_float(
            first["settlement_index_price"], "settlement_index_price", positive=True
        )
        index_price = _to_float(first["index_price"], "index_price", positive=True)
        usd_conversion_inputs = _json_loads_mapping(
            first["usd_conversion_inputs_json"], "usd_conversion_inputs_json"
        )
        legs: list[OptionLeg] = []
        for rec in group:
            _require(rec["scenario_id"] == scenario_id, "row scenario_id mismatch")
            _require(rec["exchange"] == exchange, "snapshot exchange mismatch")
            _require(rec["source_id"] == source_id, "snapshot source_id mismatch")
            _require(
                _to_int(rec["source_ts_ms"], "source_ts_ms", positive=True) == source_ts_ms,
                "source_ts_ms mismatch",
            )
            _require(
                _to_float(rec["settlement_index_price"], "settlement_index_price", positive=True)
                == settlement_index_price,
                "settlement_index_price mismatch within snapshot",
            )
            _require(
                _to_float(rec["index_price"], "index_price", positive=True) == index_price,
                "index_price mismatch within snapshot",
            )
            _require(
                _json_loads_mapping(
                    rec["usd_conversion_inputs_json"], "usd_conversion_inputs_json"
                )
                == usd_conversion_inputs,
                "usd_conversion_inputs mismatch within snapshot",
            )
            leg = OptionLeg(
                instrument_name=rec["instrument_name"],
                underlying=underlying,
                contract_multiplier=_to_float(
                    rec["contract_multiplier"], "contract_multiplier", positive=True
                ),
                option_type=_to_enum(OptionType, rec["option_type"], "option_type"),
                side=_to_enum(Side, rec["side"], "side"),
                strike=_to_float(rec["strike"], "strike", positive=True),
                expiry_ms=expiry_ms,
                settlement_style=rec["settlement_style"],
                settlement_index=rec["settlement_index"],
                premium_currency=rec["premium_currency"],
                fee_currency=rec["fee_currency"],
                collateral_currency=rec["collateral_currency"],
                usd_conversion_source=rec["usd_conversion_source"],
                quote_ts_ms=_to_int(rec["quote_ts_ms"], "quote_ts_ms", positive=True),
                quote_age_s=_to_float(rec["quote_age_s"], "quote_age_s"),
                bid_price=_to_float(rec["bid_price"], "bid_price"),
                bid_amount=_to_float(rec["bid_amount"], "bid_amount", positive=True),
                bid_iv=_to_float(rec["bid_iv"], "bid_iv"),
                ask_price=_to_float(rec["ask_price"], "ask_price"),
                ask_amount=_to_float(rec["ask_amount"], "ask_amount", positive=True),
                ask_iv=_to_float(rec["ask_iv"], "ask_iv"),
                mark_price=_to_optional_float(rec["mark_price"], "mark_price"),
                greek_provenance_key=rec["greek_provenance_key"],
                min_tick=_to_float(rec["min_tick"], "min_tick", positive=True),
                min_lot=_to_float(rec["min_lot"], "min_lot", positive=True),
                source_quality=_coerce_source_quality(
                    rec["source_quality"], "leg.source_quality"
                ),
            )
            _validate_leg_domains(leg)
            legs.append(leg)
        snapshots.append(
            OptionChainSnapshot(
                underlying=underlying,
                exchange=exchange,
                snapshot_ts_ms=snapshot_ts_ms,
                source_ts_ms=source_ts_ms,
                source_id=source_id,
                scenario_id=scenario_id,
                settlement_index_price=settlement_index_price,
                index_price=index_price,
                usd_conversion_inputs=usd_conversion_inputs,
                legs=tuple(legs),
                source_quality_map=source_quality_map,
                schema_version=SCHEMA_VERSION,
                manifest_sha256=manifest_sha256,
            )
        )
    return tuple(snapshots)


def load_normalized_cache(
    cache_root: str | Path, scenario_id: str
) -> tuple[OptionChainSnapshot, ...]:
    """Load + fail-closed validate normalized option-chain snapshots (no network)."""

    scenario_dir = _scenario_dir(cache_root, scenario_id)
    manifest_path = scenario_dir / "manifest.json"
    manifest_text = manifest_path.read_text(encoding="utf-8") if manifest_path.is_file() else ""
    manifest = _read_manifest(scenario_dir, scenario_id, NORMALIZED_MANIFEST_KIND)
    option_chain_text = _verify_sha(scenario_dir, manifest, OPTION_CHAIN_FILE)
    records = _parse_csv(option_chain_text, _OPTION_CHAIN_HEADER, OPTION_CHAIN_FILE)
    _require(bool(records), "option chain cache is empty")
    _validate_record_order(records)
    _validate_manifest_coverage(manifest, records)
    return _load_snapshots_from_records(records, manifest, sha256_text(manifest_text))


def load_normalized_manifest(cache_root: str | Path, scenario_id: str) -> dict[str, Any]:
    """Load and validate the normalized manifest plus file hashes."""

    scenario_dir = _scenario_dir(cache_root, scenario_id)
    manifest = _read_manifest(scenario_dir, scenario_id, NORMALIZED_MANIFEST_KIND)
    _verify_sha(scenario_dir, manifest, OPTION_CHAIN_FILE)
    return manifest


__all__ = [
    "NORMALIZED_MANIFEST_KIND",
    "OPTION_CHAIN_FILE",
    "RAW_MANIFEST_KIND",
    "SCHEMA_VERSION",
    "TRANSFORM_VERSION",
    "OptionsCacheValidationError",
    "load_normalized_cache",
    "load_normalized_manifest",
    "load_raw_source_manifest",
    "sha256_text",
    "write_normalized_cache",
    "write_raw_source_manifest",
]

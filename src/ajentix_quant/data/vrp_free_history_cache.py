"""Deterministic raw cache for free Deribit-history VRP option trades.

``aq-vrp-free-history-cache-v1`` stores only observed public Deribit-history trade
rows plus the exact ETH index path carried in those rows. The module validates
strictly and fails closed on malformed instruments, missing IV/index/amount,
non-finite values, stale time gaps, or missing requested grid/stress coverage. It
never imputes trades, IV, size, or underlying prices.
"""

from __future__ import annotations

import csv
import io
import json
import math
import re
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from ajentix_quant.adapters.base import SourceQuality
from ajentix_quant.research.vrp_free_preregistration import (
    DEFAULT_SCENARIO_ID,
    PLAN_BINNING,
    PLAN_FOLDS,
    PLAN_RECONSTRUCTION_CONFIG,
)

SCHEMA_VERSION = "aq-vrp-free-history-cache-v1"
RAW_MANIFEST_KIND = "raw_source"
GENERATOR_VERSION = "ajentix-quant/g002-vrp-free-history-cache-v1"
DEFAULT_MAX_NON_IV_BEARING_RATE = 0.10

TRADES_FILE = "trades.jsonl"
INDEX_PATH_FILE = "index_path.csv"

_REQUIRED_TRADE_FIELDS = (
    "trade_id",
    "instrument_name",
    "timestamp",
    "trade_seq",
    "price",
    "mark_price",
    "iv",
    "index_price",
    "amount",
    "direction",
    "tick_direction",
)
_OPTIONAL_TRADE_FIELDS = ("contracts",)
_INDEX_PATH_HEADER = ["timestamp_ms", "underlying", "index_price"]
_INSTRUMENT_RE = re.compile(
    r"^(?P<underlying>[A-Z0-9]+)-(?P<expiry>\d{1,2}[A-Z]{3}\d{2})-"
    r"(?P<strike>\d+(?:\.\d+)?)-(?P<option_type>[CP])$"
)
_DEFAULT_MAX_TIME_GAP_MS = (
    int(PLAN_RECONSTRUCTION_CONFIG["max_trade_staleness_hours"]) * 60 * 60 * 1_000
)
_GRID_HOURS = tuple(int(value) for value in PLAN_RECONSTRUCTION_CONFIG["utc_hours"])


class VrpFreeHistoryCacheValidationError(Exception):
    """Raised when the free-history raw cache fails fail-closed validation."""


@dataclass(frozen=True, kw_only=True)
class ParsedDeribitOptionTrade:
    trade_id: str
    trade_seq: int
    instrument_name: str
    underlying: str
    expiry_token: str
    expiry_ms: int
    strike: float
    option_type: str
    timestamp_ms: int
    price: float
    mark_price: float
    iv: float
    index_price: float
    amount: float
    contracts: float | None
    direction: str
    tick_direction: int
    dte_days: float
    abs_log_moneyness: float


@dataclass(frozen=True, kw_only=True)
class IndexPathPoint:
    timestamp_ms: int
    underlying: str
    index_price: float


@dataclass(frozen=True, kw_only=True)
class VrpFreeHistoryDataset:
    manifest: Mapping[str, Any]
    raw_rows: tuple[dict[str, Any], ...]
    trades: tuple[ParsedDeribitOptionTrade, ...]
    index_path: tuple[IndexPathPoint, ...]


# ---------------------------------------------------------------------------
# small deterministic helpers
# ---------------------------------------------------------------------------
def sha256_text(text: str) -> str:
    import hashlib

    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _require(condition: bool, message: str) -> None:
    if not condition:
        raise VrpFreeHistoryCacheValidationError(message)


def _manifest_text(manifest: Mapping[str, Any]) -> str:
    return json.dumps(dict(manifest), indent=2, sort_keys=True) + "\n"


def _canonical_json(value: Mapping[str, Any]) -> str:
    return json.dumps(dict(value), sort_keys=True, separators=(",", ":"))


def _jsonl_text(rows: Sequence[Mapping[str, Any]]) -> str:
    return "".join(_canonical_json(row) + "\n" for row in rows)


def _fmt_num(value: float) -> str:
    out = float(value)
    _require(math.isfinite(out), f"numeric value must be finite, got {value!r}")
    return repr(out)


def _iso_ms(ts_ms: int) -> str:
    return datetime.fromtimestamp(ts_ms / 1000, tz=UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _parse_iso_ms(value: str) -> int:
    normalized = value[:-1] + "+00:00" if value.endswith("Z") else value
    dt = datetime.fromisoformat(normalized)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return int(dt.astimezone(UTC).timestamp() * 1000)


def _int_value(value: object, label: str, *, nonnegative: bool = True) -> int:
    if value is None or isinstance(value, bool) or not isinstance(value, (int, float, str)):
        raise VrpFreeHistoryCacheValidationError(f"{label} must be an integer")
    try:
        out = int(value)
    except (TypeError, ValueError) as exc:
        raise VrpFreeHistoryCacheValidationError(f"{label} must be an integer") from exc
    if nonnegative and out < 0:
        raise VrpFreeHistoryCacheValidationError(f"{label} must be non-negative")
    return out


def _finite_float(value: object, label: str) -> float:
    if value is None or isinstance(value, bool):
        raise VrpFreeHistoryCacheValidationError(f"{label} is required")
    try:
        out = float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError) as exc:
        raise VrpFreeHistoryCacheValidationError(f"{label} must be numeric") from exc
    if not math.isfinite(out):
        raise VrpFreeHistoryCacheValidationError(f"{label} must be finite")
    return out


def _positive_float(value: object, label: str) -> float:
    out = _finite_float(value, label)
    if out <= 0.0:
        raise VrpFreeHistoryCacheValidationError(f"{label} must be positive")
    return out


_NON_PRICED_QUOTE_FIELDS = ("iv", "mark_price")


def _is_non_priced_print(row: Mapping[str, Any]) -> bool:
    """True for non-priced Deribit prints (block/combo) that carry no usable quote.

    A row is non-priced when its ``iv`` or ``mark_price`` is missing/None/non-
    positive/non-finite. Such prints (e.g. block/combo trades) have no usable
    implied vol or fair quote for IV-surface reconstruction and are excluded +
    counted (never fabricated). Only iv/mark_price are treated as the benign
    no-quote class; index_price, amount, instrument, timestamp, etc. stay strict so
    the canonical parser still fails loud on genuine structural corruption. Rows
    whose iv/mark_price are present-but-non-numeric are NOT excluded here so the
    strict parser raises on them too.
    """
    for field in _NON_PRICED_QUOTE_FIELDS:
        if field not in row or row[field] is None:
            return True
        value = row[field]
        if isinstance(value, bool):
            return True
        try:
            num = float(value)
        except (TypeError, ValueError):
            return False
        if not math.isfinite(num) or num <= 0.0:
            return True
    return False


def partition_iv_bearing_trades(
    rows: Iterable[dict[str, Any]],
) -> tuple[tuple[dict[str, Any], ...], int]:
    """Partition usable priced rows from benign non-priced Deribit prints.

    Excludes and counts non-priced prints (missing/None/non-positive/non-finite
    ``iv`` or ``mark_price``) which carry no usable implied vol or fair quote
    for IV-surface reconstruction. Every other row passes through unchanged so the
    strict parser still fails loud on genuine structural corruption downstream.
    Exclusion is honest data omission, never fabrication.
    """

    usable_rows: list[dict[str, Any]] = []
    excluded_non_iv_count = 0
    for row in rows:
        if _is_non_priced_print(row):
            excluded_non_iv_count += 1
        else:
            usable_rows.append(row)
    return tuple(usable_rows), excluded_non_iv_count


def _nonnegative_float(value: object, label: str) -> float:
    out = _finite_float(value, label)
    if out < 0.0:
        raise VrpFreeHistoryCacheValidationError(f"{label} must be non-negative")
    return out


def _optional_positive_float(value: object, label: str) -> float | None:
    if value is None:
        return None
    return _positive_float(value, label)


def _coerce_source_quality(value: SourceQuality | str, label: str) -> SourceQuality:
    try:
        return value if isinstance(value, SourceQuality) else SourceQuality(str(value))
    except ValueError as exc:
        raise VrpFreeHistoryCacheValidationError(
            f"{label} invalid SourceQuality: {value!r}"
        ) from exc


def _source_quality_manifest(
    source_quality: Mapping[str, SourceQuality | str] | None,
) -> dict[str, str]:
    raw = source_quality or {
        "option_trades": SourceQuality.VENUE,
        "underlying_index": SourceQuality.VENUE,
    }
    out: dict[str, str] = {}
    for key, value in raw.items():
        out[str(key)] = _coerce_source_quality(value, f"source_quality[{key}]").value
    missing = {"option_trades", "underlying_index"} - set(out)
    _require(not missing, f"source_quality missing required keys: {sorted(missing)}")
    return dict(sorted(out.items()))


# ---------------------------------------------------------------------------
# strict parser
# ---------------------------------------------------------------------------
def parse_deribit_history_trade(row: Mapping[str, Any]) -> ParsedDeribitOptionTrade:
    """Parse one Deribit public history option trade, failing closed on drift."""

    for field in _REQUIRED_TRADE_FIELDS:
        if field not in row or row[field] is None:
            raise VrpFreeHistoryCacheValidationError(f"Deribit trade missing required {field}")

    trade_id = str(row["trade_id"])
    if not trade_id:
        raise VrpFreeHistoryCacheValidationError("trade_id must be non-empty")
    instrument_name = str(row["instrument_name"])
    instrument = _parse_instrument_name(instrument_name)
    timestamp_ms = _int_value(row["timestamp"], "timestamp")
    trade_seq = _int_value(row["trade_seq"], "trade_seq")
    tick_direction = _int_value(row["tick_direction"], "tick_direction", nonnegative=False)
    if tick_direction not in {0, 1, 2, 3}:
        raise VrpFreeHistoryCacheValidationError("tick_direction must be 0, 1, 2, or 3")
    strike = instrument["strike"]
    index_price = _positive_float(row["index_price"], "index_price")
    dte_days = (instrument["expiry_ms"] - timestamp_ms) / 86_400_000.0
    if dte_days < 0.0:
        raise VrpFreeHistoryCacheValidationError(
            f"{instrument_name} trade timestamp is after expiry"
        )

    direction = str(row["direction"]).lower()
    if direction not in {"buy", "sell"}:
        raise VrpFreeHistoryCacheValidationError(f"direction must be buy/sell, got {direction!r}")

    return ParsedDeribitOptionTrade(
        trade_id=trade_id,
        trade_seq=trade_seq,
        instrument_name=instrument_name,
        underlying=instrument["underlying"],
        expiry_token=instrument["expiry_token"],
        expiry_ms=instrument["expiry_ms"],
        strike=strike,
        option_type=instrument["option_type"],
        timestamp_ms=timestamp_ms,
        price=_nonnegative_float(row["price"], "price"),
        mark_price=_positive_float(row["mark_price"], "mark_price"),
        iv=_positive_float(row["iv"], "iv"),
        index_price=index_price,
        amount=_positive_float(row["amount"], "amount"),
        contracts=_optional_positive_float(row.get("contracts"), "contracts"),
        direction=direction,
        tick_direction=tick_direction,
        dte_days=dte_days,
        abs_log_moneyness=abs(math.log(strike / index_price)),
    )


def parse_deribit_history_trades(
    rows: Iterable[Mapping[str, Any]],
) -> tuple[ParsedDeribitOptionTrade, ...]:
    return tuple(sorted((parse_deribit_history_trade(row) for row in rows), key=_trade_sort_key))


def _parse_instrument_name(instrument_name: str) -> dict[str, Any]:
    match = _INSTRUMENT_RE.match(instrument_name)
    if not match:
        raise VrpFreeHistoryCacheValidationError(
            f"invalid Deribit option instrument_name: {instrument_name!r}"
        )
    expiry_token = match.group("expiry").upper()
    try:
        expiry_dt = datetime.strptime(expiry_token, "%d%b%y").replace(
            tzinfo=UTC, hour=8, minute=0, second=0, microsecond=0
        )
    except ValueError as exc:
        raise VrpFreeHistoryCacheValidationError(
            f"invalid Deribit option expiry token: {expiry_token!r}"
        ) from exc
    strike = _positive_float(match.group("strike"), "strike")
    option_type = "call" if match.group("option_type") == "C" else "put"
    return {
        "underlying": match.group("underlying").upper(),
        "expiry_token": expiry_token,
        "expiry_ms": int(expiry_dt.timestamp() * 1000),
        "strike": strike,
        "option_type": option_type,
    }


def _trade_sort_key(trade: ParsedDeribitOptionTrade) -> tuple[int, int, str, str]:
    return (trade.timestamp_ms, trade.trade_seq, trade.instrument_name, trade.trade_id)


def _raw_trade_sort_key(row: Mapping[str, Any]) -> tuple[int, int, str, str]:
    parsed = parse_deribit_history_trade(row)
    return _trade_sort_key(parsed)


def _dedupe_raw_rows(rows: Sequence[Mapping[str, Any]]) -> tuple[dict[str, Any], ...]:
    by_trade_id: dict[str, dict[str, Any]] = {}
    for row in rows:
        trade_id = str(row.get("trade_id") or "")
        if not trade_id:
            raise VrpFreeHistoryCacheValidationError("Deribit trade missing required trade_id")
        current = dict(row)
        previous = by_trade_id.get(trade_id)
        if previous is not None and _canonical_json(previous) != _canonical_json(current):
            raise VrpFreeHistoryCacheValidationError(f"conflicting duplicate trade_id {trade_id!r}")
        by_trade_id[trade_id] = current
    return tuple(sorted(by_trade_id.values(), key=_raw_trade_sort_key))


# ---------------------------------------------------------------------------
# coverage and exact underlying/index path
# ---------------------------------------------------------------------------
def build_coverage_manifest(
    trades: Sequence[ParsedDeribitOptionTrade],
    *,
    start_ts_ms: int,
    end_ts_ms: int,
    max_time_gap_ms: int = _DEFAULT_MAX_TIME_GAP_MS,
    stress_windows: Sequence[Mapping[str, Any]] | None = None,
) -> dict[str, Any]:
    """Build the deterministic coverage section for a raw-source manifest."""

    _validate_trade_sequence(trades, start_ts_ms, end_ts_ms)
    index_path = _index_path_from_trades(trades)
    _validate_time_gaps(index_path, max_time_gap_ms)
    grid = _snapshot_grid_coverage(
        trades,
        index_path,
        start_ts_ms=start_ts_ms,
        end_ts_ms=end_ts_ms,
        max_time_gap_ms=max_time_gap_ms,
    )
    stress = _stress_window_coverage(index_path, stress_windows)
    _require(not grid["missing_timestamps_ms"], "missing required 8h snapshot-grid coverage")
    _require(not stress["missing_window_ids"], "missing required stress-window coverage")

    return {
        "by_expiry": _coverage_counts(trades, lambda trade: _iso_ms(trade.expiry_ms)),
        "by_strike": _coverage_counts(trades, lambda trade: _fmt_num(trade.strike)),
        "by_option_type": _coverage_counts(trades, lambda trade: trade.option_type),
        "by_dte_bucket": _coverage_counts(trades, _dte_bucket),
        "by_moneyness_bucket": _coverage_counts(trades, _moneyness_bucket),
        "by_fold": _fold_coverage(trades),
        "trade_lattice": _trade_lattice(trades),
        "snapshot_grid_8h": grid,
        "stress_windows": stress,
    }


def _validate_trade_sequence(
    trades: Sequence[ParsedDeribitOptionTrade], start_ts_ms: int, end_ts_ms: int
) -> None:
    _require(bool(trades), "raw history cache requires at least one trade")
    _require(start_ts_ms <= end_ts_ms, "date_range start_ts_ms must be <= end_ts_ms")
    previous: tuple[int, int, str, str] | None = None
    underlyings = {trade.underlying for trade in trades}
    _require(len(underlyings) == 1, f"raw history cache expects one underlying, got {underlyings}")
    for trade in trades:
        _require(
            start_ts_ms <= trade.timestamp_ms <= end_ts_ms,
            f"trade {trade.trade_id} timestamp outside manifest date_range",
        )
        key = _trade_sort_key(trade)
        if previous is not None:
            _require(key > previous, "trades are not strictly sorted by timestamp/sequence/id")
        previous = key


_MAX_SAME_MS_INDEX_REL_DIFF = 0.002


def _index_path_from_trades(
    trades: Sequence[ParsedDeribitOptionTrade],
) -> tuple[IndexPathPoint, ...]:
    by_timestamp: dict[int, IndexPathPoint] = {}
    for trade in trades:
        point = IndexPathPoint(
            timestamp_ms=trade.timestamp_ms,
            underlying=trade.underlying,
            index_price=trade.index_price,
        )
        previous = by_timestamp.get(point.timestamp_ms)
        if previous is not None:
            _require(
                previous.underlying == point.underlying,
                f"conflicting underlying at timestamp {point.timestamp_ms}",
            )
            # Same-millisecond trades can report marginally different index_price as
            # the Deribit index ticks sub-millisecond. Tolerate that sub-ms noise
            # within a tight relative bound and deterministically keep the first
            # (stable-sorted) real observation; fail loud on a large conflict, which
            # signals genuine corruption rather than sub-ms index movement.
            rel_diff = abs(previous.index_price - point.index_price) / previous.index_price
            _require(
                rel_diff <= _MAX_SAME_MS_INDEX_REL_DIFF,
                f"conflicting index_price at timestamp {point.timestamp_ms}",
            )
            continue
        by_timestamp[point.timestamp_ms] = point
    return tuple(by_timestamp[ts] for ts in sorted(by_timestamp))


def _validate_time_gaps(points: Sequence[IndexPathPoint], max_time_gap_ms: int) -> None:
    _require(max_time_gap_ms > 0, "max_time_gap_ms must be positive")
    for i in range(1, len(points)):
        gap = points[i].timestamp_ms - points[i - 1].timestamp_ms
        _require(
            0 < gap <= max_time_gap_ms,
            f"underlying index time gap {gap}ms exceeds tolerance {max_time_gap_ms}ms",
        )


def _coverage_counts(
    trades: Sequence[ParsedDeribitOptionTrade], key_fn,
) -> dict[str, dict[str, Any]]:
    grouped: dict[str, list[ParsedDeribitOptionTrade]] = {}
    for trade in trades:
        grouped.setdefault(str(key_fn(trade)), []).append(trade)
    return {
        key: {
            "trade_count": len(values),
            "timestamps_ms": sorted({trade.timestamp_ms for trade in values}),
        }
        for key, values in sorted(grouped.items())
    }


def _dte_bucket(trade: ParsedDeribitOptionTrade) -> str:
    for name, bounds in PLAN_BINNING["dte_buckets_days"].items():
        lo, hi = float(bounds[0]), float(bounds[1])
        if lo <= trade.dte_days <= hi:
            return str(name)
    return "out_of_grid"


def _moneyness_bucket(trade: ParsedDeribitOptionTrade) -> str:
    for name, bounds in PLAN_BINNING["absolute_log_moneyness_buckets"].items():
        lo, hi = float(bounds[0]), float(bounds[1])
        if lo <= trade.abs_log_moneyness <= hi:
            return str(name)
    return "out_of_grid"


def _fold_coverage(trades: Sequence[ParsedDeribitOptionTrade]) -> dict[str, dict[str, Any]]:
    out: dict[str, dict[str, Any]] = {}
    for fold in PLAN_FOLDS:
        fold_id = str(fold["id"])
        train_start = _parse_iso_ms(str(fold["train_start"]))
        train_end = _parse_iso_ms(str(fold["train_end"]))
        test_start = _parse_iso_ms(str(fold["test_start"]))
        test_end = _parse_iso_ms(str(fold["test_end"]))
        train_ts = sorted(
            {t.timestamp_ms for t in trades if train_start <= t.timestamp_ms < train_end}
        )
        test_ts = sorted(
            {t.timestamp_ms for t in trades if test_start <= t.timestamp_ms < test_end}
        )
        out[fold_id] = {
            "train_count": sum(1 for t in trades if train_start <= t.timestamp_ms < train_end),
            "test_count": sum(1 for t in trades if test_start <= t.timestamp_ms < test_end),
            "train_timestamps_ms": train_ts,
            "test_timestamps_ms": test_ts,
        }
    return out


def _trade_lattice(trades: Sequence[ParsedDeribitOptionTrade]) -> list[dict[str, Any]]:
    grouped: dict[tuple[int, str, str, str, str], list[ParsedDeribitOptionTrade]] = {}
    for trade in trades:
        key = (
            trade.expiry_ms,
            _fmt_num(trade.strike),
            trade.option_type,
            _dte_bucket(trade),
            _moneyness_bucket(trade),
        )
        grouped.setdefault(key, []).append(trade)
    rows: list[dict[str, Any]] = []
    for key, values in sorted(grouped.items()):
        expiry_ms, strike, option_type, dte_bucket, moneyness_bucket = key
        rows.append(
            {
                "expiry_ms": expiry_ms,
                "expiry_utc": _iso_ms(expiry_ms),
                "strike": strike,
                "option_type": option_type,
                "dte_bucket": dte_bucket,
                "moneyness_bucket": moneyness_bucket,
                "trade_count": len(values),
                "timestamps_ms": sorted({trade.timestamp_ms for trade in values}),
            }
        )
    return rows


def _snapshot_grid_coverage(
    trades: Sequence[ParsedDeribitOptionTrade],
    points: Sequence[IndexPathPoint],
    *,
    start_ts_ms: int,
    end_ts_ms: int,
    max_time_gap_ms: int,
) -> dict[str, Any]:
    required = _required_grid_timestamps(trades, start_ts_ms, end_ts_ms)
    covered: list[int] = []
    missing: list[int] = []
    support_by_grid: dict[str, int] = {}
    point_index = 0
    latest: IndexPathPoint | None = None
    for grid_ts in required:
        while point_index < len(points) and points[point_index].timestamp_ms <= grid_ts:
            latest = points[point_index]
            point_index += 1
        if latest is not None and grid_ts - latest.timestamp_ms <= max_time_gap_ms:
            covered.append(grid_ts)
            support_by_grid[str(grid_ts)] = latest.timestamp_ms
        else:
            missing.append(grid_ts)
    return {
        "cadence_hours": PLAN_RECONSTRUCTION_CONFIG["cadence_hours"],
        "utc_hours": list(_GRID_HOURS),
        "max_trade_staleness_hours": PLAN_RECONSTRUCTION_CONFIG["max_trade_staleness_hours"],
        "required_timestamps_ms": required,
        "covered_timestamps_ms": covered,
        "missing_timestamps_ms": missing,
        "supporting_trade_timestamps_ms_by_grid": support_by_grid,
        "status": "pass" if not missing else "fail",
    }


def _required_grid_timestamps(
    trades: Sequence[ParsedDeribitOptionTrade], start_ts_ms: int, end_ts_ms: int
) -> list[int]:
    start_day = datetime.fromtimestamp(start_ts_ms / 1000, tz=UTC).date()
    end_day = datetime.fromtimestamp(end_ts_ms / 1000, tz=UTC).date()
    out: set[int] = set()
    current = start_day
    while current <= end_day:
        for hour in _GRID_HOURS:
            day_start = datetime.combine(current, datetime.min.time(), tzinfo=UTC)
            ts = _datetime_ms(day_start + timedelta(hours=hour))
            if start_ts_ms <= ts <= end_ts_ms:
                out.add(ts)
        current += timedelta(days=1)
    if PLAN_RECONSTRUCTION_CONFIG["include_expiry_settlement_timestamps"]:
        for trade in trades:
            if start_ts_ms <= trade.expiry_ms <= end_ts_ms:
                out.add(trade.expiry_ms)
    return sorted(out)


def _datetime_ms(value: datetime) -> int:
    return int(value.astimezone(UTC).timestamp() * 1000)


def _stress_window_coverage(
    points: Sequence[IndexPathPoint], stress_windows: Sequence[Mapping[str, Any]] | None
) -> dict[str, Any]:
    windows = [_normalize_stress_window(i, value) for i, value in enumerate(stress_windows or ())]
    covered: list[str] = []
    missing: list[str] = []
    for window in windows:
        has_point = any(
            int(window["start_ts_ms"]) <= point.timestamp_ms <= int(window["end_ts_ms"])
            for point in points
        )
        (covered if has_point else missing).append(str(window["id"]))
    return {
        "status": "not_requested" if not windows else ("pass" if not missing else "fail"),
        "windows": windows,
        "covered_window_ids": covered,
        "missing_window_ids": missing,
    }


def _normalize_stress_window(index: int, value: Mapping[str, Any]) -> dict[str, Any]:
    start = value.get("start_ts_ms", value.get("start"))
    end = value.get("end_ts_ms", value.get("end"))
    if isinstance(start, str):
        start_ts_ms = _parse_iso_ms(start)
    else:
        start_ts_ms = _int_value(start, "stress_window.start_ts_ms")
    if isinstance(end, str):
        end_ts_ms = _parse_iso_ms(end)
    else:
        end_ts_ms = _int_value(end, "stress_window.end_ts_ms")
    _require(start_ts_ms <= end_ts_ms, "stress_window start must be <= end")
    return {
        "id": str(value.get("id", f"stress_{index + 1}")),
        "start_ts_ms": start_ts_ms,
        "end_ts_ms": end_ts_ms,
    }


def _index_path_text(points: Sequence[IndexPathPoint]) -> str:
    buf = io.StringIO()
    writer = csv.writer(buf, lineterminator="\n")
    writer.writerow(_INDEX_PATH_HEADER)
    for point in points:
        writer.writerow([point.timestamp_ms, point.underlying, _fmt_num(point.index_price)])
    return buf.getvalue()


def _parse_index_path(text: str) -> tuple[IndexPathPoint, ...]:
    rows = list(csv.reader(text.splitlines()))
    _require(bool(rows), "index_path.csv is empty")
    _require(rows[0] == _INDEX_PATH_HEADER, "index_path.csv header mismatch")
    points = tuple(
        IndexPathPoint(
            timestamp_ms=_int_value(row[0], "index_path.timestamp_ms"),
            underlying=str(row[1]),
            index_price=_positive_float(row[2], "index_path.index_price"),
        )
        for row in rows[1:]
    )
    _require(points == tuple(sorted(points, key=lambda p: p.timestamp_ms)), "index_path not sorted")
    return points


def _index_path_manifest(points: Sequence[IndexPathPoint], text: str) -> dict[str, Any]:
    _require(bool(points), "underlying index path must be non-empty")
    underlyings = sorted({point.underlying for point in points})
    _require(len(underlyings) == 1, "underlying index path must contain one underlying")
    return {
        "file": INDEX_PATH_FILE,
        "source_field": "trade_rows.index_price",
        "source": "Deribit public history trade index_price",
        "underlying": underlyings[0],
        "exact": True,
        "fabricated": False,
        "row_count": len(points),
        "date_range": {
            "start_ts_ms": points[0].timestamp_ms,
            "end_ts_ms": points[-1].timestamp_ms,
        },
        "timestamps_ms": [point.timestamp_ms for point in points],
        "sha256": sha256_text(text),
    }


# ---------------------------------------------------------------------------
# write/load
# ---------------------------------------------------------------------------
def write_vrp_free_history_cache(
    raw_source_root: str | Path,
    scenario_id: str,
    *,
    raw_rows: Sequence[Mapping[str, Any]],
    currency: str,
    start_ts_ms: int,
    end_ts_ms: int,
    download_timestamp_ms: int,
    source_ids: Sequence[str],
    source_url_ids: Sequence[str],
    source_quality: Mapping[str, SourceQuality | str] | None = None,
    acquisition_tool_version: str = GENERATOR_VERSION,
    max_time_gap_ms: int = _DEFAULT_MAX_TIME_GAP_MS,
    stress_windows: Sequence[Mapping[str, Any]] | None = None,
) -> Path:
    """Write a manifest-hashed raw Deribit-history cache and return its directory."""

    _require(scenario_id == DEFAULT_SCENARIO_ID, f"unexpected scenario_id {scenario_id!r}")
    _require(currency.upper() == "ETH", "Phase-1 free VRP history cache is ETH-only")
    rows = _dedupe_raw_rows(raw_rows)
    trades = parse_deribit_history_trades(rows)
    _validate_trade_sequence(trades, start_ts_ms, end_ts_ms)
    index_path = _index_path_from_trades(trades)
    _validate_time_gaps(index_path, max_time_gap_ms)
    coverage = build_coverage_manifest(
        trades,
        start_ts_ms=start_ts_ms,
        end_ts_ms=end_ts_ms,
        max_time_gap_ms=max_time_gap_ms,
        stress_windows=stress_windows,
    )
    source_quality_payload = _source_quality_manifest(source_quality)
    source_urls = sorted(set(map(str, source_url_ids)))

    trades_text = _jsonl_text(rows)
    index_text = _index_path_text(index_path)
    scenario_dir = Path(raw_source_root) / scenario_id
    scenario_dir.mkdir(parents=True, exist_ok=True)
    (scenario_dir / TRADES_FILE).write_text(trades_text, encoding="utf-8")
    (scenario_dir / INDEX_PATH_FILE).write_text(index_text, encoding="utf-8")

    manifest = {
        "schema_version": SCHEMA_VERSION,
        "manifest_kind": RAW_MANIFEST_KIND,
        "scenario_id": scenario_id,
        "currency": currency.upper(),
        "exchange": "deribit",
        "endpoint": "public/get_last_trades_by_currency_and_time",
        "date_range": {"start_ts_ms": start_ts_ms, "end_ts_ms": end_ts_ms},
        "download_timestamp_ms": _int_value(download_timestamp_ms, "download_timestamp_ms"),
        "source_ids": sorted(set(map(str, source_ids))),
        "source_url_ids": source_urls,
        "source_uri_ids": source_urls,
        "acquisition_tool_version": acquisition_tool_version,
        "source_quality": source_quality_payload,
        "required_column_coverage": _required_column_coverage(rows),
        "sha256_by_file": {
            TRADES_FILE: sha256_text(trades_text),
            INDEX_PATH_FILE: sha256_text(index_text),
        },
        "file_sizes": {
            TRADES_FILE: len(trades_text.encode("utf-8")),
            INDEX_PATH_FILE: len(index_text.encode("utf-8")),
        },
        "row_counts": {
            TRADES_FILE: len(rows),
            INDEX_PATH_FILE: len(index_path),
            "trades": len(trades),
            "underlying_index_points": len(index_path),
        },
        "coverage": coverage,
        "underlying_index_path": _index_path_manifest(index_path, index_text),
        "validation": {
            "max_time_gap_ms": max_time_gap_ms,
            "no_future_trades": PLAN_RECONSTRUCTION_CONFIG["no_future_trades"],
            "missing_required_coverage": PLAN_RECONSTRUCTION_CONFIG[
                "missing_required_coverage"
            ],
        },
        "cache_fabricated": False,
        "no_fabrication_policy": (
            "Observed Deribit public history rows only; missing IV/index/amount/time coverage "
            "raises VrpFreeHistoryCacheValidationError and is never filled."
        ),
    }
    (scenario_dir / "manifest.json").write_text(_manifest_text(manifest), encoding="utf-8")
    return scenario_dir


def load_vrp_free_history_manifest(raw_source_root: str | Path, scenario_id: str) -> dict[str, Any]:
    scenario_dir = Path(raw_source_root) / scenario_id
    manifest = _read_json_object(scenario_dir / "manifest.json")
    _validate_manifest_header(manifest, scenario_id)
    sha_map = _require_mapping(manifest.get("sha256_by_file"), "sha256_by_file missing")
    for filename in sorted(sha_map):
        _verify_sha(scenario_dir, manifest, str(filename))
    return manifest


def load_vrp_free_history_cache(
    raw_source_root: str | Path,
    scenario_id: str,
) -> VrpFreeHistoryDataset:
    """Load and fail-closed validate a raw free-history cache without network access."""

    scenario_dir = Path(raw_source_root) / scenario_id
    manifest = load_vrp_free_history_manifest(raw_source_root, scenario_id)
    trades_text = _verify_sha(scenario_dir, manifest, TRADES_FILE)
    index_text = _verify_sha(scenario_dir, manifest, INDEX_PATH_FILE)
    raw_rows = _parse_jsonl_rows(trades_text)
    trades = parse_deribit_history_trades(raw_rows)
    index_path = _parse_index_path(index_text)
    _require(
        index_path == _index_path_from_trades(trades),
        "index_path.csv does not match trades",
    )

    row_counts = _require_mapping(manifest.get("row_counts"), "row_counts missing")
    _require(row_counts.get(TRADES_FILE) == len(raw_rows), "manifest trades row_count mismatch")
    _require(
        row_counts.get(INDEX_PATH_FILE) == len(index_path),
        "manifest index row_count mismatch",
    )

    date_range = _require_mapping(manifest.get("date_range"), "date_range missing")
    validation = _require_mapping(manifest.get("validation"), "validation missing")
    max_gap = _int_value(validation.get("max_time_gap_ms"), "validation.max_time_gap_ms")
    coverage = build_coverage_manifest(
        trades,
        start_ts_ms=_int_value(date_range.get("start_ts_ms"), "date_range.start_ts_ms"),
        end_ts_ms=_int_value(date_range.get("end_ts_ms"), "date_range.end_ts_ms"),
        max_time_gap_ms=max_gap,
        stress_windows=_manifest_stress_windows(manifest),
    )
    _require(manifest.get("coverage") == coverage, "coverage manifest mismatch")
    _require(
        manifest.get("underlying_index_path") == _index_path_manifest(index_path, index_text),
        "underlying_index_path manifest mismatch",
    )
    _require(manifest.get("cache_fabricated") is False, "cache_fabricated must be false")
    return VrpFreeHistoryDataset(
        manifest=manifest,
        raw_rows=raw_rows,
        trades=trades,
        index_path=index_path,
    )


def _required_column_coverage(rows: Sequence[Mapping[str, Any]]) -> dict[str, bool]:
    fields = _REQUIRED_TRADE_FIELDS + _OPTIONAL_TRADE_FIELDS
    return {field: all(field in row and row[field] is not None for row in rows) for field in fields}


def _read_json_object(path: Path) -> dict[str, Any]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except OSError as exc:
        raise VrpFreeHistoryCacheValidationError(f"cannot read {path}") from exc
    except json.JSONDecodeError as exc:
        raise VrpFreeHistoryCacheValidationError(f"invalid JSON in {path}") from exc
    if not isinstance(data, dict):
        raise VrpFreeHistoryCacheValidationError(f"{path} must contain a JSON object")
    return data


def _validate_manifest_header(manifest: Mapping[str, Any], scenario_id: str) -> None:
    _require(manifest.get("schema_version") == SCHEMA_VERSION, "manifest schema_version mismatch")
    _require(manifest.get("manifest_kind") == RAW_MANIFEST_KIND, "manifest_kind mismatch")
    _require(manifest.get("scenario_id") == scenario_id, "manifest scenario_id mismatch")
    _require(manifest.get("currency") == "ETH", "manifest currency must be ETH")
    _require(manifest.get("cache_fabricated") is False, "cache_fabricated must be false")
    _source_quality_manifest(
        _require_mapping(manifest.get("source_quality"), "source_quality missing")
    )


def _require_mapping(value: object, message: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise VrpFreeHistoryCacheValidationError(message)
    return value


def _verify_sha(scenario_dir: Path, manifest: Mapping[str, Any], filename: str) -> str:
    sha_map = _require_mapping(manifest.get("sha256_by_file"), "sha256_by_file missing")
    expected = sha_map.get(filename)
    _require(isinstance(expected, str), f"missing sha256 for {filename}")
    path = scenario_dir / filename
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise VrpFreeHistoryCacheValidationError(f"missing cache file {filename}") from exc
    actual = sha256_text(text)
    _require(actual == expected, f"sha256 mismatch for {filename}: {actual} != {expected}")
    return text


def _parse_jsonl_rows(text: str) -> tuple[dict[str, Any], ...]:
    rows: list[dict[str, Any]] = []
    for line_no, line in enumerate(text.splitlines(), start=1):
        try:
            value = json.loads(line)
        except json.JSONDecodeError as exc:
            raise VrpFreeHistoryCacheValidationError(f"invalid JSONL row {line_no}") from exc
        if not isinstance(value, dict):
            raise VrpFreeHistoryCacheValidationError(f"JSONL row {line_no} must be an object")
        rows.append(value)
    _require(bool(rows), "trades.jsonl must be non-empty")
    _require(tuple(rows) == _dedupe_raw_rows(rows), "trades.jsonl is not canonical sorted/deduped")
    return tuple(rows)


def _manifest_stress_windows(manifest: Mapping[str, Any]) -> Sequence[Mapping[str, Any]]:
    coverage = _require_mapping(manifest.get("coverage"), "coverage missing")
    stress = _require_mapping(coverage.get("stress_windows"), "coverage.stress_windows missing")
    windows = stress.get("windows", [])
    _require(isinstance(windows, list), "coverage.stress_windows.windows must be a list")
    return windows


__all__ = [
    "GENERATOR_VERSION",
    "INDEX_PATH_FILE",
    "RAW_MANIFEST_KIND",
    "SCHEMA_VERSION",
    "TRADES_FILE",
    "IndexPathPoint",
    "ParsedDeribitOptionTrade",
    "VrpFreeHistoryCacheValidationError",
    "VrpFreeHistoryDataset",
    "build_coverage_manifest",
    "load_vrp_free_history_cache",
    "load_vrp_free_history_manifest",
    "parse_deribit_history_trade",
    "parse_deribit_history_trades",
    "sha256_text",
    "write_vrp_free_history_cache",
]

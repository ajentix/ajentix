"""Tardis-free Deribit options-chain spread calibration for VRP-free research.

The module ingests local, already-downloaded Tardis FREE options_chain CSV samples
and derives deterministic two-leg spread-cost quantiles. It never fetches data and
never fabricates missing bid/ask, index, regime, or coverage inputs; insufficient
bins resolve through the frozen fallback order or remain INCONCLUSIVE.
"""

from __future__ import annotations

import ast
import csv
import io
import json
import math
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, NoReturn

from ajentix_quant.research.vrp_free_preregistration import (
    DEFAULT_SCENARIO_ID,
    PLAN_BINNING,
    PLAN_COST_BUDGET_BAR,
    PLAN_FAIL_CLOSED_RULES,
    PLAN_FOLD_CAUSAL_CALIBRATION_RULE,
    PLAN_FOLDS,
    PLAN_REGIME_LABELS,
    PLAN_UNIT_CONVERSIONS,
    TARDIS_SAMPLE_MONTHS,
)

SCHEMA_VERSION = "aq-vrp-free-spread-calibration-v1"
MANIFEST_KIND = "spread_calibration"
GENERATOR_VERSION = "ajentix-quant/g003-tardis-free-spread-calibration-v1"
SPREAD_BINS_FILE = "spread_bins.csv"
STATUS_RESOLVED = "RESOLVED"
STATUS_INCONCLUSIVE = "INCONCLUSIVE"

_SPREAD_BINS_HEADER = [
    "fold_id",
    "fold_train_end",
    "sample_cutoff_ms",
    "requested_option_type",
    "requested_dte_bucket",
    "requested_moneyness_bucket",
    "requested_regime_label",
    "status",
    "resolved_level",
    "resolved_option_type",
    "resolved_dte_bucket",
    "resolved_moneyness_bucket",
    "resolved_regime_label",
    "p50_round_trip_structure_spread_usd",
    "p75_round_trip_structure_spread_usd",
    "sample_count",
    "distinct_month_count",
    "sample_months",
    "reason",
]

_SYMBOL_RE = re.compile(
    r"^(?P<underlying>[A-Z0-9]+)-(?P<expiry>\d{1,2}[A-Z]{3}\d{2})-"
    r"(?P<strike>\d+(?:\.\d+)?)-(?P<option_type>[CP])$"
)
_EXACT_LEVEL = str(PLAN_BINNING["fallback_order"][0])
_SAMPLE_MONTH_SET = set(TARDIS_SAMPLE_MONTHS)


class TardisFreeSpreadCalibrationError(Exception):
    """Raised when Tardis-free spread calibration inputs fail closed."""


@dataclass(frozen=True, kw_only=True)
class TardisOptionLegSample:
    sample_timestamp_ms: int
    sample_month: str
    instrument_name: str
    underlying: str
    expiry_ms: int
    strike: float
    option_type: str
    bid_price: float
    ask_price: float
    index_price_usd: float
    contract_multiplier: float
    quantity: float
    trailing_30d_rv_annualized: float
    abs_24h_return: float
    dte_days: float
    abs_log_moneyness: float
    dte_bucket: str
    moneyness_bucket: str
    regime_label: str
    structure_sample_id: str | None = None
    leg_role: str | None = None

    @property
    def round_trip_leg_crossing_usd(self) -> float:
        spread_price_eth = max(self.ask_price - self.bid_price, 0.0)
        return spread_price_eth * self.index_price_usd * self.contract_multiplier * self.quantity


@dataclass(frozen=True, kw_only=True)
class StructureSpreadSample:
    sample_id: str
    sample_timestamp_ms: int
    sample_month: str
    option_type: str
    dte_bucket: str
    moneyness_bucket: str
    regime_label: str
    round_trip_structure_spread_usd: float
    leg_instruments: tuple[str, str]


@dataclass(frozen=True, kw_only=True)
class SpreadQuantileResolution:
    requested_option_type: str
    requested_dte_bucket: str
    requested_moneyness_bucket: str
    requested_regime_label: str
    status: str
    resolved_level: str
    p50_round_trip_structure_spread_usd: float | None
    p75_round_trip_structure_spread_usd: float | None
    sample_count: int
    distinct_month_count: int
    sample_months: tuple[str, ...]
    reason: str
    sample_cutoff_ms: int | None = None
    fold_id: str | None = None
    fold_train_end: str | None = None

    def as_csv_row(self) -> list[str]:
        fields = (
            set(_level_fields(self.resolved_level))
            if self.resolved_level != "fail_closed"
            else set()
        )
        resolved = {
            "option_type": self.requested_option_type if "option_type" in fields else "",
            "dte_bucket": self.requested_dte_bucket if "dte_bucket" in fields else "",
            "moneyness_bucket": (
                self.requested_moneyness_bucket if "moneyness_bucket" in fields else ""
            ),
            "regime_label": self.requested_regime_label if "regime_label" in fields else "",
        }
        return [
            self.fold_id or "",
            self.fold_train_end or "",
            "" if self.sample_cutoff_ms is None else str(self.sample_cutoff_ms),
            self.requested_option_type,
            self.requested_dte_bucket,
            self.requested_moneyness_bucket,
            self.requested_regime_label,
            self.status,
            self.resolved_level,
            resolved["option_type"],
            resolved["dte_bucket"],
            resolved["moneyness_bucket"],
            resolved["regime_label"],
            _fmt_optional_num(self.p50_round_trip_structure_spread_usd),
            _fmt_optional_num(self.p75_round_trip_structure_spread_usd),
            str(self.sample_count),
            str(self.distinct_month_count),
            ";".join(self.sample_months),
            self.reason,
        ]


@dataclass(frozen=True, kw_only=True)
class _GroupStats:
    p50: float
    p75: float
    sample_count: int
    distinct_month_count: int
    sample_months: tuple[str, ...]
    reason: str


# ---------------------------------------------------------------------------
# deterministic primitives
# ---------------------------------------------------------------------------
def sha256_text(text: str) -> str:
    import hashlib

    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _require(condition: bool, message: str) -> None:
    if not condition:
        raise TardisFreeSpreadCalibrationError(message)


def _manifest_text(manifest: Mapping[str, Any]) -> str:
    return json.dumps(dict(manifest), indent=2, sort_keys=True) + "\n"


def _fmt_num(value: float) -> str:
    out = float(value)
    _require(math.isfinite(out), f"numeric value must be finite, got {value!r}")
    return repr(out)


def _fmt_optional_num(value: float | None) -> str:
    return "" if value is None else _fmt_num(value)


def _finite_float(value: object, label: str) -> float:
    if value is None or isinstance(value, bool):
        raise TardisFreeSpreadCalibrationError(f"{label} is required")
    try:
        out = float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError) as exc:
        raise TardisFreeSpreadCalibrationError(f"{label} must be numeric") from exc
    if not math.isfinite(out):
        raise TardisFreeSpreadCalibrationError(f"{label} must be finite")
    return out


def _nonnegative_float(value: object, label: str) -> float:
    out = _finite_float(value, label)
    if out < 0.0:
        raise TardisFreeSpreadCalibrationError(f"{label} must be non-negative")
    return out


def _positive_float(value: object, label: str) -> float:
    out = _finite_float(value, label)
    if out <= 0.0:
        raise TardisFreeSpreadCalibrationError(f"{label} must be positive")
    return out


def _optional_row_value(row: Mapping[str, str], names: Sequence[str]) -> str | None:
    for name in names:
        value = row.get(name)
        if value is not None and str(value).strip() != "":
            return str(value).strip()
    return None


def _row_value(row: Mapping[str, str], names: Sequence[str], label: str) -> str:
    value = _optional_row_value(row, names)
    if value is None:
        raise TardisFreeSpreadCalibrationError(f"missing required {label}")
    return value


def _parse_timestamp_ms(value: str, label: str) -> int:
    raw = str(value).strip()
    if not raw:
        raise TardisFreeSpreadCalibrationError(f"{label} is required")
    try:
        numeric = float(raw)
    except ValueError:
        normalized = raw[:-1] + "+00:00" if raw.endswith("Z") else raw
        try:
            dt = datetime.fromisoformat(normalized)
        except ValueError as exc:
            raise TardisFreeSpreadCalibrationError(f"{label} must be ISO8601 or epoch") from exc
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=UTC)
        return int(dt.astimezone(UTC).timestamp() * 1000)

    if not math.isfinite(numeric) or numeric < 0:
        raise TardisFreeSpreadCalibrationError(f"{label} must be a non-negative finite epoch")
    if numeric >= 10_000_000_000_000_000:
        return int(numeric / 1_000_000)  # nanoseconds
    if numeric >= 10_000_000_000_000:
        return int(numeric / 1_000)  # microseconds
    if numeric >= 10_000_000_000:
        return int(numeric)  # milliseconds
    return int(numeric * 1000)  # seconds


def _parse_expiry_ms(value: str, label: str) -> int:
    raw = str(value).strip().upper()
    if _SYMBOL_RE.match(f"ETH-{raw}-1-C"):
        try:
            dt = datetime.strptime(raw, "%d%b%y").replace(
                tzinfo=UTC, hour=8, minute=0, second=0, microsecond=0
            )
        except ValueError as exc:
            raise TardisFreeSpreadCalibrationError(f"invalid {label}: {value!r}") from exc
        return int(dt.timestamp() * 1000)
    return _parse_timestamp_ms(value, label)


def _iso_ms(ts_ms: int) -> str:
    return datetime.fromtimestamp(ts_ms / 1000, tz=UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _sample_month(sample_timestamp_ms: int, explicit: str | None) -> str:
    derived = datetime.fromtimestamp(sample_timestamp_ms / 1000, tz=UTC).strftime(
        "%Y-%m-01"
    )
    if explicit is not None and explicit.strip() and explicit.strip() != derived:
        raise TardisFreeSpreadCalibrationError(
            f"sample_month label {explicit!r} does not match "
            f"timestamp-derived month {derived!r}"
        )
    _require(derived in _SAMPLE_MONTH_SET, f"sample_month {derived!r} is outside frozen list")
    return derived


def _parse_symbol(symbol: str | None) -> dict[str, Any] | None:
    if not symbol:
        return None
    match = _SYMBOL_RE.match(symbol.strip().upper())
    if not match:
        return None
    expiry_ms = _parse_expiry_ms(match.group("expiry"), "instrument expiry")
    option_type = "call" if match.group("option_type") == "C" else "put"
    return {
        "underlying": match.group("underlying").upper(),
        "expiry_ms": expiry_ms,
        "strike": _positive_float(match.group("strike"), "instrument strike"),
        "option_type": option_type,
    }


def dte_days_to_bucket(dte_days: float) -> str:
    """Return the frozen DTE bucket name or ``out_of_grid``."""

    dte = _finite_float(dte_days, "dte_days")
    for name, bounds in PLAN_BINNING["dte_buckets_days"].items():
        lo, hi = float(bounds[0]), float(bounds[1])
        if lo <= dte <= hi:
            return str(name)
    return "out_of_grid"


def abs_log_moneyness_to_bucket(abs_log_moneyness: float) -> str:
    """Return the frozen absolute-log-moneyness bucket name or ``out_of_grid``."""

    value = _finite_float(abs_log_moneyness, "abs_log_moneyness")
    if value < 0.0:
        raise TardisFreeSpreadCalibrationError("abs_log_moneyness must be non-negative")
    for name, bounds in PLAN_BINNING["absolute_log_moneyness_buckets"].items():
        lo, hi = float(bounds[0]), float(bounds[1])
        if lo <= value <= hi:
            return str(name)
    return "out_of_grid"


def regime_label(trailing_30d_rv_annualized: float, abs_24h_return: float) -> str:
    """Classify volatility regime by evaluating the frozen PLAN_REGIME_LABELS rules."""

    env = {
        "trailing_30d_rv_annualized": _nonnegative_float(
            trailing_30d_rv_annualized, "trailing_30d_rv_annualized"
        ),
        "abs_24h_return": _nonnegative_float(abs_24h_return, "abs_24h_return"),
    }
    for label in ("tail", "high_vol", "normal"):
        if _eval_regime_expr(PLAN_REGIME_LABELS[label], env):
            return label
    raise TardisFreeSpreadCalibrationError("frozen regime rules did not match any label")


def _eval_regime_expr(expr: str, env: Mapping[str, float]) -> bool:
    tree = ast.parse(expr, mode="eval")
    return bool(_eval_regime_node(tree.body, env))


def _eval_regime_node(node: ast.AST, env: Mapping[str, float]) -> bool | float:
    if isinstance(node, ast.BoolOp):
        values = [bool(_eval_regime_node(value, env)) for value in node.values]
        if isinstance(node.op, ast.And):
            return all(values)
        if isinstance(node.op, ast.Or):
            return any(values)
    if isinstance(node, ast.Compare):
        left = float(_eval_regime_node(node.left, env))
        for op, comparator in zip(node.ops, node.comparators, strict=True):
            right = float(_eval_regime_node(comparator, env))
            if isinstance(op, ast.Lt):
                ok = left < right
            elif isinstance(op, ast.LtE):
                ok = left <= right
            elif isinstance(op, ast.Gt):
                ok = left > right
            elif isinstance(op, ast.GtE):
                ok = left >= right
            else:
                raise TardisFreeSpreadCalibrationError("unsupported regime comparison operator")
            if not ok:
                return False
            left = right
        return True
    if isinstance(node, ast.Name):
        if node.id not in env:
            raise TardisFreeSpreadCalibrationError(f"unknown regime variable {node.id!r}")
        return env[node.id]
    if isinstance(node, ast.Constant) and isinstance(node.value, int | float):
        return float(node.value)
    raise TardisFreeSpreadCalibrationError("unsupported frozen regime expression")


def nearest_rank_quantile(values: Sequence[float], probability: float) -> float:
    """Return a deterministic nearest-rank empirical quantile."""

    _require(bool(values), "quantile requires at least one value")
    _require(0.0 < probability <= 1.0, "quantile probability must be in (0, 1]")
    ordered = sorted(_finite_float(value, "quantile value") for value in values)
    rank = max(1, math.ceil(probability * len(ordered)))
    return ordered[rank - 1]


# ---------------------------------------------------------------------------
# CSV ingestion and structure-sample construction
# ---------------------------------------------------------------------------
def load_tardis_free_option_leg_samples(
    csv_paths: Sequence[str | Path],
) -> tuple[TardisOptionLegSample, ...]:
    """Load local Tardis options_chain CSV rows into strict leg samples."""

    legs: list[TardisOptionLegSample] = []
    for csv_path in csv_paths:
        path = Path(csv_path)
        try:
            text = path.read_text(encoding="utf-8")
        except OSError as exc:
            raise TardisFreeSpreadCalibrationError(f"cannot read Tardis CSV {path}") from exc
        reader = csv.DictReader(text.splitlines())
        _require(reader.fieldnames is not None, f"{path} has no CSV header")
        for line_no, row in enumerate(reader, start=2):
            try:
                legs.append(_parse_leg_row(row, source=path.name, line_no=line_no))
            except TardisFreeSpreadCalibrationError as exc:
                raise TardisFreeSpreadCalibrationError(
                    f"{path.name} row {line_no}: {exc}"
                ) from exc
    return tuple(sorted(legs, key=_leg_sort_key))


def build_structure_spread_samples(
    legs: Sequence[TardisOptionLegSample],
) -> tuple[StructureSpreadSample, ...]:
    """Build deterministic two-leg structure spread samples from real bid/ask legs."""

    explicit: dict[str, list[TardisOptionLegSample]] = {}
    unassigned: list[TardisOptionLegSample] = []
    for leg in legs:
        if leg.structure_sample_id:
            explicit.setdefault(leg.structure_sample_id, []).append(leg)
        else:
            unassigned.append(leg)

    samples: list[StructureSpreadSample] = []
    for sample_id, group in sorted(explicit.items()):
        _require(len(group) == 2, f"structure_sample_id {sample_id!r} must have exactly two legs")
        ordered = sorted(group, key=_leg_sort_key)
        sample = _structure_from_legs(sample_id, (ordered[0], ordered[1]))
        if sample is not None:
            samples.append(sample)

    grouped: dict[tuple[int, int, str, str, str], list[TardisOptionLegSample]] = {}
    for leg in unassigned:
        if leg.dte_bucket == "out_of_grid":
            continue
        grouped.setdefault(
            (
                leg.sample_timestamp_ms,
                leg.expiry_ms,
                leg.option_type,
                leg.dte_bucket,
                leg.regime_label,
            ),
            [],
        ).append(leg)

    for key, group in sorted(grouped.items()):
        ordered = sorted(group, key=lambda leg: (leg.strike, leg.instrument_name))
        for index in range(1, len(ordered)):
            left, right = ordered[index - 1], ordered[index]
            sample_id = "derived:{}:{}:{}:{}:{}:{:04d}".format(*key, index)
            sample = _structure_from_legs(sample_id, (left, right))
            if sample is not None:
                samples.append(sample)

    return tuple(sorted(samples, key=_structure_sort_key))


def load_tardis_free_structure_samples(
    csv_paths: Sequence[str | Path],
) -> tuple[StructureSpreadSample, ...]:
    """Load local Tardis CSV files and return deterministic two-leg spread samples."""

    return build_structure_spread_samples(load_tardis_free_option_leg_samples(csv_paths))


def _parse_leg_row(
    row: Mapping[str, str], *, source: str, line_no: int
) -> TardisOptionLegSample:
    symbol = _optional_row_value(row, ("instrument_name", "symbol", "instrument", "instrument_id"))
    parsed_symbol = _parse_symbol(symbol)
    sample_timestamp_ms = _parse_timestamp_ms(
        _row_value(
            row,
            ("sample_timestamp_ms", "sample_timestamp", "timestamp", "local_timestamp"),
            "sample_timestamp",
        ),
        "sample_timestamp",
    )
    sample_month = _sample_month(
        sample_timestamp_ms,
        _optional_row_value(row, ("sample_month", "month", "sample_date")),
    )

    option_type_raw = _optional_row_value(row, ("option_type", "type", "option_kind"))
    option_type = _coerce_option_type(
        option_type_raw or (parsed_symbol["option_type"] if parsed_symbol else None)
    )
    strike_raw = _optional_row_value(row, ("strike", "strike_price"))
    strike = _positive_float(
        strike_raw
        if strike_raw is not None
        else (parsed_symbol["strike"] if parsed_symbol else None),
        "strike",
    )
    expiry_raw = _optional_row_value(
        row, ("expiry_ms", "expiration_ms", "expiry", "expiration", "expiration_timestamp")
    )
    expiry_ms = (
        _parse_expiry_ms(expiry_raw, "expiry")
        if expiry_raw is not None
        else int(parsed_symbol["expiry_ms"] if parsed_symbol else _missing("expiry"))
    )
    underlying = (
        _optional_row_value(row, ("underlying", "base_currency", "currency"))
        or (parsed_symbol["underlying"] if parsed_symbol else "")
    ).upper()
    _require(bool(underlying), "underlying is required")

    index_price = _positive_float(
        _row_value(row, ("index_price", "index_price_usd", "underlying_price"), "index_price"),
        "index_price",
    )
    dte_raw = _optional_row_value(row, ("dte_days", "dte"))
    dte_days = (
        _finite_float(dte_raw, "dte_days")
        if dte_raw is not None
        else (expiry_ms - sample_timestamp_ms) / 86_400_000.0
    )
    if dte_days < 0.0:
        raise TardisFreeSpreadCalibrationError("dte_days must be non-negative")
    abs_log_raw = _optional_row_value(
        row, ("abs_log_moneyness", "absolute_log_moneyness")
    )
    abs_log_moneyness = (
        _finite_float(abs_log_raw, "abs_log_moneyness")
        if abs_log_raw is not None
        else abs(math.log(strike / index_price))
    )

    rv = _nonnegative_float(
        _row_value(
            row,
            ("trailing_30d_rv_annualized", "trailing_30d_rv", "rv_30d"),
            "trailing_30d_rv_annualized",
        ),
        "trailing_30d_rv_annualized",
    )
    abs_return = _nonnegative_float(
        _row_value(row, ("abs_24h_return", "abs_return_24h"), "abs_24h_return"),
        "abs_24h_return",
    )
    instrument_name = symbol or f"{underlying}-{expiry_ms}-{strike}-{option_type}"
    return TardisOptionLegSample(
        sample_timestamp_ms=sample_timestamp_ms,
        sample_month=sample_month,
        instrument_name=instrument_name,
        underlying=underlying,
        expiry_ms=expiry_ms,
        strike=strike,
        option_type=option_type,
        bid_price=_nonnegative_float(
            _row_value(row, ("bid_price", "bid"), "bid_price"), "bid_price"
        ),
        ask_price=_nonnegative_float(
            _row_value(row, ("ask_price", "ask"), "ask_price"), "ask_price"
        ),
        index_price_usd=index_price,
        contract_multiplier=_positive_float(
            _optional_row_value(row, ("contract_multiplier", "multiplier")) or "1.0",
            "contract_multiplier",
        ),
        quantity=_positive_float(
            _optional_row_value(row, ("quantity", "contracts", "amount")) or "1.0",
            "quantity",
        ),
        trailing_30d_rv_annualized=rv,
        abs_24h_return=abs_return,
        dte_days=dte_days,
        abs_log_moneyness=abs_log_moneyness,
        dte_bucket=dte_days_to_bucket(dte_days),
        moneyness_bucket=abs_log_moneyness_to_bucket(abs_log_moneyness),
        regime_label=regime_label(rv, abs_return),
        structure_sample_id=_optional_row_value(
            row, ("structure_sample_id", "structure_id", "spread_sample_id")
        ),
        leg_role=_optional_row_value(row, ("leg_role", "role", "leg")),
    )


def _missing(label: str) -> NoReturn:
    raise TardisFreeSpreadCalibrationError(f"missing required {label}")


def _coerce_option_type(value: object) -> str:
    if value is None:
        raise TardisFreeSpreadCalibrationError("option_type is required")
    raw = str(value).strip().lower()
    if raw in {"c", "call", "calls"}:
        return "call"
    if raw in {"p", "put", "puts"}:
        return "put"
    raise TardisFreeSpreadCalibrationError(f"option_type must be call/put, got {value!r}")


def _leg_sort_key(leg: TardisOptionLegSample) -> tuple[int, str, int, float, str]:
    return (
        leg.sample_timestamp_ms,
        leg.option_type,
        leg.expiry_ms,
        leg.strike,
        leg.instrument_name,
    )


def _structure_sort_key(sample: StructureSpreadSample) -> tuple[int, str, str, str, str, str]:
    return (
        sample.sample_timestamp_ms,
        sample.option_type,
        sample.dte_bucket,
        sample.moneyness_bucket,
        sample.regime_label,
        sample.sample_id,
    )


def _structure_from_legs(
    sample_id: str, legs: tuple[TardisOptionLegSample, TardisOptionLegSample]
) -> StructureSpreadSample | None:
    left, right = legs
    _require(
        left.sample_timestamp_ms == right.sample_timestamp_ms,
        f"{sample_id} timestamp mismatch",
    )
    _require(left.sample_month == right.sample_month, f"{sample_id} sample_month mismatch")
    _require(left.option_type == right.option_type, f"{sample_id} option_type mismatch")
    _require(left.dte_bucket == right.dte_bucket, f"{sample_id} dte_bucket mismatch")
    _require(left.regime_label == right.regime_label, f"{sample_id} regime_label mismatch")
    if left.dte_bucket == "out_of_grid":
        return None
    structure_moneyness = max(left.abs_log_moneyness, right.abs_log_moneyness)
    moneyness_bucket = abs_log_moneyness_to_bucket(structure_moneyness)
    if moneyness_bucket == "out_of_grid":
        return None
    spread = left.round_trip_leg_crossing_usd + right.round_trip_leg_crossing_usd
    _require(math.isfinite(spread) and spread >= 0.0, f"{sample_id} spread must be finite")
    return StructureSpreadSample(
        sample_id=sample_id,
        sample_timestamp_ms=left.sample_timestamp_ms,
        sample_month=left.sample_month,
        option_type=left.option_type,
        dte_bucket=left.dte_bucket,
        moneyness_bucket=moneyness_bucket,
        regime_label=left.regime_label,
        round_trip_structure_spread_usd=spread,
        leg_instruments=(
            min(left.instrument_name, right.instrument_name),
            max(left.instrument_name, right.instrument_name),
        ),
    )


# ---------------------------------------------------------------------------
# quantile resolution and fallback
# ---------------------------------------------------------------------------
def resolve_spread_quantiles(
    samples: Sequence[StructureSpreadSample],
    *,
    option_type: str,
    dte_bucket: str,
    moneyness_bucket: str,
    regime_label: str,
    fold_id: str | None = None,
    train_end_ms: int | None = None,
) -> SpreadQuantileResolution:
    """Resolve p50/p75 spread quantiles under the frozen fallback order."""

    request = {
        "option_type": _coerce_option_type(option_type),
        "dte_bucket": _validate_bucket(
            dte_bucket, PLAN_BINNING["dte_buckets_days"], "dte_bucket"
        ),
        "moneyness_bucket": _validate_bucket(
            moneyness_bucket,
            PLAN_BINNING["absolute_log_moneyness_buckets"],
            "moneyness_bucket",
        ),
        "regime_label": _validate_bucket(regime_label, PLAN_REGIME_LABELS, "regime_label"),
    }
    cutoff_ms, train_end_iso, resolved_fold_id = _resolve_cutoff(fold_id, train_end_ms)
    causal_samples = tuple(
        sample for sample in samples if cutoff_ms is None or sample.sample_timestamp_ms <= cutoff_ms
    )

    failed: list[str] = []
    for level in PLAN_BINNING["fallback_order"]:
        level = str(level)
        if level == "fail_closed":
            break
        group = tuple(sample for sample in causal_samples if _matches_level(sample, request, level))
        stats = _stats_for_group(group, level)
        if stats is not None:
            return SpreadQuantileResolution(
                requested_option_type=request["option_type"],
                requested_dte_bucket=request["dte_bucket"],
                requested_moneyness_bucket=request["moneyness_bucket"],
                requested_regime_label=request["regime_label"],
                status=STATUS_RESOLVED,
                resolved_level=level,
                p50_round_trip_structure_spread_usd=stats.p50,
                p75_round_trip_structure_spread_usd=stats.p75,
                sample_count=stats.sample_count,
                distinct_month_count=stats.distinct_month_count,
                sample_months=stats.sample_months,
                reason=stats.reason,
                sample_cutoff_ms=cutoff_ms,
                fold_id=resolved_fold_id,
                fold_train_end=train_end_iso,
            )
        failed.append(f"{level}:{_insufficient_reason(group)}")

    return SpreadQuantileResolution(
        requested_option_type=request["option_type"],
        requested_dte_bucket=request["dte_bucket"],
        requested_moneyness_bucket=request["moneyness_bucket"],
        requested_regime_label=request["regime_label"],
        status=STATUS_INCONCLUSIVE,
        resolved_level="fail_closed",
        p50_round_trip_structure_spread_usd=None,
        p75_round_trip_structure_spread_usd=None,
        sample_count=0,
        distinct_month_count=0,
        sample_months=(),
        reason=";".join(failed) or str(PLAN_FAIL_CLOSED_RULES["insufficient_bin_samples"]),
        sample_cutoff_ms=cutoff_ms,
        fold_id=resolved_fold_id,
        fold_train_end=train_end_iso,
    )


def calibration_rows(samples: Sequence[StructureSpreadSample]) -> list[SpreadQuantileResolution]:
    """Resolve all frozen direct bins for every frozen fold."""

    rows: list[SpreadQuantileResolution] = []
    for fold in PLAN_FOLDS:
        fold_id = str(fold["id"])
        for option_type in PLAN_BINNING["option_types"]:
            for dte_bucket in PLAN_BINNING["dte_buckets_days"]:
                for moneyness_bucket in PLAN_BINNING["absolute_log_moneyness_buckets"]:
                    for regime in PLAN_REGIME_LABELS:
                        rows.append(
                            resolve_spread_quantiles(
                                samples,
                                option_type=str(option_type),
                                dte_bucket=str(dte_bucket),
                                moneyness_bucket=str(moneyness_bucket),
                                regime_label=str(regime),
                                fold_id=fold_id,
                            )
                        )
    return rows


def _validate_bucket(value: str, allowed: Mapping[str, Any], label: str) -> str:
    raw = str(value)
    if raw not in allowed:
        raise TardisFreeSpreadCalibrationError(f"{label} {raw!r} is not in the frozen taxonomy")
    return raw


def _resolve_cutoff(
    fold_id: str | None, train_end_ms: int | None
) -> tuple[int | None, str | None, str | None]:
    if fold_id is not None and train_end_ms is not None:
        raise TardisFreeSpreadCalibrationError("pass fold_id or train_end_ms, not both")
    if fold_id is None and train_end_ms is None:
        return None, None, None
    if train_end_ms is not None:
        return int(train_end_ms), _iso_ms(int(train_end_ms)), None
    for fold in PLAN_FOLDS:
        if str(fold["id"]) == str(fold_id):
            cutoff = _parse_timestamp_ms(str(fold["train_end"]), "fold.train_end")
            return cutoff, str(fold["train_end"]), str(fold_id)
    raise TardisFreeSpreadCalibrationError(f"unknown fold_id {fold_id!r}")


def _matches_level(
    sample: StructureSpreadSample, request: Mapping[str, str], level: str
) -> bool:
    for field in _level_fields(level):
        if getattr(sample, field) != request[field]:
            return False
    return True


def _level_fields(level: str) -> tuple[str, ...]:
    if level == "fail_closed":
        return ()
    fields = tuple(part.strip() for part in level.split("+") if part.strip())
    allowed = {"option_type", "dte_bucket", "moneyness_bucket", "regime_label"}
    if any(field not in allowed for field in fields):
        raise TardisFreeSpreadCalibrationError(f"unsupported fallback level {level!r}")
    return fields


def _stats_for_group(samples: Sequence[StructureSpreadSample], level: str) -> _GroupStats | None:
    min_samples = int(PLAN_COST_BUDGET_BAR["min_samples_per_bin"])
    min_months = int(PLAN_COST_BUDGET_BAR["min_distinct_months_per_bin"])
    if len(samples) < min_samples:
        return None
    months = tuple(sorted({sample.sample_month for sample in samples}))
    if len(months) < min_months:
        return None

    values = [sample.round_trip_structure_spread_usd for sample in samples]
    p50 = nearest_rank_quantile(values, 0.50)
    p75 = nearest_rank_quantile(values, 0.75)
    if level != _EXACT_LEVEL:
        child_p50, child_p75 = _valid_exact_child_ceilings(samples)
        if child_p50 is not None:
            p50 = max(p50, child_p50)
        if child_p75 is not None:
            p75 = max(p75, child_p75)
    return _GroupStats(
        p50=p50,
        p75=p75,
        sample_count=len(samples),
        distinct_month_count=len(months),
        sample_months=months,
        reason="resolved_from_real_tardis_bid_ask_samples",
    )


def _valid_exact_child_ceilings(
    samples: Sequence[StructureSpreadSample],
) -> tuple[float | None, float | None]:
    grouped: dict[tuple[str, str, str, str], list[StructureSpreadSample]] = {}
    for sample in samples:
        key = (sample.option_type, sample.dte_bucket, sample.moneyness_bucket, sample.regime_label)
        grouped.setdefault(key, []).append(sample)
    p50s: list[float] = []
    p75s: list[float] = []
    for group in grouped.values():
        stats = _stats_for_group_no_child(group)
        if stats is not None:
            p50s.append(stats.p50)
            p75s.append(stats.p75)
    return (max(p50s) if p50s else None, max(p75s) if p75s else None)


def _stats_for_group_no_child(samples: Sequence[StructureSpreadSample]) -> _GroupStats | None:
    min_samples = int(PLAN_COST_BUDGET_BAR["min_samples_per_bin"])
    min_months = int(PLAN_COST_BUDGET_BAR["min_distinct_months_per_bin"])
    if len(samples) < min_samples:
        return None
    months = tuple(sorted({sample.sample_month for sample in samples}))
    if len(months) < min_months:
        return None
    values = [sample.round_trip_structure_spread_usd for sample in samples]
    return _GroupStats(
        p50=nearest_rank_quantile(values, 0.50),
        p75=nearest_rank_quantile(values, 0.75),
        sample_count=len(samples),
        distinct_month_count=len(months),
        sample_months=months,
        reason="resolved_from_real_tardis_bid_ask_samples",
    )


def _insufficient_reason(samples: Sequence[StructureSpreadSample]) -> str:
    months = {sample.sample_month for sample in samples}
    min_samples = int(PLAN_COST_BUDGET_BAR["min_samples_per_bin"])
    min_months = int(PLAN_COST_BUDGET_BAR["min_distinct_months_per_bin"])
    if len(samples) < min_samples:
        return f"sample_count {len(samples)} < {min_samples}"
    if len(months) < min_months:
        return f"distinct_month_count {len(months)} < {min_months}"
    return "unavailable"


# ---------------------------------------------------------------------------
# deterministic cache writing/loading
# ---------------------------------------------------------------------------
def write_spread_calibration_cache(
    cache_root: str | Path,
    scenario_id: str = DEFAULT_SCENARIO_ID,
    *,
    csv_paths: Sequence[str | Path],
    precalibration_config_sha256: str,
    generator_version: str = GENERATOR_VERSION,
) -> Path:
    """Write deterministic spread-bin calibration cache and manifest."""

    _require(scenario_id == DEFAULT_SCENARIO_ID, f"unexpected scenario_id {scenario_id!r}")
    source_files = _source_file_manifest(csv_paths)
    legs = load_tardis_free_option_leg_samples(csv_paths)
    samples = build_structure_spread_samples(legs)
    rows = calibration_rows(samples)
    bins_text = _spread_bins_text(rows)

    scenario_dir = Path(cache_root) / scenario_id
    scenario_dir.mkdir(parents=True, exist_ok=True)
    (scenario_dir / SPREAD_BINS_FILE).write_text(bins_text, encoding="utf-8")
    manifest = _manifest_from_rows(
        scenario_id,
        rows,
        bins_text,
        source_files=source_files,
        input_leg_count=len(legs),
        structure_sample_count=len(samples),
        precalibration_config_sha256=precalibration_config_sha256,
        generator_version=generator_version,
    )
    (scenario_dir / "manifest.json").write_text(_manifest_text(manifest), encoding="utf-8")
    return scenario_dir


def load_spread_calibration_manifest(cache_root: str | Path, scenario_id: str) -> dict[str, Any]:
    """Load and hash-verify a spread calibration manifest without network access."""

    scenario_dir = Path(cache_root) / scenario_id
    manifest = _read_json_object(scenario_dir / "manifest.json")
    _require(manifest.get("schema_version") == SCHEMA_VERSION, "manifest schema_version mismatch")
    _require(manifest.get("manifest_kind") == MANIFEST_KIND, "manifest_kind mismatch")
    _require(manifest.get("scenario_id") == scenario_id, "manifest scenario_id mismatch")
    _verify_sha(scenario_dir, manifest, SPREAD_BINS_FILE)
    return manifest


def load_spread_calibration_rows(
    cache_root: str | Path, scenario_id: str
) -> tuple[dict[str, str], ...]:
    """Load the manifest-verified spread_bins.csv rows."""

    scenario_dir = Path(cache_root) / scenario_id
    manifest = load_spread_calibration_manifest(cache_root, scenario_id)
    text = _verify_sha(scenario_dir, manifest, SPREAD_BINS_FILE)
    rows = list(csv.DictReader(text.splitlines()))
    _require(rows is not None, "spread_bins.csv has no header")
    return tuple(dict(row) for row in rows)


def _source_file_manifest(csv_paths: Sequence[str | Path]) -> list[dict[str, Any]]:
    _require(bool(csv_paths), "at least one Tardis CSV path is required")
    out: list[dict[str, Any]] = []
    for path_like in sorted(csv_paths, key=lambda p: Path(p).as_posix()):
        path = Path(path_like)
        try:
            text = path.read_text(encoding="utf-8")
        except OSError as exc:
            raise TardisFreeSpreadCalibrationError(f"cannot read Tardis CSV {path}") from exc
        out.append(
            {
                "filename": path.name,
                "sha256": sha256_text(text),
                "file_size": len(text.encode("utf-8")),
                "row_count": max(0, text.count("\n") - 1),
            }
        )
    return out


def _spread_bins_text(rows: Sequence[SpreadQuantileResolution]) -> str:
    buf = io.StringIO()
    writer = csv.writer(buf, lineterminator="\n")
    writer.writerow(_SPREAD_BINS_HEADER)
    for row in rows:
        writer.writerow(row.as_csv_row())
    return buf.getvalue()


def _manifest_from_rows(
    scenario_id: str,
    rows: Sequence[SpreadQuantileResolution],
    bins_text: str,
    *,
    source_files: Sequence[Mapping[str, Any]],
    input_leg_count: int,
    structure_sample_count: int,
    precalibration_config_sha256: str,
    generator_version: str,
) -> dict[str, Any]:
    resolved = sum(1 for row in rows if row.status == STATUS_RESOLVED)
    inconclusive = sum(1 for row in rows if row.status == STATUS_INCONCLUSIVE)
    observed_months = sorted({month for row in rows for month in row.sample_months})
    return {
        "schema_version": SCHEMA_VERSION,
        "manifest_kind": MANIFEST_KIND,
        "scenario_id": scenario_id,
        "exchange": "deribit",
        "generator_version": generator_version,
        "precalibration_config_sha256": str(precalibration_config_sha256),
        "spread_source_quality": "calibrated_spread_sample",
        "free_source_quality": "reconstructed_from_real_trade_iv",
        "non_authorizing_reason": "reconstructed_from_real_trade_iv",
        "authorizing": False,
        "capital_go_allowed": False,
        "cache_fabricated": False,
        "source_files": list(source_files),
        "row_counts": {
            SPREAD_BINS_FILE: len(rows),
            "input_option_chain_rows": input_leg_count,
            "structure_spread_samples": structure_sample_count,
            "resolved_bins": resolved,
            "inconclusive_bins": inconclusive,
        },
        "sample_months_observed": observed_months,
        "tardis_sample_months_frozen": list(TARDIS_SAMPLE_MONTHS),
        "fold_ids": [str(fold["id"]) for fold in PLAN_FOLDS],
        "cost_budget_bar": PLAN_COST_BUDGET_BAR,
        "binning": PLAN_BINNING,
        "regime_labels": PLAN_REGIME_LABELS,
        "unit_conversions": PLAN_UNIT_CONVERSIONS,
        "fold_causal_calibration_rule": PLAN_FOLD_CAUSAL_CALIBRATION_RULE,
        "sha256_by_file": {SPREAD_BINS_FILE: sha256_text(bins_text)},
        "file_sizes": {SPREAD_BINS_FILE: len(bins_text.encode("utf-8"))},
        "no_fabrication_policy": (
            "Only observed local Tardis FREE bid/ask rows are consumed. Missing or non-finite "
            "bid/ask/index/regime inputs raise TardisFreeSpreadCalibrationError; sparse bins "
            "resolve via frozen fallback or remain INCONCLUSIVE."
        ),
    }


def _read_json_object(path: Path) -> dict[str, Any]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except OSError as exc:
        raise TardisFreeSpreadCalibrationError(f"cannot read {path}") from exc
    except json.JSONDecodeError as exc:
        raise TardisFreeSpreadCalibrationError(f"invalid JSON in {path}") from exc
    if not isinstance(data, dict):
        raise TardisFreeSpreadCalibrationError(f"{path} must contain a JSON object")
    return data


def _verify_sha(scenario_dir: Path, manifest: Mapping[str, Any], filename: str) -> str:
    sha_map = manifest.get("sha256_by_file")
    if not isinstance(sha_map, Mapping):
        raise TardisFreeSpreadCalibrationError("manifest sha256_by_file missing")
    expected = sha_map.get(filename)
    if not isinstance(expected, str):
        raise TardisFreeSpreadCalibrationError(f"missing sha256 for {filename}")
    path = scenario_dir / filename
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise TardisFreeSpreadCalibrationError(f"missing cache file {filename}") from exc
    actual = sha256_text(text)
    _require(actual == expected, f"sha256 mismatch for {filename}: {actual} != {expected}")
    return text


__all__ = [
    "GENERATOR_VERSION",
    "MANIFEST_KIND",
    "SCHEMA_VERSION",
    "SPREAD_BINS_FILE",
    "STATUS_INCONCLUSIVE",
    "STATUS_RESOLVED",
    "SpreadQuantileResolution",
    "StructureSpreadSample",
    "TardisFreeSpreadCalibrationError",
    "TardisOptionLegSample",
    "abs_log_moneyness_to_bucket",
    "build_structure_spread_samples",
    "calibration_rows",
    "dte_days_to_bucket",
    "load_spread_calibration_manifest",
    "load_spread_calibration_rows",
    "load_tardis_free_option_leg_samples",
    "load_tardis_free_structure_samples",
    "nearest_rank_quantile",
    "regime_label",
    "resolve_spread_quantiles",
    "sha256_text",
    "write_spread_calibration_cache",
]

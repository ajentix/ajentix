"""Deribit-history trade-vs-mark effective-spread calibration for VRP-free research.

This module is deliberately additive: it converts already parsed free Deribit
history trades into the same ``TardisOptionLegSample`` / ``StructureSpreadSample``
shape consumed by the existing G003 spread-calibration machinery, without live
network access and without treating executed trade-vs-mark spreads as quoted
bid/ask.
"""

from __future__ import annotations

import bisect
import csv
import io
import json
import math
from collections import deque
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from ajentix_quant.data.tardis_free_spread_calibration import (
    _SPREAD_BINS_HEADER,
    SPREAD_BINS_FILE,
    STATUS_INCONCLUSIVE,
    STATUS_RESOLVED,
    SpreadQuantileResolution,
    StructureSpreadSample,
    TardisOptionLegSample,
    _parse_symbol,
    _resolve_cutoff,
    abs_log_moneyness_to_bucket,
    build_structure_spread_samples,
    dte_days_to_bucket,
    resolve_spread_quantiles,
    sha256_text,
)
from ajentix_quant.data.tardis_free_spread_calibration import (
    _manifest_text as _g003_manifest_text,
)
from ajentix_quant.data.tardis_free_spread_calibration import (
    regime_label as classify_regime_label,
)
from ajentix_quant.data.vrp_free_history_cache import (
    IndexPathPoint,
    ParsedDeribitOptionTrade,
)
from ajentix_quant.research.vrp_free_preregistration import (
    DEFAULT_SCENARIO_ID,
    PLAN_BINNING,
    PLAN_COST_BUDGET_BAR,
    PLAN_FOLD_CAUSAL_CALIBRATION_RULE,
    PLAN_FOLDS,
    PLAN_REGIME_LABELS,
    PLAN_SOURCE_QUALITY_BRIDGE,
    PLAN_UNIT_CONVERSIONS,
)

EFFECTIVE_SPREAD_SCHEMA_VERSION = "deribit-history-trade-vs-mark-effective-spread-v1"
EFFECTIVE_SPREAD_METHOD_VERSION = "deribit-history-trade-vs-mark-effective-spread-v1"
EFFECTIVE_SPREAD_SOURCE_BASIS = "deribit_history_trade_vs_mark_effective"
RESOLVED_EFFECTIVE_SPREAD_REASON = "resolved_from_deribit_history_trade_vs_mark_effective_spread"
DEFAULT_MAX_EXTREME_EFFECTIVE_SPREAD_RATE = 0.10
INDEX_PATH_MATCH_REL_TOL = 0.05
EFFECTIVE_SPREAD_MANIFEST_KIND = "effective_spread_calibration"
EFFECTIVE_SPREAD_GENERATOR_VERSION = (
    "ajentix-quant/deribit-history-trade-vs-mark-effective-spread-v1"
)
DEFAULT_EFFECTIVE_SPREAD_CALIBRATION_ROOT = "data/cache/vrp_free_effective_spread_calibration"
SELECTION_BIAS_CAVEAT = (
    "Deribit-history trade-vs-mark effective spread is an executed-trade proxy, not quoted "
    "bid/ask. Executed trades can be selected toward marks and may understate the spread a "
    "strategy would pay when crossing posted markets, especially in size or stress."
)
NO_FABRICATION_POLICY = (
    "Only observed Deribit-history trade price, mark_price, index_price, instrument, and "
    "observed IndexPathPoint lookback data are consumed. Missing, non-finite, unparseable, "
    "or insufficient-lookback inputs raise DeribitHistoryEffectiveSpreadCalibrationError; "
    "sparse bins resolve through the frozen G003 fallback order or remain INCONCLUSIVE."
)

DAY_MS = 86_400_000
RV_LOOKBACK_DAYS = 30
RETURN_LOOKBACK_HOURS = 24
INDEX_LOOKBACK_MAX_GAP_MS = 72 * 60 * 60 * 1_000
MIN_RV_COVERAGE_RATIO = 0.90


class DeribitHistoryEffectiveSpreadCalibrationError(Exception):
    """Raised when trade-vs-mark effective-spread calibration must fail closed."""


@dataclass(frozen=True, kw_only=True)
class IndexRegimeMetrics:
    """Observed index-path regime inputs used by the frozen G003 regime classifier."""

    timestamp_ms: int
    index_price: float
    trailing_30d_rv_annualized: float
    abs_24h_return: float


@dataclass(frozen=True, kw_only=True)
class EffectiveSpreadCacheResult:
    """Deterministic cache-write receipt."""

    scenario_dir: Path
    spread_bins_path: Path
    manifest_path: Path
    manifest_sha256: str
    row_counts: Mapping[str, int]


def _require(condition: bool, message: str) -> None:
    if not condition:
        raise DeribitHistoryEffectiveSpreadCalibrationError(message)


def _finite_float(value: object, label: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float, str)):
        raise DeribitHistoryEffectiveSpreadCalibrationError(f"{label} must be finite")
    try:
        out = float(value)
    except (TypeError, ValueError) as exc:
        raise DeribitHistoryEffectiveSpreadCalibrationError(f"{label} must be finite") from exc
    if not math.isfinite(out):
        raise DeribitHistoryEffectiveSpreadCalibrationError(f"{label} must be finite")
    return out


def _positive_float(value: object, label: str) -> float:
    out = _finite_float(value, label)
    if out <= 0.0:
        raise DeribitHistoryEffectiveSpreadCalibrationError(f"{label} must be positive")
    return out


def _nonnegative_float(value: object, label: str) -> float:
    out = _finite_float(value, label)
    if out < 0.0:
        raise DeribitHistoryEffectiveSpreadCalibrationError(f"{label} must be non-negative")
    return out


def _sample_month(timestamp_ms: int) -> str:
    return datetime.fromtimestamp(int(timestamp_ms) / 1000, tz=UTC).strftime("%Y-%m-01")


def _iso_ms(timestamp_ms: int) -> str:
    return datetime.fromtimestamp(int(timestamp_ms) / 1000, tz=UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _filter_trades(
    trades: Sequence[ParsedDeribitOptionTrade],
    *,
    min_timestamp_ms: int | None,
    max_timestamp_ms: int | None,
) -> tuple[ParsedDeribitOptionTrade, ...]:
    if min_timestamp_ms is not None and max_timestamp_ms is not None:
        _require(
            int(min_timestamp_ms) <= int(max_timestamp_ms),
            "min_timestamp_ms must be <= max_timestamp_ms",
        )
    out = []
    for trade in trades:
        ts = int(trade.timestamp_ms)
        if min_timestamp_ms is not None and ts < int(min_timestamp_ms):
            continue
        if max_timestamp_ms is not None and ts > int(max_timestamp_ms):
            continue
        out.append(trade)
    return tuple(
        sorted(out, key=lambda t: (int(t.timestamp_ms), str(t.instrument_name), int(t.trade_seq)))
    )


def _validate_index_path(index_path: Sequence[IndexPathPoint]) -> tuple[IndexPathPoint, ...]:
    _require(bool(index_path), "index_path must be non-empty")
    ordered = tuple(sorted(index_path, key=lambda point: int(point.timestamp_ms)))
    previous_ts: int | None = None
    for point in ordered:
        ts = int(point.timestamp_ms)
        _positive_float(point.index_price, "index_path.index_price")
        _require(str(point.underlying).upper() == "ETH", "index_path underlying must be ETH")
        if previous_ts is not None:
            _require(ts > previous_ts, "index_path timestamps must be strictly increasing")
        previous_ts = ts
    return ordered


def _index_pair_prefix_sums(
    ordered: Sequence[IndexPathPoint],
) -> tuple[tuple[float, ...], tuple[int, ...]]:
    prefix_sq_log_returns = [0.0]
    gaps: list[int] = []
    for previous, point in zip(ordered, ordered[1:], strict=False):
        gap = int(point.timestamp_ms) - int(previous.timestamp_ms)
        gaps.append(gap)
        previous_price = _positive_float(
            previous.index_price, "index_path.previous.index_price"
        )
        point_price = _positive_float(point.index_price, "index_path.point.index_price")
        prefix_sq_log_returns.append(
            prefix_sq_log_returns[-1] + math.log(point_price / previous_price) ** 2
        )
    return tuple(prefix_sq_log_returns), tuple(gaps)


def _index_metrics_by_timestamp(
    index_path: Sequence[IndexPathPoint],
    required_timestamps: Sequence[int] | None = None,
) -> dict[int, IndexRegimeMetrics]:
    ordered = _validate_index_path(index_path)
    timestamps = [int(point.timestamp_ms) for point in ordered]
    by_ts = {int(point.timestamp_ms): point for point in ordered}
    prefix_sq_log_returns, gaps = _index_pair_prefix_sums(ordered)
    targets = (
        timestamps
        if required_timestamps is None
        else sorted({int(ts) for ts in required_timestamps})
    )
    out: dict[int, IndexRegimeMetrics] = {}
    max_gap_indices: deque[int] = deque()
    invalid_gap_indices: deque[int] = deque()
    next_gap_index = 0
    for ts in targets:
        _require(ts in by_ts, f"index_path missing exact current timestamp {ts}")
        rv_start = int(ts) - RV_LOOKBACK_DAYS * DAY_MS
        left = bisect.bisect_left(timestamps, rv_start)
        right = bisect.bisect_right(timestamps, int(ts))
        pair_stop = max(0, right - 1)
        while next_gap_index < pair_stop:
            gap = gaps[next_gap_index]
            while max_gap_indices and gaps[max_gap_indices[-1]] <= gap:
                max_gap_indices.pop()
            max_gap_indices.append(next_gap_index)
            if gap <= 0 or gap > INDEX_LOOKBACK_MAX_GAP_MS:
                invalid_gap_indices.append(next_gap_index)
            next_gap_index += 1
        while max_gap_indices and max_gap_indices[0] < left:
            max_gap_indices.popleft()
        while invalid_gap_indices and invalid_gap_indices[0] < left:
            invalid_gap_indices.popleft()
        max_gap = gaps[max_gap_indices[0]] if max_gap_indices else None
        first_invalid_gap = gaps[invalid_gap_indices[0]] if invalid_gap_indices else None
        out[ts] = _index_metrics_for_timestamp(
            ts,
            ordered,
            timestamps,
            by_ts,
            left=left,
            right=right,
            prefix_sq_log_returns=prefix_sq_log_returns,
            max_gap=max_gap,
            first_invalid_gap=first_invalid_gap,
        )
    return out


def _index_metrics_for_timestamp(
    timestamp_ms: int,
    ordered: Sequence[IndexPathPoint],
    timestamps: Sequence[int],
    by_ts: Mapping[int, IndexPathPoint],
    *,
    left: int | None = None,
    right: int | None = None,
    prefix_sq_log_returns: Sequence[float] | None = None,
    max_gap: int | None = None,
    first_invalid_gap: int | None = None,
) -> IndexRegimeMetrics:
    current = by_ts.get(int(timestamp_ms))
    if current is None:
        raise DeribitHistoryEffectiveSpreadCalibrationError(
            f"index_path missing current point {timestamp_ms}"
        )
    current_price = _positive_float(current.index_price, "index_path.current.index_price")

    if left is None or right is None:
        rv_start = int(timestamp_ms) - RV_LOOKBACK_DAYS * DAY_MS
        left = bisect.bisect_left(timestamps, rv_start)
        right = bisect.bisect_right(timestamps, int(timestamp_ms))
    _require(
        right - left >= 2,
        f"insufficient {RV_LOOKBACK_DAYS}d index lookback for {timestamp_ms}",
    )
    _require(
        int(ordered[right - 1].timestamp_ms) == int(timestamp_ms),
        f"index_path missing exact current timestamp {timestamp_ms}",
    )
    coverage_ms = int(ordered[right - 1].timestamp_ms) - int(ordered[left].timestamp_ms)
    min_coverage_ms = int(RV_LOOKBACK_DAYS * DAY_MS * MIN_RV_COVERAGE_RATIO)
    _require(
        coverage_ms >= min_coverage_ms,
        f"insufficient {RV_LOOKBACK_DAYS}d index lookback coverage for {timestamp_ms}",
    )

    if prefix_sq_log_returns is None or max_gap is None:
        sum_sq_log_returns = 0.0
        for previous, point in zip(
            ordered[left:right], ordered[left + 1 : right], strict=False
        ):
            gap = int(point.timestamp_ms) - int(previous.timestamp_ms)
            _require(
                0 < gap <= INDEX_LOOKBACK_MAX_GAP_MS,
                f"index_path lookback gap {gap}ms exceeds tolerance",
            )
            previous_price = _positive_float(
                previous.index_price, "index_path.previous.index_price"
            )
            point_price = _positive_float(point.index_price, "index_path.point.index_price")
            sum_sq_log_returns += math.log(point_price / previous_price) ** 2
    else:
        gap_for_message = first_invalid_gap if first_invalid_gap is not None else max_gap
        _require(
            0 < gap_for_message <= INDEX_LOOKBACK_MAX_GAP_MS,
            f"index_path lookback gap {gap_for_message}ms exceeds tolerance",
        )
        sum_sq_log_returns = prefix_sq_log_returns[right - 1] - prefix_sq_log_returns[left]
    elapsed_years = coverage_ms / (365.0 * DAY_MS)
    _require(elapsed_years > 0.0, "index_path lookback elapsed time must be positive")
    trailing_30d_rv_annualized = math.sqrt(sum_sq_log_returns / elapsed_years)

    return_start = int(timestamp_ms) - RETURN_LOOKBACK_HOURS * 60 * 60 * 1_000
    ref_index = bisect.bisect_right(timestamps, return_start) - 1
    _require(ref_index >= 0, f"missing {RETURN_LOOKBACK_HOURS}h index reference")
    ref = ordered[ref_index]
    ref_age = return_start - int(ref.timestamp_ms)
    _require(
        0 <= ref_age <= INDEX_LOOKBACK_MAX_GAP_MS,
        f"{RETURN_LOOKBACK_HOURS}h index reference outside coverage tolerance",
    )
    ref_price = _positive_float(ref.index_price, "index_path.24h_reference.index_price")
    abs_24h_return = abs(current_price / ref_price - 1.0)

    _require(
        math.isfinite(trailing_30d_rv_annualized) and trailing_30d_rv_annualized >= 0.0,
        "trailing_30d_rv_annualized must be finite and non-negative",
    )
    _require(
        math.isfinite(abs_24h_return) and abs_24h_return >= 0.0,
        "abs_24h_return must be finite and non-negative",
    )
    return IndexRegimeMetrics(
        timestamp_ms=int(timestamp_ms),
        index_price=current_price,
        trailing_30d_rv_annualized=trailing_30d_rv_annualized,
        abs_24h_return=abs_24h_return,
    )


def _parse_trade_instrument(trade: ParsedDeribitOptionTrade) -> Mapping[str, Any]:
    parsed = _parse_symbol(str(trade.instrument_name))
    if parsed is None:
        raise DeribitHistoryEffectiveSpreadCalibrationError(
            f"invalid Deribit option instrument_name: {trade.instrument_name!r}"
        )
    return parsed


def effective_spread_leg_sample(
    trade: ParsedDeribitOptionTrade,
    index_metrics: Mapping[int, IndexRegimeMetrics],
    *,
    index_price_tolerance: float = 0.0,
) -> TardisOptionLegSample:
    """Convert one parsed Deribit-history trade into a G003-compatible leg sample.

    The effective full-spread proxy is ``2 * abs(price - mark_price)`` in ETH. It is
    represented as a synthetic bid/ask around mark so the existing G003 leg math
    observes ``ask_price - bid_price`` as that full effective spread.
    """

    instrument = _parse_trade_instrument(trade)
    timestamp_ms = int(trade.timestamp_ms)
    price = _nonnegative_float(trade.price, "price")
    mark_price = _positive_float(trade.mark_price, "mark_price")
    index_price = _positive_float(trade.index_price, "index_price")
    strike = _positive_float(instrument["strike"], "instrument strike")
    expiry_ms = int(instrument["expiry_ms"])
    dte_days = (expiry_ms - timestamp_ms) / DAY_MS
    _require(dte_days >= 0.0, f"{trade.instrument_name} trade timestamp is after expiry")

    metrics = index_metrics.get(timestamp_ms)
    if metrics is None:
        raise DeribitHistoryEffectiveSpreadCalibrationError(
            f"missing index-path regime metrics for {timestamp_ms}"
        )

    half_spread_eth = abs(price - mark_price)
    _require(math.isfinite(half_spread_eth), "effective half-spread must be finite")
    bid_price = mark_price - half_spread_eth
    ask_price = mark_price + half_spread_eth
    _require(bid_price >= 0.0, "synthesized bid_price must be non-negative")
    _require(ask_price >= bid_price, "synthesized ask_price must be >= bid_price")

    index_point_price = _positive_float(metrics.index_price, "index_path current index_price")
    # The index path is built from per-millisecond MEDIAN reconciliation of same-ms
    # readings (within the cache same-ms extreme threshold), so this trade's own
    # index_price can differ from the reconciled path point by sub-millisecond noise.
    # Accept that bounded difference (relative tolerance aligned to the reconciliation
    # policy); a larger mismatch indicates wrong-timestamp data and still fails loud.
    _require(
        math.isclose(
            index_point_price,
            index_price,
            rel_tol=INDEX_PATH_MATCH_REL_TOL,
            abs_tol=index_price_tolerance,
        ),
        f"trade index_price {index_price!r} does not match index_path {index_point_price!r}",
    )

    abs_log_moneyness = abs(math.log(strike / index_price))
    dte_bucket = dte_days_to_bucket(dte_days)
    moneyness_bucket = abs_log_moneyness_to_bucket(abs_log_moneyness)
    regime = classify_regime_label(
        metrics.trailing_30d_rv_annualized,
        metrics.abs_24h_return,
    )
    return TardisOptionLegSample(
        sample_timestamp_ms=timestamp_ms,
        sample_month=_sample_month(timestamp_ms),
        instrument_name=str(trade.instrument_name),
        underlying=str(instrument["underlying"]).upper(),
        expiry_ms=expiry_ms,
        strike=strike,
        option_type=str(instrument["option_type"]),
        bid_price=bid_price,
        ask_price=ask_price,
        index_price_usd=index_price,
        contract_multiplier=1.0,
        quantity=1.0,
        trailing_30d_rv_annualized=metrics.trailing_30d_rv_annualized,
        abs_24h_return=metrics.abs_24h_return,
        dte_days=dte_days,
        abs_log_moneyness=abs_log_moneyness,
        dte_bucket=dte_bucket,
        moneyness_bucket=moneyness_bucket,
        regime_label=regime,
    )


def _synthesized_bid_negative(trade: ParsedDeribitOptionTrade) -> bool:
    """True when ``abs(price - mark_price) > mark_price`` so the synthesized bid
    ``mark - abs(price - mark)`` would be negative. Such extreme/illiquid prints
    (a trade priced far from mark relative to a tiny premium) are unusable for the
    trade-vs-mark effective-spread model; they are excluded + rate-guarded, never
    fabricated. Non-priced rows (mark<=0) are handled by the upstream partition.
    """
    price = trade.price
    mark = trade.mark_price
    if isinstance(price, bool) or isinstance(mark, bool):
        return False
    if not isinstance(price, (int, float)) or not isinstance(mark, (int, float)):
        return False
    price_f = float(price)
    mark_f = float(mark)
    if not (math.isfinite(price_f) and math.isfinite(mark_f)) or mark_f <= 0.0:
        return False
    return abs(price_f - mark_f) > mark_f


def effective_spread_leg_samples(
    trades: Sequence[ParsedDeribitOptionTrade],
    index_path: Sequence[IndexPathPoint],
    *,
    min_timestamp_ms: int | None = None,
    max_timestamp_ms: int | None = None,
) -> tuple[TardisOptionLegSample, ...]:
    """Build G003-compatible leg samples from parsed Deribit-history trades."""

    filtered = _filter_trades(
        trades,
        min_timestamp_ms=min_timestamp_ms,
        max_timestamp_ms=max_timestamp_ms,
    )
    _require(bool(filtered), "at least one Deribit-history trade is required")
    usable = tuple(trade for trade in filtered if not _synthesized_bid_negative(trade))
    excluded = len(filtered) - len(usable)
    extreme_rate = excluded / len(filtered)
    _require(
        extreme_rate <= DEFAULT_MAX_EXTREME_EFFECTIVE_SPREAD_RATE,
        f"extreme effective-spread prints {extreme_rate:.4f} exceed max rate "
        f"{DEFAULT_MAX_EXTREME_EFFECTIVE_SPREAD_RATE}",
    )
    _require(
        bool(usable),
        "no usable effective-spread prints after extreme-print exclusion",
    )
    metrics = _index_metrics_by_timestamp(
        index_path,
        required_timestamps=[trade.timestamp_ms for trade in usable],
    )
    legs = tuple(effective_spread_leg_sample(trade, metrics) for trade in usable)
    return tuple(
        sorted(
            legs,
            key=lambda leg: (
                leg.sample_timestamp_ms,
                leg.expiry_ms,
                leg.option_type,
                leg.strike,
                leg.instrument_name,
            ),
        )
    )


def effective_spread_structure_samples(
    trades: Sequence[ParsedDeribitOptionTrade],
    index_path: Sequence[IndexPathPoint],
    *,
    min_timestamp_ms: int | None = None,
    max_timestamp_ms: int | None = None,
) -> tuple[StructureSpreadSample, ...]:
    """Build G003 ``StructureSpreadSample`` values from effective-spread leg samples."""

    legs = effective_spread_leg_samples(
        trades,
        index_path,
        min_timestamp_ms=min_timestamp_ms,
        max_timestamp_ms=max_timestamp_ms,
    )
    return build_structure_spread_samples(legs)


def effective_spread_structure_samples_for_fold(
    trades: Sequence[ParsedDeribitOptionTrade],
    index_path: Sequence[IndexPathPoint],
    *,
    fold_id: str | None = None,
    train_end_ms: int | None = None,
    min_timestamp_ms: int | None = None,
) -> tuple[StructureSpreadSample, ...]:
    """Build structure samples after applying the frozen fold-causal cutoff."""

    cutoff_ms, _, _ = _resolve_cutoff(fold_id, train_end_ms)
    return effective_spread_structure_samples(
        trades,
        index_path,
        min_timestamp_ms=min_timestamp_ms,
        max_timestamp_ms=cutoff_ms,
    )


def resolve_effective_spread_quantiles(
    trades: Sequence[ParsedDeribitOptionTrade],
    index_path: Sequence[IndexPathPoint],
    *,
    option_type: str,
    dte_bucket: str,
    moneyness_bucket: str,
    regime_label: str,
    fold_id: str | None = None,
    train_end_ms: int | None = None,
    min_timestamp_ms: int | None = None,
) -> SpreadQuantileResolution:
    """Resolve spread quantiles by reusing G003's frozen fallback machinery."""

    samples = effective_spread_structure_samples_for_fold(
        trades,
        index_path,
        fold_id=fold_id,
        train_end_ms=train_end_ms,
        min_timestamp_ms=min_timestamp_ms,
    )
    return resolve_spread_quantiles(
        samples,
        option_type=option_type,
        dte_bucket=dte_bucket,
        moneyness_bucket=moneyness_bucket,
        regime_label=regime_label,
        fold_id=fold_id,
        train_end_ms=train_end_ms,
    )


def effective_spread_calibration_rows(
    trades: Sequence[ParsedDeribitOptionTrade],
    index_path: Sequence[IndexPathPoint],
    *,
    min_timestamp_ms: int | None = None,
) -> list[SpreadQuantileResolution]:
    """Resolve every frozen direct bin for every frozen fold from effective-spread samples."""

    rows: list[SpreadQuantileResolution] = []
    for fold in PLAN_FOLDS:
        fold_id = str(fold["id"])
        cutoff_ms, _, _ = _resolve_cutoff(fold_id, None)
        fold_trades = _filter_trades(
            trades,
            min_timestamp_ms=min_timestamp_ms,
            max_timestamp_ms=cutoff_ms,
        )
        fold_samples = (
            ()
            if not fold_trades
            else effective_spread_structure_samples(
                fold_trades,
                index_path,
            )
        )
        for option_type in PLAN_BINNING["option_types"]:
            for dte_bucket in PLAN_BINNING["dte_buckets_days"]:
                for moneyness_bucket in PLAN_BINNING["absolute_log_moneyness_buckets"]:
                    for regime in PLAN_REGIME_LABELS:
                        rows.append(
                            _relabel_effective_spread_reason(
                                resolve_spread_quantiles(
                                    fold_samples,
                                    option_type=str(option_type),
                                    dte_bucket=str(dte_bucket),
                                    moneyness_bucket=str(moneyness_bucket),
                                    regime_label=str(regime),
                                    fold_id=fold_id,
                                )
                            )
                        )
    return rows


def _relabel_effective_spread_reason(
    resolution: SpreadQuantileResolution,
) -> SpreadQuantileResolution:
    """Replace the inherited G003 Tardis bid/ask source reason on RESOLVED rows.

    These samples are synthetic trade-vs-mark effective spreads, not Tardis quoted
    bid/ask, so a RESOLVED audit row must not claim Tardis/quoted lineage. INCONCLUSIVE
    fail-closed reasons are preserved verbatim (they are diagnostics, not source claims).
    """
    if resolution.status == STATUS_RESOLVED:
        return replace(resolution, reason=RESOLVED_EFFECTIVE_SPREAD_REASON)
    return resolution


def _spread_bins_text(rows: Sequence[SpreadQuantileResolution]) -> str:
    buf = io.StringIO()
    writer = csv.writer(buf, lineterminator="\n")
    writer.writerow(_SPREAD_BINS_HEADER)
    for row in rows:
        writer.writerow(row.as_csv_row())
    return buf.getvalue()


def _row_dict(row: SpreadQuantileResolution) -> dict[str, str]:
    return dict(zip(_SPREAD_BINS_HEADER, row.as_csv_row(), strict=True))


def raw_source_manifest_sha256(manifest: Mapping[str, Any]) -> str:
    """Hash a G002 raw-cache manifest using the deterministic manifest text convention."""

    return sha256_text(_g003_manifest_text(manifest))


def build_effective_spread_manifest(
    scenario_id: str,
    rows: Sequence[SpreadQuantileResolution],
    bins_text: str,
    *,
    input_trade_count: int,
    input_leg_count: int,
    structure_sample_count: int,
    sample_months_observed: Sequence[str],
    raw_source_manifest: Mapping[str, Any],
    precalibration_config_sha256: str,
    generator_version: str = EFFECTIVE_SPREAD_GENERATOR_VERSION,
    min_timestamp_ms: int | None = None,
) -> dict[str, Any]:
    """Build the deterministic, hashed effective-spread cache manifest."""

    resolved = sum(1 for row in rows if row.status == STATUS_RESOLVED)
    inconclusive = sum(1 for row in rows if row.status == STATUS_INCONCLUSIVE)
    sample_months = sorted(set(map(str, sample_months_observed)))
    source_quality = str(PLAN_SOURCE_QUALITY_BRIDGE["spread_source_quality"])
    _require(
        source_quality == "calibrated_spread_sample",
        "frozen spread_source_quality bridge drifted",
    )
    return {
        "schema_version": EFFECTIVE_SPREAD_SCHEMA_VERSION,
        "manifest_kind": EFFECTIVE_SPREAD_MANIFEST_KIND,
        "scenario_id": scenario_id,
        "exchange": "deribit",
        "generator_version": generator_version,
        "method_version": EFFECTIVE_SPREAD_METHOD_VERSION,
        "spread_basis": EFFECTIVE_SPREAD_SOURCE_BASIS,
        "selection_bias_caveat": SELECTION_BIAS_CAVEAT,
        "spread_source_quality": source_quality,
        "effective_spread_source_quality": EFFECTIVE_SPREAD_SOURCE_BASIS,
        "free_source_quality": PLAN_SOURCE_QUALITY_BRIDGE["free_source_quality"],
        "non_authorizing_reason": PLAN_SOURCE_QUALITY_BRIDGE["non_authorizing_reason"],
        "authorizing": False,
        "capital_go_allowed": False,
        "cache_fabricated": False,
        "precalibration_config_sha256": str(precalibration_config_sha256),
        "source_trade_cache_manifest_sha": raw_source_manifest_sha256(raw_source_manifest),
        "raw_source_manifest_sha256": raw_source_manifest_sha256(raw_source_manifest),
        "sample_filter": {
            "min_timestamp_ms": min_timestamp_ms,
            "min_sample_timestamp": None if min_timestamp_ms is None else _iso_ms(min_timestamp_ms),
        },
        "row_counts": {
            SPREAD_BINS_FILE: len(rows),
            "input_deribit_history_trades": input_trade_count,
            "input_effective_spread_leg_samples": input_leg_count,
            "structure_spread_samples": structure_sample_count,
            "resolved_bins": resolved,
            "inconclusive_bins": inconclusive,
        },
        "sample_months_observed": sample_months,
        "fold_ids": [str(fold["id"]) for fold in PLAN_FOLDS],
        "cost_budget_bar": PLAN_COST_BUDGET_BAR,
        "binning": PLAN_BINNING,
        "regime_labels": PLAN_REGIME_LABELS,
        "unit_conversions": PLAN_UNIT_CONVERSIONS,
        "fold_causal_calibration_rule": PLAN_FOLD_CAUSAL_CALIBRATION_RULE,
        "index_regime_inputs": {
            "trailing_30d_rv_annualized": {
                "lookback_days": RV_LOOKBACK_DAYS,
                "annualization_days": 365,
                "max_observed_gap_ms": INDEX_LOOKBACK_MAX_GAP_MS,
                "min_coverage_ratio": MIN_RV_COVERAGE_RATIO,
                "formula": "sqrt(sum(log_return^2) / elapsed_years)",
            },
            "abs_24h_return": {
                "lookback_hours": RETURN_LOOKBACK_HOURS,
                "max_reference_age_ms": INDEX_LOOKBACK_MAX_GAP_MS,
                "formula": "abs(current_index_price / observed_reference_price - 1)",
            },
        },
        "effective_spread_formula": {
            "half_spread_eth": "abs(price - mark_price)",
            "full_spread_eth": "2 * abs(price - mark_price)",
            "synthetic_bid_price": "mark_price - abs(price - mark_price)",
            "synthetic_ask_price": "mark_price + abs(price - mark_price)",
        },
        "per_bin_resolutions": [_row_dict(row) for row in rows],
        "sha256_by_file": {SPREAD_BINS_FILE: sha256_text(bins_text)},
        "file_sizes": {SPREAD_BINS_FILE: len(bins_text.encode("utf-8"))},
        "no_fabrication_policy": NO_FABRICATION_POLICY,
    }


def write_effective_spread_calibration_cache(
    cache_root: str | Path,
    scenario_id: str = DEFAULT_SCENARIO_ID,
    *,
    trades: Sequence[ParsedDeribitOptionTrade],
    index_path: Sequence[IndexPathPoint],
    raw_source_manifest: Mapping[str, Any],
    precalibration_config_sha256: str,
    generator_version: str = EFFECTIVE_SPREAD_GENERATOR_VERSION,
    min_timestamp_ms: int | None = None,
) -> Path:
    """Write a deterministic effective-spread calibration cache and manifest."""

    _require(scenario_id == DEFAULT_SCENARIO_ID, f"unexpected scenario_id {scenario_id!r}")
    legs = effective_spread_leg_samples(
        trades,
        index_path,
        min_timestamp_ms=min_timestamp_ms,
    )
    samples = build_structure_spread_samples(legs)
    rows = effective_spread_calibration_rows(
        trades,
        index_path,
        min_timestamp_ms=min_timestamp_ms,
    )
    bins_text = _spread_bins_text(rows)
    scenario_dir = Path(cache_root) / scenario_id
    scenario_dir.mkdir(parents=True, exist_ok=True)
    (scenario_dir / SPREAD_BINS_FILE).write_text(bins_text, encoding="utf-8")
    manifest = build_effective_spread_manifest(
        scenario_id,
        rows,
        bins_text,
        input_trade_count=len(
            _filter_trades(trades, min_timestamp_ms=min_timestamp_ms, max_timestamp_ms=None)
        ),
        input_leg_count=len(legs),
        structure_sample_count=len(samples),
        sample_months_observed=tuple(sorted({sample.sample_month for sample in samples})),
        raw_source_manifest=raw_source_manifest,
        precalibration_config_sha256=precalibration_config_sha256,
        generator_version=generator_version,
        min_timestamp_ms=min_timestamp_ms,
    )
    manifest_text = _g003_manifest_text(manifest)
    (scenario_dir / "manifest.json").write_text(manifest_text, encoding="utf-8")
    return scenario_dir


def load_effective_spread_calibration_manifest(
    cache_root: str | Path, scenario_id: str = DEFAULT_SCENARIO_ID
) -> dict[str, Any]:
    """Load and SHA-verify an effective-spread calibration manifest."""

    scenario_dir = Path(cache_root) / scenario_id
    try:
        manifest = json.loads((scenario_dir / "manifest.json").read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise DeribitHistoryEffectiveSpreadCalibrationError(
            f"cannot read effective-spread manifest for {scenario_id!r}"
        ) from exc
    _require(
        manifest.get("schema_version") == EFFECTIVE_SPREAD_SCHEMA_VERSION,
        "manifest schema_version mismatch",
    )
    _require(
        manifest.get("manifest_kind") == EFFECTIVE_SPREAD_MANIFEST_KIND,
        "manifest_kind mismatch",
    )
    _require(manifest.get("scenario_id") == scenario_id, "manifest scenario_id mismatch")
    _verify_cache_file_sha(scenario_dir, manifest, SPREAD_BINS_FILE)
    return manifest


def load_effective_spread_calibration_rows(
    cache_root: str | Path, scenario_id: str = DEFAULT_SCENARIO_ID
) -> tuple[dict[str, str], ...]:
    """Load manifest-verified effective-spread calibration rows."""

    scenario_dir = Path(cache_root) / scenario_id
    manifest = load_effective_spread_calibration_manifest(cache_root, scenario_id)
    text = _verify_cache_file_sha(scenario_dir, manifest, SPREAD_BINS_FILE)
    rows = list(csv.DictReader(text.splitlines()))
    return tuple(dict(row) for row in rows)


def write_effective_spread_calibration_cache_result(
    cache_root: str | Path,
    scenario_id: str = DEFAULT_SCENARIO_ID,
    **kwargs: Any,
) -> EffectiveSpreadCacheResult:
    """Write the cache and return a small deterministic receipt."""

    scenario_dir = write_effective_spread_calibration_cache(cache_root, scenario_id, **kwargs)
    manifest_path = scenario_dir / "manifest.json"
    manifest_text = manifest_path.read_text(encoding="utf-8")
    manifest = load_effective_spread_calibration_manifest(cache_root, scenario_id)
    return EffectiveSpreadCacheResult(
        scenario_dir=scenario_dir,
        spread_bins_path=scenario_dir / SPREAD_BINS_FILE,
        manifest_path=manifest_path,
        manifest_sha256=sha256_text(manifest_text),
        row_counts=manifest["row_counts"],
    )


def _verify_cache_file_sha(scenario_dir: Path, manifest: Mapping[str, Any], filename: str) -> str:
    sha_map = manifest.get("sha256_by_file")
    if not isinstance(sha_map, Mapping) or filename not in sha_map:
        raise DeribitHistoryEffectiveSpreadCalibrationError(f"sha256 missing for {filename}")
    path = scenario_dir / filename
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise DeribitHistoryEffectiveSpreadCalibrationError(f"cannot read {path}") from exc
    observed = sha256_text(text)
    expected = str(sha_map[filename])
    if observed != expected:
        raise DeribitHistoryEffectiveSpreadCalibrationError(
            f"sha mismatch for {filename}: {observed} != {expected}"
        )
    return text


__all__ = [
    "DEFAULT_EFFECTIVE_SPREAD_CALIBRATION_ROOT",
    "EFFECTIVE_SPREAD_GENERATOR_VERSION",
    "EFFECTIVE_SPREAD_MANIFEST_KIND",
    "EFFECTIVE_SPREAD_METHOD_VERSION",
    "EFFECTIVE_SPREAD_SCHEMA_VERSION",
    "EFFECTIVE_SPREAD_SOURCE_BASIS",
    "INDEX_LOOKBACK_MAX_GAP_MS",
    "MIN_RV_COVERAGE_RATIO",
    "NO_FABRICATION_POLICY",
    "RETURN_LOOKBACK_HOURS",
    "RV_LOOKBACK_DAYS",
    "SELECTION_BIAS_CAVEAT",
    "DeribitHistoryEffectiveSpreadCalibrationError",
    "EffectiveSpreadCacheResult",
    "IndexRegimeMetrics",
    "build_effective_spread_manifest",
    "effective_spread_calibration_rows",
    "effective_spread_leg_sample",
    "effective_spread_leg_samples",
    "effective_spread_structure_samples",
    "effective_spread_structure_samples_for_fold",
    "load_effective_spread_calibration_manifest",
    "load_effective_spread_calibration_rows",
    "raw_source_manifest_sha256",
    "resolve_effective_spread_quantiles",
    "write_effective_spread_calibration_cache",
    "write_effective_spread_calibration_cache_result",
]

"""Causal VRP-free IV-surface reconstruction from observed Deribit-history trades.

This module turns raw free Deribit-history option trades into non-authorizing synthetic
``OptionChainSnapshot`` values. Every reconstructed leg is priced from an observed traded
IV and an observed ETH index point at or before the snapshot timestamp; missing or stale
coverage raises ``IVSurfaceCoverageError`` instead of filling rows. The compatibility
``OptionLeg.source_quality`` slot is always ``SourceQuality.FIXTURE``. Free-data lineage is
kept outside ``OptionLeg`` on the dataclasses below.
"""

from __future__ import annotations

import json
import math
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any

from ajentix_quant.adapters.base import SourceQuality
from ajentix_quant.data.options_cache import sha256_text, write_normalized_cache
from ajentix_quant.data.vrp_free_history_cache import (
    IndexPathPoint,
    ParsedDeribitOptionTrade,
    VrpFreeHistoryDataset,
)
from ajentix_quant.options.types import OptionChainSnapshot, OptionLeg, OptionType, Side
from ajentix_quant.options.valuation import black_scholes_value_greeks, year_fraction_act_365
from ajentix_quant.research.vrp_free_preregistration import (
    DEFAULT_SCENARIO_ID,
    PLAN_RECONSTRUCTION_CONFIG,
    PLAN_SETTLEMENT,
    PLAN_SOURCE_QUALITY_BRIDGE,
    PLAN_STRESS_RULE,
)

SCHEMA_VERSION = "aq-vrp-free-iv-surface-reconstruction-v1"
TRANSFORM_VERSION = (
    "ajentix-quant/g003-" + str(PLAN_RECONSTRUCTION_CONFIG["method_version"])
)
LINEAGE_FILE = "reconstruction_lineage.jsonl"
INCONCLUSIVE_STATUS = "INCONCLUSIVE"

_MS_PER_HOUR = 60 * 60 * 1_000


class IVSurfaceReconstructionError(Exception):
    """Base error for fail-closed reconstructed-chain validation failures."""


class IVSurfaceCoverageError(IVSurfaceReconstructionError):
    """Raised when required real trade/index coverage is missing or stale."""

    status = INCONCLUSIVE_STATUS

    def __init__(self, reason_code: str, message: str) -> None:
        self.reason_code = reason_code
        super().__init__(f"{INCONCLUSIVE_STATUS}:{reason_code}: {message}")


@dataclass(frozen=True, kw_only=True)
class ReconstructedLegLineage:
    """Observed trade and model diagnostics for one reconstructed compatibility leg."""

    instrument_name: str
    source_trade_id: str
    source_trade_seq: int
    source_trade_timestamp_ms: int
    source_trade_iv: float
    reconstructed_iv_fraction: float
    source_trade_index_price: float
    source_trade_amount: float
    quote_age_s: float
    model_price_eth: float
    model_value_usd: float
    delta: float

    def as_dict(self) -> dict[str, Any]:
        return {
            "instrument_name": self.instrument_name,
            "source_trade_id": self.source_trade_id,
            "source_trade_seq": self.source_trade_seq,
            "source_trade_timestamp_ms": self.source_trade_timestamp_ms,
            "source_trade_iv": self.source_trade_iv,
            "reconstructed_iv_fraction": self.reconstructed_iv_fraction,
            "source_trade_index_price": self.source_trade_index_price,
            "source_trade_amount": self.source_trade_amount,
            "quote_age_s": self.quote_age_s,
            "model_price_eth": self.model_price_eth,
            "model_value_usd": self.model_value_usd,
            "delta": self.delta,
        }


@dataclass(frozen=True, kw_only=True)
class ReconstructedOptionLineage:
    """Free-data lineage carried beside, not inside, reconstructed ``OptionLeg`` values."""

    schema_version: str
    reconstruction_method_version: str
    snapshot_ts_ms: int
    expiry_ms: int
    source_index_timestamp_ms: int
    source_index_price: float
    max_trade_staleness_hours: int
    no_future_trades: bool
    no_extrapolation: bool
    legacy_option_leg_source_quality: SourceQuality
    free_source_quality: str
    spread_source_quality: str
    authorizing: bool
    capital_go_allowed: bool
    non_authorizing_reason: str
    legs: tuple[ReconstructedLegLineage, ...]

    def as_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "reconstruction_method_version": self.reconstruction_method_version,
            "snapshot_ts_ms": self.snapshot_ts_ms,
            "expiry_ms": self.expiry_ms,
            "source_index_timestamp_ms": self.source_index_timestamp_ms,
            "source_index_price": self.source_index_price,
            "max_trade_staleness_hours": self.max_trade_staleness_hours,
            "no_future_trades": self.no_future_trades,
            "no_extrapolation": self.no_extrapolation,
            "legacy_option_leg_source_quality": self.legacy_option_leg_source_quality.value,
            "free_source_quality": self.free_source_quality,
            "spread_source_quality": self.spread_source_quality,
            "authorizing": self.authorizing,
            "capital_go_allowed": self.capital_go_allowed,
            "non_authorizing_reason": self.non_authorizing_reason,
            "legs": [leg.as_dict() for leg in self.legs],
        }


@dataclass(frozen=True, kw_only=True)
class ReconstructedOptionChain:
    """One reconstructed one-expiry chain plus its non-authorizing free lineage."""

    snapshot: OptionChainSnapshot
    lineage: ReconstructedOptionLineage

    def as_dict(self) -> dict[str, Any]:
        return {
            "snapshot": _snapshot_payload(self.snapshot),
            "lineage": self.lineage.as_dict(),
        }


def required_reconstruction_timestamps(
    trades: Sequence[ParsedDeribitOptionTrade],
    *,
    start_ts_ms: int,
    end_ts_ms: int,
    stress_timestamps_ms: Sequence[int] = (),
) -> tuple[int, ...]:
    """Return the frozen 8h grid plus configured stress/expiry-settlement timestamps."""

    _require_int(start_ts_ms, "start_ts_ms", positive=True)
    _require_int(end_ts_ms, "end_ts_ms", positive=True)
    if start_ts_ms > end_ts_ms:
        raise IVSurfaceCoverageError("invalid_date_range", "start_ts_ms must be <= end_ts_ms")

    utc_hours = tuple(int(value) for value in PLAN_RECONSTRUCTION_CONFIG["utc_hours"])
    timestamps: set[int] = set()
    current = datetime.fromtimestamp(start_ts_ms / 1000, tz=UTC).date()
    end_day = datetime.fromtimestamp(end_ts_ms / 1000, tz=UTC).date()
    while current <= end_day:
        day_start = datetime.combine(current, datetime.min.time(), tzinfo=UTC)
        for hour in utc_hours:
            ts_ms = int((day_start + timedelta(hours=hour)).timestamp() * 1000)
            if start_ts_ms <= ts_ms <= end_ts_ms:
                timestamps.add(ts_ms)
        current += timedelta(days=1)

    if PLAN_RECONSTRUCTION_CONFIG["include_expiry_settlement_timestamps"]:
        timestamps.update(
            trade.expiry_ms for trade in trades if start_ts_ms <= trade.expiry_ms <= end_ts_ms
        )
    if PLAN_RECONSTRUCTION_CONFIG["include_required_stress_timestamps"]:
        for raw_ts_ms in stress_timestamps_ms:
            ts_ms = _require_int(raw_ts_ms, "stress_timestamp_ms", positive=True)
            if start_ts_ms <= ts_ms <= end_ts_ms:
                timestamps.add(ts_ms)
    return tuple(sorted(timestamps))


def reconstruct_iv_surface_at(
    trades: Sequence[ParsedDeribitOptionTrade],
    *,
    snapshot_ts_ms: int,
    index_path: Sequence[IndexPathPoint] | None = None,
    scenario_id: str = DEFAULT_SCENARIO_ID,
    required_instrument_names: Sequence[str] = (),
) -> tuple[ReconstructedOptionChain, ...]:
    """Reconstruct all one-expiry chains available at one timestamp, causally.

    Only trades with ``timestamp_ms <= snapshot_ts_ms`` and age within the frozen
    staleness window are eligible. The builder does not interpolate or extrapolate;
    every output instrument is backed by one observed trade row.
    """

    snapshot_ts_ms = _require_int(snapshot_ts_ms, "snapshot_ts_ms", positive=True)
    if scenario_id != DEFAULT_SCENARIO_ID:
        raise IVSurfaceCoverageError(
            "unexpected_scenario_id",
            f"scenario_id {scenario_id!r} does not match {DEFAULT_SCENARIO_ID!r}",
        )
    _validate_reconstruction_config()
    max_staleness_ms = _max_staleness_ms()
    points = tuple(index_path) if index_path is not None else _index_path_from_trades(trades)
    index_point = _latest_index_point(
        points,
        snapshot_ts_ms=snapshot_ts_ms,
        max_staleness_ms=max_staleness_ms,
    )
    latest_by_instrument = _latest_eligible_trade_by_instrument(
        trades,
        snapshot_ts_ms=snapshot_ts_ms,
        max_staleness_ms=max_staleness_ms,
    )
    required = tuple(dict.fromkeys(str(name) for name in required_instrument_names))
    missing = sorted(name for name in required if name not in latest_by_instrument)
    if missing:
        raise IVSurfaceCoverageError(
            "missing_required_instrument_coverage",
            f"missing required reconstructed instruments at {snapshot_ts_ms}: {missing}",
        )
    if not latest_by_instrument:
        raise IVSurfaceCoverageError(
            "missing_trade_coverage",
            f"no non-stale observed option trades are available at {snapshot_ts_ms}",
        )

    by_expiry: dict[int, list[ParsedDeribitOptionTrade]] = {}
    for trade in latest_by_instrument.values():
        by_expiry.setdefault(trade.expiry_ms, []).append(trade)
    chains = tuple(
        _chain_from_expiry_trades(
            tuple(sorted(expiry_trades, key=_instrument_sort_key)),
            snapshot_ts_ms=snapshot_ts_ms,
            index_point=index_point,
            scenario_id=scenario_id,
        )
        for _expiry_ms, expiry_trades in sorted(by_expiry.items())
    )
    if not chains:
        raise IVSurfaceCoverageError(
            "missing_expiry_coverage",
            f"no non-expired option expiries are available at {snapshot_ts_ms}",
        )
    return chains


def reconstruct_iv_surface(
    trades: Sequence[ParsedDeribitOptionTrade],
    *,
    snapshot_timestamps_ms: Sequence[int],
    index_path: Sequence[IndexPathPoint] | None = None,
    scenario_id: str = DEFAULT_SCENARIO_ID,
) -> tuple[ReconstructedOptionChain, ...]:
    """Reconstruct deterministic chains for the supplied snapshot timestamps."""

    chains: list[ReconstructedOptionChain] = []
    for ts_ms in _normalize_timestamps(snapshot_timestamps_ms, "snapshot_timestamps_ms"):
        chains.extend(
            reconstruct_iv_surface_at(
                trades,
                snapshot_ts_ms=ts_ms,
                index_path=index_path,
                scenario_id=scenario_id,
            )
        )
    return tuple(sorted(chains, key=_chain_sort_key))


def reconstruct_from_history_dataset(
    dataset: VrpFreeHistoryDataset,
    *,
    snapshot_timestamps_ms: Sequence[int] | None = None,
    stress_timestamps_ms: Sequence[int] = (),
    scenario_id: str = DEFAULT_SCENARIO_ID,
) -> tuple[ReconstructedOptionChain, ...]:
    """Reconstruct the frozen grid from a loaded network-free raw history cache."""

    timestamps = tuple(snapshot_timestamps_ms or ())
    if snapshot_timestamps_ms is None:
        date_range = _require_mapping(dataset.manifest.get("date_range"), "date_range missing")
        manifest_stress = _stress_timestamps_from_manifest(dataset.manifest)
        start_ts_ms = _require_int(date_range.get("start_ts_ms"), "date_range.start_ts_ms")
        # The reconstruction grid is required only over the coverage window; earlier
        # trades are warmup that supply trailing IV/index for the opening grid points.
        coverage_start_raw = date_range.get("coverage_start_ts_ms")
        grid_start_ts_ms = (
            _require_int(coverage_start_raw, "date_range.coverage_start_ts_ms")
            if coverage_start_raw is not None
            else start_ts_ms
        )
        timestamps = required_reconstruction_timestamps(
            dataset.trades,
            start_ts_ms=grid_start_ts_ms,
            end_ts_ms=_require_int(date_range.get("end_ts_ms"), "date_range.end_ts_ms"),
            stress_timestamps_ms=tuple(stress_timestamps_ms) + manifest_stress,
        )
    return reconstruct_iv_surface(
        dataset.trades,
        snapshot_timestamps_ms=timestamps,
        index_path=dataset.index_path,
        scenario_id=scenario_id,
    )


def reconstructed_chains_sha256(chains: Sequence[ReconstructedOptionChain]) -> str:
    """Return a canonical digest of reconstructed snapshots and lineage."""

    payload = [chain.as_dict() for chain in sorted(chains, key=_chain_sort_key)]
    return sha256_text(_canonical_json(payload))


def write_reconstructed_chain_cache(
    cache_root: str | Path,
    scenario_id: str,
    *,
    reconstructed_chains: Sequence[ReconstructedOptionChain],
    raw_manifest_sha256: str | None = None,
) -> Path:
    """Write a deterministic reconstructed-chain cache and manifest.

    ``option_chains.csv`` uses the existing normalized cache layout for downstream reuse.
    ``reconstruction_lineage.jsonl`` carries the free-data lineage that the committed
    ``OptionLeg`` type deliberately does not know about.
    """

    chains = tuple(sorted(reconstructed_chains, key=_chain_sort_key))
    if not chains:
        raise IVSurfaceCoverageError("missing_reconstructed_chains", "no chains to write")
    for chain in chains:
        if chain.snapshot.scenario_id != scenario_id:
            raise IVSurfaceCoverageError("scenario_mismatch", "snapshot scenario_id mismatch")
        _assert_fixture_legacy_quality(chain)

    scenario_dir = write_normalized_cache(
        cache_root,
        scenario_id,
        snapshots=tuple(chain.snapshot for chain in chains),
        source_ids=[TRANSFORM_VERSION],
        transform_version=TRANSFORM_VERSION,
        raw_manifest_sha256=raw_manifest_sha256,
    )
    lineage_text = _lineage_text(chains)
    lineage_path = scenario_dir / LINEAGE_FILE
    lineage_path.write_text(lineage_text, encoding="utf-8")

    manifest_path = scenario_dir / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    if not isinstance(manifest, dict):
        raise IVSurfaceReconstructionError("normalized manifest must be a JSON object")
    sha_by_file = dict(_require_mapping(manifest.get("sha256_by_file"), "sha256_by_file"))
    sha_by_file[LINEAGE_FILE] = sha256_text(lineage_text)
    manifest["sha256_by_file"] = dict(sorted(sha_by_file.items()))
    file_sizes = dict(_require_mapping(manifest.get("file_sizes"), "file_sizes"))
    file_sizes[LINEAGE_FILE] = len(lineage_text.encode("utf-8"))
    manifest["file_sizes"] = dict(sorted(file_sizes.items()))
    row_counts = dict(_require_mapping(manifest.get("row_counts"), "row_counts"))
    row_counts[LINEAGE_FILE] = len(chains)
    row_counts["lineage_rows"] = len(chains)
    manifest["row_counts"] = dict(sorted(row_counts.items()))

    source_quality = dict(_require_mapping(manifest.get("source_quality"), "source_quality"))
    source_quality["free_reconstruction_lineage"] = SourceQuality.FIXTURE.value
    source_quality["reconstructed_option_chain"] = SourceQuality.FIXTURE.value
    manifest["source_quality"] = dict(sorted(source_quality.items()))
    manifest["non_authorizing_source_quality_keys"] = sorted(
        key
        for key, value in source_quality.items()
        if SourceQuality(str(value)) in {SourceQuality.FIXTURE, SourceQuality.PROXY}
    )
    manifest["reconstruction_schema_version"] = SCHEMA_VERSION
    manifest["reconstruction_method_version"] = str(
        PLAN_RECONSTRUCTION_CONFIG["method_version"]
    )
    manifest["reconstruction_config"] = dict(PLAN_RECONSTRUCTION_CONFIG)
    manifest["settlement"] = dict(PLAN_SETTLEMENT)
    manifest["stress_rule"] = dict(PLAN_STRESS_RULE)
    manifest["free_lineage"] = _lineage_manifest(chains)
    manifest["reconstructed_chain_sha256"] = reconstructed_chains_sha256(chains)
    manifest["synthetic_model_prices"] = True
    manifest["fabricated_quotes_or_spreads"] = False
    manifest["cache_fabricated"] = False
    manifest["no_fabrication_policy"] = (
        "Rows are emitted only for observed Deribit-history trade IV within the frozen "
        "staleness window. Bid and ask compatibility fields are equal to the "
        "Black-Scholes model value, so no synthetic spread is fabricated."
    )
    manifest_path.write_text(_manifest_text(manifest), encoding="utf-8")
    return scenario_dir


def _chain_from_expiry_trades(
    trades: Sequence[ParsedDeribitOptionTrade],
    *,
    snapshot_ts_ms: int,
    index_point: IndexPathPoint,
    scenario_id: str,
) -> ReconstructedOptionChain:
    if not trades:
        raise IVSurfaceCoverageError("empty_expiry_trades", "expiry trade group is empty")
    expiry_ms = trades[0].expiry_ms
    if expiry_ms <= snapshot_ts_ms:
        raise IVSurfaceCoverageError("expired_contract", "cannot reconstruct expired contracts")
    underlyings = {trade.underlying for trade in trades}
    if len(underlyings) != 1:
        raise IVSurfaceCoverageError(
            "mixed_underlyings",
            f"mixed underlyings: {sorted(underlyings)}",
        )
    if index_point.underlying != next(iter(underlyings)):
        raise IVSurfaceCoverageError("index_underlying_mismatch", "index path underlying mismatch")

    legs: list[OptionLeg] = []
    leg_lineage: list[ReconstructedLegLineage] = []
    for trade in trades:
        leg, leg_line = _leg_from_trade(
            trade,
            snapshot_ts_ms=snapshot_ts_ms,
            index_price=index_point.index_price,
        )
        legs.append(leg)
        leg_lineage.append(leg_line)

    source_ts_ms = max([index_point.timestamp_ms, *(leg.quote_ts_ms for leg in legs)])
    usd_conversion_inputs = {
        "ETH_USD": index_point.index_price,
        "source": PLAN_SETTLEMENT["usd_conversion_source"],
        "source_timestamp_ms": index_point.timestamp_ms,
    }
    snapshot_hash = _pre_snapshot_hash(
        underlying=next(iter(underlyings)),
        snapshot_ts_ms=snapshot_ts_ms,
        source_ts_ms=source_ts_ms,
        scenario_id=scenario_id,
        expiry_ms=expiry_ms,
        index_price=index_point.index_price,
        usd_conversion_inputs=usd_conversion_inputs,
        legs=legs,
    )
    snapshot = OptionChainSnapshot(
        underlying=next(iter(underlyings)),
        exchange="deribit",
        snapshot_ts_ms=snapshot_ts_ms,
        source_ts_ms=source_ts_ms,
        source_id=TRANSFORM_VERSION,
        scenario_id=scenario_id,
        settlement_index_price=index_point.index_price,
        index_price=index_point.index_price,
        usd_conversion_inputs=usd_conversion_inputs,
        legs=tuple(sorted(legs, key=lambda leg: leg.instrument_name)),
        source_quality_map={
            "free_reconstruction_lineage": SourceQuality.FIXTURE,
            "instrument_metadata": SourceQuality.FIXTURE,
            "option_chain": SourceQuality.FIXTURE,
            "reconstructed_option_chain": SourceQuality.FIXTURE,
            "settlement_index": SourceQuality.FIXTURE,
        },
        schema_version=SCHEMA_VERSION,
        manifest_sha256=snapshot_hash,
    )
    lineage = ReconstructedOptionLineage(
        schema_version=SCHEMA_VERSION,
        reconstruction_method_version=str(PLAN_RECONSTRUCTION_CONFIG["method_version"]),
        snapshot_ts_ms=snapshot_ts_ms,
        expiry_ms=expiry_ms,
        source_index_timestamp_ms=index_point.timestamp_ms,
        source_index_price=index_point.index_price,
        max_trade_staleness_hours=int(PLAN_RECONSTRUCTION_CONFIG["max_trade_staleness_hours"]),
        no_future_trades=bool(PLAN_RECONSTRUCTION_CONFIG["no_future_trades"]),
        no_extrapolation=True,
        legacy_option_leg_source_quality=_legacy_reconstructed_source_quality(),
        free_source_quality=str(PLAN_SOURCE_QUALITY_BRIDGE["free_source_quality"]),
        spread_source_quality=str(PLAN_SOURCE_QUALITY_BRIDGE["spread_source_quality"]),
        authorizing=False,
        capital_go_allowed=False,
        non_authorizing_reason=str(PLAN_SOURCE_QUALITY_BRIDGE["non_authorizing_reason"]),
        legs=tuple(sorted(leg_lineage, key=lambda item: item.instrument_name)),
    )
    return ReconstructedOptionChain(snapshot=snapshot, lineage=lineage)


def _leg_from_trade(
    trade: ParsedDeribitOptionTrade,
    *,
    snapshot_ts_ms: int,
    index_price: float,
) -> tuple[OptionLeg, ReconstructedLegLineage]:
    iv_fraction = _trade_iv_fraction(trade.iv)
    time_to_expiry = year_fraction_act_365(
        snapshot_ts_ms=snapshot_ts_ms,
        expiry_ms=trade.expiry_ms,
    )
    option_type = OptionType(trade.option_type)
    greeks = black_scholes_value_greeks(
        option_type=option_type,
        spot=index_price,
        strike=trade.strike,
        time_to_expiry_years=time_to_expiry,
        volatility=iv_fraction,
    )
    model_price_eth = _require_non_negative(
        greeks.value / index_price,
        "model_price_eth",
    )
    quote_age_s = _require_non_negative(
        (snapshot_ts_ms - trade.timestamp_ms) / 1000.0,
        "quote_age_s",
    )
    leg = OptionLeg(
        instrument_name=trade.instrument_name,
        underlying=trade.underlying,
        contract_multiplier=float(PLAN_SETTLEMENT["contract_multiplier"]),
        option_type=option_type,
        side=Side.LONG,
        strike=trade.strike,
        expiry_ms=trade.expiry_ms,
        settlement_style=str(PLAN_SETTLEMENT["style"]),
        settlement_index=str(PLAN_SETTLEMENT["settlement_index"]),
        premium_currency=str(PLAN_SETTLEMENT["premium_currency"]),
        fee_currency=str(PLAN_SETTLEMENT["fee_currency"]),
        collateral_currency=str(PLAN_SETTLEMENT["collateral_currency"]),
        usd_conversion_source=str(PLAN_SETTLEMENT["usd_conversion_source"]),
        quote_ts_ms=trade.timestamp_ms,
        quote_age_s=quote_age_s,
        bid_price=model_price_eth,
        bid_amount=trade.amount,
        bid_iv=iv_fraction,
        ask_price=model_price_eth,
        ask_amount=trade.amount,
        ask_iv=iv_fraction,
        mark_price=model_price_eth,
        greek_provenance_key=str(PLAN_RECONSTRUCTION_CONFIG["pricing_model"]),
        min_tick=_observed_price_tick(trade),
        min_lot=trade.amount,
        source_quality=_legacy_reconstructed_source_quality(),
    )
    lineage = ReconstructedLegLineage(
        instrument_name=trade.instrument_name,
        source_trade_id=trade.trade_id,
        source_trade_seq=trade.trade_seq,
        source_trade_timestamp_ms=trade.timestamp_ms,
        source_trade_iv=trade.iv,
        reconstructed_iv_fraction=iv_fraction,
        source_trade_index_price=trade.index_price,
        source_trade_amount=trade.amount,
        quote_age_s=quote_age_s,
        model_price_eth=model_price_eth,
        model_value_usd=greeks.value,
        delta=greeks.delta,
    )
    return leg, lineage


def _latest_eligible_trade_by_instrument(
    trades: Sequence[ParsedDeribitOptionTrade],
    *,
    snapshot_ts_ms: int,
    max_staleness_ms: int,
) -> dict[str, ParsedDeribitOptionTrade]:
    latest: dict[str, ParsedDeribitOptionTrade] = {}
    for trade in sorted(trades, key=_trade_sort_key):
        if trade.timestamp_ms > snapshot_ts_ms:
            continue
        if snapshot_ts_ms - trade.timestamp_ms > max_staleness_ms:
            continue
        if trade.expiry_ms <= snapshot_ts_ms:
            continue
        previous = latest.get(trade.instrument_name)
        if previous is None or _trade_sort_key(trade) > _trade_sort_key(previous):
            latest[trade.instrument_name] = trade
    return latest


def _latest_index_point(
    points: Sequence[IndexPathPoint],
    *,
    snapshot_ts_ms: int,
    max_staleness_ms: int,
) -> IndexPathPoint:
    latest: IndexPathPoint | None = None
    for point in sorted(points, key=lambda item: item.timestamp_ms):
        if point.timestamp_ms <= snapshot_ts_ms:
            latest = point
        else:
            break
    if latest is None:
        raise IVSurfaceCoverageError(
            "missing_index_coverage",
            f"no observed index_price at or before {snapshot_ts_ms}",
        )
    if snapshot_ts_ms - latest.timestamp_ms > max_staleness_ms:
        raise IVSurfaceCoverageError(
            "stale_index_coverage",
            f"latest observed index_price is stale at {snapshot_ts_ms}",
        )
    _require_positive(latest.index_price, "index_price")
    return latest


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
        if previous is not None and previous != point:
            raise IVSurfaceCoverageError(
                "conflicting_index_price",
                f"conflicting index_price at timestamp {point.timestamp_ms}",
            )
        by_timestamp[point.timestamp_ms] = point
    if not by_timestamp:
        raise IVSurfaceCoverageError("missing_index_coverage", "no index_path observations")
    return tuple(by_timestamp[ts] for ts in sorted(by_timestamp))


def _stress_timestamps_from_manifest(manifest: Mapping[str, Any]) -> tuple[int, ...]:
    coverage = manifest.get("coverage")
    if not isinstance(coverage, Mapping):
        return ()
    stress = coverage.get("stress_windows")
    if not isinstance(stress, Mapping):
        return ()
    windows = stress.get("windows")
    if not isinstance(windows, list):
        return ()
    timestamps: set[int] = set()
    for window in windows:
        if not isinstance(window, Mapping):
            continue
        for key in ("start_ts_ms", "end_ts_ms"):
            value = window.get(key)
            if isinstance(value, int) and not isinstance(value, bool) and value > 0:
                timestamps.add(value)
    return tuple(sorted(timestamps))


def _trade_iv_fraction(value: float) -> float:
    iv = _require_positive(value, "iv")
    return iv / 100.0 if iv > 1.0 else iv


def _observed_price_tick(trade: ParsedDeribitOptionTrade) -> float:
    places = 0
    for value in (trade.price, trade.mark_price):
        try:
            decimal = Decimal(str(value)).normalize()
        except InvalidOperation as exc:
            raise IVSurfaceCoverageError("invalid_price_precision", "invalid trade price") from exc
        exponent = decimal.as_tuple().exponent
        if not isinstance(exponent, int):
            raise IVSurfaceCoverageError(
                "invalid_price_precision", "non-finite trade price"
            )
        places = max(places, max(0, -exponent))
    tick = float(Decimal(1).scaleb(-places))
    return _require_positive(tick, "min_tick")


def _legacy_reconstructed_source_quality() -> SourceQuality:
    try:
        quality = SourceQuality[str(PLAN_SOURCE_QUALITY_BRIDGE["legacy_source_quality"])]
    except KeyError as exc:
        raise IVSurfaceReconstructionError("invalid frozen legacy source-quality bridge") from exc
    if quality is not SourceQuality.FIXTURE or quality is SourceQuality.VENUE:
        raise IVSurfaceReconstructionError(
            "reconstructed OptionLeg source_quality must be SourceQuality.FIXTURE"
        )
    return quality


def _validate_reconstruction_config() -> None:
    if PLAN_RECONSTRUCTION_CONFIG["no_future_trades"] is not True:
        raise IVSurfaceReconstructionError("frozen reconstruction config must forbid future trades")
    if str(PLAN_RECONSTRUCTION_CONFIG["pricing_model"]) != "black_scholes_from_real_trade_iv":
        raise IVSurfaceReconstructionError("unexpected reconstruction pricing model")
    _max_staleness_ms()


def _max_staleness_ms() -> int:
    hours = _require_int(
        PLAN_RECONSTRUCTION_CONFIG["max_trade_staleness_hours"],
        "max_trade_staleness_hours",
        positive=True,
    )
    return hours * _MS_PER_HOUR


def _assert_fixture_legacy_quality(chain: ReconstructedOptionChain) -> None:
    for leg in chain.snapshot.legs:
        if leg.source_quality is SourceQuality.VENUE:
            raise IVSurfaceReconstructionError("VENUE is forbidden on reconstructed OptionLeg")
        if leg.source_quality is not SourceQuality.FIXTURE:
            raise IVSurfaceReconstructionError("reconstructed OptionLeg must be FIXTURE")
    if chain.lineage.legacy_option_leg_source_quality is not SourceQuality.FIXTURE:
        raise IVSurfaceReconstructionError("lineage legacy source quality must be FIXTURE")


def _lineage_text(chains: Sequence[ReconstructedOptionChain]) -> str:
    return "".join(
        _canonical_json(chain.lineage.as_dict()) + "\n"
        for chain in sorted(chains, key=_chain_sort_key)
    )


def _lineage_manifest(chains: Sequence[ReconstructedOptionChain]) -> dict[str, Any]:
    source_trade_ids = sorted(
        {
            leg.source_trade_id
            for chain in chains
            for leg in chain.lineage.legs
        }
    )
    snapshot_timestamps = sorted({chain.snapshot.snapshot_ts_ms for chain in chains})
    return {
        "file": LINEAGE_FILE,
        "legacy_option_leg_source_quality": "SourceQuality.FIXTURE",
        "forbidden_option_leg_source_quality": "SourceQuality.VENUE",
        "free_source_quality": PLAN_SOURCE_QUALITY_BRIDGE["free_source_quality"],
        "spread_source_quality": PLAN_SOURCE_QUALITY_BRIDGE["spread_source_quality"],
        "authorizing": False,
        "capital_go_allowed": False,
        "non_authorizing_reason": PLAN_SOURCE_QUALITY_BRIDGE["non_authorizing_reason"],
        "snapshot_timestamps_ms": snapshot_timestamps,
        "source_trade_ids": source_trade_ids,
        "source_trade_count": len(source_trade_ids),
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
        "legs": [_leg_payload(leg) for leg in snapshot.legs],
        "source_quality_map": {
            key: value.value for key, value in sorted(snapshot.source_quality_map.items())
        },
        "schema_version": snapshot.schema_version,
        "manifest_sha256": snapshot.manifest_sha256,
    }


def _leg_payload(leg: OptionLeg) -> dict[str, Any]:
    return {
        "instrument_name": leg.instrument_name,
        "underlying": leg.underlying,
        "contract_multiplier": leg.contract_multiplier,
        "option_type": leg.option_type.value,
        "side": leg.side.value,
        "strike": leg.strike,
        "expiry_ms": leg.expiry_ms,
        "settlement_style": leg.settlement_style,
        "settlement_index": leg.settlement_index,
        "premium_currency": leg.premium_currency,
        "fee_currency": leg.fee_currency,
        "collateral_currency": leg.collateral_currency,
        "usd_conversion_source": leg.usd_conversion_source,
        "quote_ts_ms": leg.quote_ts_ms,
        "quote_age_s": leg.quote_age_s,
        "bid_price": leg.bid_price,
        "bid_amount": leg.bid_amount,
        "bid_iv": leg.bid_iv,
        "ask_price": leg.ask_price,
        "ask_amount": leg.ask_amount,
        "ask_iv": leg.ask_iv,
        "mark_price": leg.mark_price,
        "greek_provenance_key": leg.greek_provenance_key,
        "min_tick": leg.min_tick,
        "min_lot": leg.min_lot,
        "source_quality": leg.source_quality.value,
    }


def _pre_snapshot_hash(**payload: Any) -> str:
    converted = dict(payload)
    converted["legs"] = [_leg_payload(leg) for leg in converted["legs"]]
    return sha256_text(_canonical_json(converted))


def _manifest_text(manifest: Mapping[str, Any]) -> str:
    return json.dumps(dict(manifest), indent=2, sort_keys=True) + "\n"


def _canonical_json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"))


def _normalize_timestamps(values: Sequence[int], label: str) -> tuple[int, ...]:
    out = tuple(sorted({_require_int(value, label, positive=True) for value in values}))
    if not out:
        raise IVSurfaceCoverageError("missing_snapshot_timestamps", f"{label} is empty")
    return out


def _trade_sort_key(trade: ParsedDeribitOptionTrade) -> tuple[int, int, str, str]:
    return (trade.timestamp_ms, trade.trade_seq, trade.instrument_name, trade.trade_id)


def _instrument_sort_key(trade: ParsedDeribitOptionTrade) -> tuple[int, str, float, str]:
    return (trade.expiry_ms, trade.option_type, trade.strike, trade.instrument_name)


def _chain_sort_key(chain: ReconstructedOptionChain) -> tuple[str, int, int]:
    return (chain.snapshot.underlying, chain.snapshot.snapshot_ts_ms, chain.lineage.expiry_ms)


def _require_mapping(value: object, label: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise IVSurfaceCoverageError("invalid_manifest", f"{label} must be a mapping")
    return value


def _require_int(value: object, label: str, *, positive: bool = False) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise IVSurfaceCoverageError("invalid_integer", f"{label} must be an integer")
    if positive and value <= 0:
        raise IVSurfaceCoverageError("invalid_integer", f"{label} must be positive")
    if not positive and value < 0:
        raise IVSurfaceCoverageError("invalid_integer", f"{label} must be non-negative")
    return value


def _require_finite(value: float, label: str) -> float:
    out = float(value)
    if not math.isfinite(out):
        raise IVSurfaceCoverageError("invalid_numeric", f"{label} must be finite")
    return out


def _require_non_negative(value: float, label: str) -> float:
    out = _require_finite(value, label)
    if out < 0.0:
        raise IVSurfaceCoverageError("invalid_numeric", f"{label} must be non-negative")
    return out


def _require_positive(value: float, label: str) -> float:
    out = _require_finite(value, label)
    if out <= 0.0:
        raise IVSurfaceCoverageError("invalid_numeric", f"{label} must be positive")
    return out


__all__ = [
    "INCONCLUSIVE_STATUS",
    "LINEAGE_FILE",
    "SCHEMA_VERSION",
    "TRANSFORM_VERSION",
    "IVSurfaceCoverageError",
    "IVSurfaceReconstructionError",
    "ReconstructedLegLineage",
    "ReconstructedOptionChain",
    "ReconstructedOptionLineage",
    "reconstruct_from_history_dataset",
    "reconstruct_iv_surface",
    "reconstruct_iv_surface_at",
    "reconstructed_chains_sha256",
    "required_reconstruction_timestamps",
    "write_reconstructed_chain_cache",
]

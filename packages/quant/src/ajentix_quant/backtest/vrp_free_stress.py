"""Exact-underlying tail stress for free-data-native VRP research.

This module is intentionally pure and network-free.  Stress windows are selected only
from the real ETH index path committed by the Phase-1 free history cache, and option
P/L is replayed exclusively through ``run_vrp_backtest`` so settlement and max-loss
logic stay in the committed engine.
"""

from __future__ import annotations

import math
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any

from ajentix_quant.adapters.base import SourceQuality
from ajentix_quant.backtest.vrp_engine import (
    VrpBacktestStep,
    run_vrp_backtest,
)
from ajentix_quant.data.vrp_free_history_cache import IndexPathPoint
from ajentix_quant.options.iv_surface_reconstruction import ReconstructedOptionChain
from ajentix_quant.options.types import DefinedRiskStructure, OptionChainSnapshot
from ajentix_quant.research.vrp_free_preregistration import (
    DEFAULT_SCENARIO_ID,
    PLAN_SETTLEMENT,
    PLAN_SOURCE_QUALITY_BRIDGE,
    PLAN_STRESS_RULE,
)

SCHEMA_VERSION = "aq-vrp-free-stress-v1"
INCONCLUSIVE_STATUS = "INCONCLUSIVE"
_HOUR_MS = 60 * 60 * 1_000
_DAY_MS = 24 * _HOUR_MS
_TRAILING_DAYS = 30
_TRAILING_MS = _TRAILING_DAYS * _DAY_MS
_EPSILON = 1e-12


class VrpFreeStressStatus(StrEnum):
    """Status for the exact-underlying stress component."""

    RAN = "RAN"
    INCONCLUSIVE = INCONCLUSIVE_STATUS


class VrpFreeStressError(Exception):
    """Base error for fail-closed free stress validation failures."""


class VrpFreeStressCoverageError(VrpFreeStressError):
    """Raised when real index or reconstructed-chain coverage cannot support stress."""

    status = INCONCLUSIVE_STATUS

    def __init__(self, reason_code: str, message: str) -> None:
        self.reason_code = reason_code
        super().__init__(f"{self.status}:{reason_code}: {message}")


@dataclass(frozen=True, kw_only=True)
class StressWindow:
    """One selected 24h real ETH-index tail window.

    ``score`` is exactly the frozen ``rv_24h_over_trailing_30d_rv`` rule: the
    24h realized volatility divided by the trailing 30d realized volatility scaled
    to a 24h horizon.  All returns are exact one-hour log returns from observed
    index points; missing hourly points make the candidate ineligible rather than
    imputed.
    """

    window_id: str
    scenario_id: str
    selected_rank: int
    start_ts_ms: int
    end_ts_ms: int
    window_hours: int
    point_count: int
    start_price: float
    end_price: float
    realized_vol_24h: float
    trailing_30d_realized_vol: float
    score: float
    max_abs_1h_return: float

    def as_dict(self) -> dict[str, Any]:
        return {
            "window_id": self.window_id,
            "scenario_id": self.scenario_id,
            "selected_rank": self.selected_rank,
            "start_ts_ms": self.start_ts_ms,
            "start_utc": _iso_ms(self.start_ts_ms),
            "end_ts_ms": self.end_ts_ms,
            "end_utc": _iso_ms(self.end_ts_ms),
            "window_hours": self.window_hours,
            "point_count": self.point_count,
            "start_price": self.start_price,
            "end_price": self.end_price,
            "realized_vol_24h": self.realized_vol_24h,
            "trailing_30d_realized_vol": self.trailing_30d_realized_vol,
            "score_name": str(PLAN_STRESS_RULE["score"]),
            "score": self.score,
            "max_abs_1h_return": self.max_abs_1h_return,
            "inputs": str(PLAN_STRESS_RULE["inputs"]),
        }


@dataclass(frozen=True, kw_only=True)
class StressLedgerEventEvidence:
    """One engine ledger event checked against the max-loss invariant."""

    window_id: str
    structure_id: str
    event_type: str
    timestamp_ms: int
    reason: str
    pnl_usd: float
    max_loss_usd: float
    invariant_ok: bool
    stress: bool

    @property
    def max_loss_margin_usd(self) -> float:
        """Positive means the event P/L stayed above ``-max_loss_usd``."""

        return float(self.pnl_usd + self.max_loss_usd)

    def as_dict(self) -> dict[str, Any]:
        return {
            "window_id": self.window_id,
            "structure_id": self.structure_id,
            "event_type": self.event_type,
            "timestamp_ms": self.timestamp_ms,
            "timestamp_utc": _iso_ms(self.timestamp_ms),
            "reason": self.reason,
            "pnl_usd": self.pnl_usd,
            "max_loss_usd": self.max_loss_usd,
            "max_loss_margin_usd": self.max_loss_margin_usd,
            "invariant_ok": self.invariant_ok,
            "stress": self.stress,
        }


@dataclass(frozen=True, kw_only=True)
class StressStructureEvidence:
    """Max-loss evidence for one structure replayed through one selected window."""

    window_id: str
    structure_id: str
    entry_timestamp_ms: int
    settlement_price: float
    event_count: int
    stress_event_count: int
    worst_event_type: str
    worst_event_reason: str
    worst_pnl_usd: float
    max_loss_usd: float
    max_loss_margin_usd: float
    invariant_ok: bool
    events: tuple[StressLedgerEventEvidence, ...]

    def as_dict(self) -> dict[str, Any]:
        return {
            "window_id": self.window_id,
            "structure_id": self.structure_id,
            "entry_timestamp_ms": self.entry_timestamp_ms,
            "entry_utc": _iso_ms(self.entry_timestamp_ms),
            "settlement_price": self.settlement_price,
            "event_count": self.event_count,
            "stress_event_count": self.stress_event_count,
            "worst_event_type": self.worst_event_type,
            "worst_event_reason": self.worst_event_reason,
            "worst_pnl_usd": self.worst_pnl_usd,
            "max_loss_usd": self.max_loss_usd,
            "max_loss_margin_usd": self.max_loss_margin_usd,
            "invariant_ok": self.invariant_ok,
            "events": [event.as_dict() for event in self.events],
        }


@dataclass(frozen=True, kw_only=True)
class VrpFreeStressResult:
    """Immutable exact-underlying stress report for reconstructed/free VRP evidence."""

    scenario_id: str
    selected_windows: tuple[StressWindow, ...]
    max_loss_evidence: tuple[StressStructureEvidence, ...]
    max_loss_ok: bool
    ran: bool
    status: VrpFreeStressStatus
    lineage: Mapping[str, Any]
    reason_codes: tuple[str, ...] = ()

    def as_dict(self) -> dict[str, Any]:
        return {
            "schema_version": SCHEMA_VERSION,
            "scenario_id": self.scenario_id,
            "status": self.status.value,
            "ran": self.ran,
            "reason_codes": list(self.reason_codes),
            "max_loss_ok": self.max_loss_ok,
            "selected_windows": [window.as_dict() for window in self.selected_windows],
            "max_loss_evidence": [item.as_dict() for item in self.max_loss_evidence],
            "lineage": dict(self.lineage),
        }


def select_exact_underlying_stress_windows(
    index_path: Sequence[IndexPathPoint],
    *,
    scenario_id: str = DEFAULT_SCENARIO_ID,
) -> tuple[StressWindow, ...]:
    """Select the frozen top-k exact-underlying ETH stress windows.

    Implements ``PLAN_STRESS_RULE`` directly:
    top ``k=3`` 24h non-overlapping windows, ranked by
    ``rv_24h_over_trailing_30d_rv`` with tie-breaks
    ``max_abs_1h_return`` then earliest UTC start.  Candidate windows are the
    fixed 24h UTC blocks anchored at the frozen coverage-window start.  A block
    is eligible only when every one-hour index point for the trailing 30d lookback
    and the 24h stress window is present in ``index_path``.  Missing coverage
    raises ``VrpFreeStressCoverageError``; no synthetic index point is created.
    """

    _assert_supported_plan()
    if scenario_id != DEFAULT_SCENARIO_ID:
        raise VrpFreeStressCoverageError(
            "scenario_mismatch",
            f"scenario_id must be {DEFAULT_SCENARIO_ID!r}",
        )
    points = _normalize_index_path(index_path)
    by_ts = {point.timestamp_ms: point for point in points}
    coverage_start, coverage_end = _coverage_window_ms()
    window_hours = int(PLAN_STRESS_RULE["window_hours"])
    window_ms = window_hours * _HOUR_MS
    candidates: list[StressWindow] = []

    start = coverage_start
    while start + window_ms <= coverage_end:
        end = start + window_ms
        candidate = _candidate_window(
            by_ts,
            scenario_id=scenario_id,
            start_ts_ms=start,
            end_ts_ms=end,
            window_hours=window_hours,
        )
        if candidate is not None:
            candidates.append(candidate)
        start += window_ms

    if not candidates:
        raise VrpFreeStressCoverageError(
            "missing_required_stress_coverage",
            "no complete 24h stress windows with trailing 30d hourly index coverage",
        )

    ranked = sorted(
        candidates,
        key=lambda window: (-window.score, -window.max_abs_1h_return, window.start_ts_ms),
    )
    selected: list[StressWindow] = []
    for candidate in ranked:
        if bool(PLAN_STRESS_RULE["non_overlapping"]) and any(
            _overlaps(candidate, already) for already in selected
        ):
            continue
        selected.append(replace(candidate, selected_rank=len(selected) + 1))
        if len(selected) == int(PLAN_STRESS_RULE["k"]):
            break

    if len(selected) != int(PLAN_STRESS_RULE["k"]):
        raise VrpFreeStressCoverageError(
            "missing_required_stress_coverage",
            f"found {len(selected)} complete non-overlapping stress windows; "
            f"required {PLAN_STRESS_RULE['k']}",
        )
    return tuple(selected)


def evaluate_exact_underlying_stress(
    *,
    structures: Sequence[DefinedRiskStructure],
    index_path: Sequence[IndexPathPoint],
    reconstructed_chains: Sequence[ReconstructedOptionChain | OptionChainSnapshot],
    equity_usd: float,
    scenario_id: str = DEFAULT_SCENARIO_ID,
    cost_mode: str = "taker",
    taker_fee_bps: float | None = None,
    usd_conversion_rate: float = 1.0,
) -> VrpFreeStressResult:
    """Replay structures through selected exact-underlying stress windows.

    The function selects real ETH index windows with
    :func:`select_exact_underlying_stress_windows`, then creates one
    ``VrpBacktestStep`` per ``(window, structure)``.  Each step sends every exact
    hourly index price in the selected window as engine stress marks and uses the
    window end price as the settlement price.  Max-loss evidence is read from the
    engine ledger only: ``pnl_usd >= -max_loss_usd`` must hold for every entry,
    stress, and expiry event.
    """

    lineage = _free_lineage()
    try:
        equity = _require_positive("equity_usd", equity_usd)
        usd_rate = _require_positive("usd_conversion_rate", usd_conversion_rate)
        inputs = tuple(structures)
        if not inputs:
            return _inconclusive_result(
                scenario_id=scenario_id,
                lineage=lineage,
                reason_codes=("missing_structures",),
            )
        snapshots = _snapshots_from_reconstructed_chains(reconstructed_chains)
        _validate_reconstructed_source_quality(snapshots)
        windows = select_exact_underlying_stress_windows(index_path, scenario_id=scenario_id)
        points = _normalize_index_path(index_path)
        by_ts = {point.timestamp_ms: point for point in points}
        evidence: list[StressStructureEvidence] = []
        for window in windows:
            stress_prices = tuple(point.index_price for point in _window_points(by_ts, window))
            if not stress_prices:
                raise VrpFreeStressCoverageError(
                    "missing_required_stress_coverage",
                    f"{window.window_id} has no exact index prices",
                )
            for structure in inputs:
                snapshot = _entry_snapshot_for_structure(structure, snapshots)
                result = run_vrp_backtest(
                    [
                        VrpBacktestStep(
                            entry_timestamp_ms=snapshot.snapshot_ts_ms,
                            structure=structure,
                            entry_snapshot=snapshot,
                            exit_timestamp_ms=window.end_ts_ms,
                            settlement_price=window.end_price,
                            stress_settlement_prices=stress_prices,
                            cost_mode=cost_mode,
                            taker_fee_bps=taker_fee_bps,
                            usd_conversion_rate=usd_rate,
                        )
                    ],
                    initial_equity_usd=equity,
                )
                evidence.append(_structure_evidence(window, structure, result, window.end_price))
    except VrpFreeStressCoverageError as exc:
        return _inconclusive_result(
            scenario_id=scenario_id,
            lineage=lineage,
            reason_codes=(exc.reason_code,),
        )
    except (KeyError, ValueError, TypeError) as exc:
        return _inconclusive_result(
            scenario_id=scenario_id,
            lineage=lineage,
            reason_codes=(exc.__class__.__name__,),
        )

    max_loss_ok = bool(evidence) and all(item.invariant_ok for item in evidence)
    return VrpFreeStressResult(
        scenario_id=scenario_id,
        selected_windows=windows,
        max_loss_evidence=tuple(evidence),
        max_loss_ok=max_loss_ok,
        ran=True,
        status=VrpFreeStressStatus.RAN,
        lineage=lineage,
        reason_codes=(),
    )


def _candidate_window(
    by_ts: Mapping[int, IndexPathPoint],
    *,
    scenario_id: str,
    start_ts_ms: int,
    end_ts_ms: int,
    window_hours: int,
) -> StressWindow | None:
    window_timestamps = tuple(range(start_ts_ms, end_ts_ms + _HOUR_MS, _HOUR_MS))
    trailing_start = start_ts_ms - _TRAILING_MS
    trailing_timestamps = tuple(range(trailing_start, start_ts_ms + _HOUR_MS, _HOUR_MS))
    if any(ts not in by_ts for ts in window_timestamps):
        return None
    if any(ts not in by_ts for ts in trailing_timestamps):
        return None

    window_returns = _log_returns([by_ts[ts].index_price for ts in window_timestamps])
    trailing_returns = _log_returns([by_ts[ts].index_price for ts in trailing_timestamps])
    if len(window_returns) != window_hours or len(trailing_returns) != _TRAILING_DAYS * 24:
        return None
    rv_24h = math.sqrt(sum(value * value for value in window_returns))
    trailing_sum_sq = sum(value * value for value in trailing_returns)
    trailing_rv = math.sqrt(trailing_sum_sq / _TRAILING_DAYS)
    if trailing_rv <= _EPSILON:
        return None
    max_abs_1h = max(abs(value) for value in window_returns)
    start = by_ts[start_ts_ms]
    end = by_ts[end_ts_ms]
    return StressWindow(
        window_id=f"stress-{_iso_ms(start_ts_ms)}",
        scenario_id=scenario_id,
        selected_rank=0,
        start_ts_ms=start_ts_ms,
        end_ts_ms=end_ts_ms,
        window_hours=window_hours,
        point_count=len(window_timestamps),
        start_price=float(start.index_price),
        end_price=float(end.index_price),
        realized_vol_24h=float(rv_24h),
        trailing_30d_realized_vol=float(trailing_rv),
        score=float(rv_24h / trailing_rv),
        max_abs_1h_return=float(max_abs_1h),
    )


def _structure_evidence(
    window: StressWindow,
    structure: DefinedRiskStructure,
    result: Any,
    settlement_price: float,
) -> StressStructureEvidence:
    events = tuple(
        StressLedgerEventEvidence(
            window_id=window.window_id,
            structure_id=event.structure_id,
            event_type=event.event_type,
            timestamp_ms=event.timestamp_ms,
            reason=event.reason,
            pnl_usd=float(event.pnl_usd),
            max_loss_usd=float(event.max_loss_usd),
            invariant_ok=bool(event.invariant_ok)
            and float(event.pnl_usd) >= -float(event.max_loss_usd) - _EPSILON,
            stress=bool(event.stress),
        )
        for event in result.events
    )
    if not events:
        raise VrpFreeStressCoverageError(
            "missing_engine_events",
            f"engine returned no events for {structure.structure_id}",
        )
    worst = min(events, key=lambda item: item.pnl_usd)
    invariant_ok = bool(result.max_loss_invariant_ok) and all(
        event.invariant_ok for event in events
    )
    entry_event = next((event for event in events if event.event_type == "entry"), events[0])
    return StressStructureEvidence(
        window_id=window.window_id,
        structure_id=structure.structure_id,
        entry_timestamp_ms=entry_event.timestamp_ms,
        settlement_price=float(settlement_price),
        event_count=len(events),
        stress_event_count=sum(1 for event in events if event.stress),
        worst_event_type=worst.event_type,
        worst_event_reason=worst.reason,
        worst_pnl_usd=float(worst.pnl_usd),
        max_loss_usd=float(worst.max_loss_usd),
        max_loss_margin_usd=float(worst.max_loss_margin_usd),
        invariant_ok=invariant_ok,
        events=events,
    )


def _entry_snapshot_for_structure(
    structure: DefinedRiskStructure,
    snapshots: Sequence[OptionChainSnapshot],
) -> OptionChainSnapshot:
    names = {leg.instrument_name for leg in structure.legs}
    target_ts_ms = _entry_snapshot_ts_ms(structure)
    candidates = tuple(
        snapshot
        for snapshot in snapshots
        if snapshot.snapshot_ts_ms == target_ts_ms
        and names <= {leg.instrument_name for leg in snapshot.legs}
    )
    if len(candidates) != 1:
        raise VrpFreeStressCoverageError(
            "missing_reconstructed_entry_snapshot",
            f"expected exactly one reconstructed entry snapshot for {structure.structure_id}",
        )
    return candidates[0]


def _entry_snapshot_ts_ms(structure: DefinedRiskStructure) -> int:
    parts = structure.entry_snapshot_id.split(":", 2)
    if len(parts) >= 2:
        try:
            return int(parts[1])
        except ValueError:
            pass
    return structure.entry_quote_ts_ms


def _snapshots_from_reconstructed_chains(
    reconstructed_chains: Sequence[ReconstructedOptionChain | OptionChainSnapshot],
) -> tuple[OptionChainSnapshot, ...]:
    if not reconstructed_chains:
        raise VrpFreeStressCoverageError(
            "missing_reconstructed_chains",
            "reconstructed_chains must be non-empty",
        )
    snapshots: list[OptionChainSnapshot] = []
    for item in reconstructed_chains:
        if isinstance(item, OptionChainSnapshot):
            snapshots.append(item)
        elif isinstance(item, ReconstructedOptionChain):
            snapshots.append(item.snapshot)
        else:
            raise TypeError(
                "reconstructed_chains must contain "
                "ReconstructedOptionChain or OptionChainSnapshot"
            )
    return tuple(
        sorted(snapshots, key=lambda snapshot: (snapshot.snapshot_ts_ms, snapshot.underlying))
    )


def _validate_reconstructed_source_quality(snapshots: Sequence[OptionChainSnapshot]) -> None:
    for snapshot in snapshots:
        for leg in snapshot.legs:
            if leg.source_quality is SourceQuality.VENUE:
                raise VrpFreeStressCoverageError(
                    "forbidden_venue_source_quality",
                    "SourceQuality.VENUE is forbidden for reconstructed free stress",
                )
            if leg.source_quality is not SourceQuality.FIXTURE:
                raise VrpFreeStressCoverageError(
                    "invalid_reconstructed_source_quality",
                    "reconstructed option legs must use legacy SourceQuality.FIXTURE only",
                )
        for value in snapshot.source_quality_map.values():
            if value is not SourceQuality.FIXTURE:
                raise VrpFreeStressCoverageError(
                    "invalid_reconstructed_source_quality",
                    "reconstructed snapshot source_quality_map must use SourceQuality.FIXTURE only",
                )


def _normalize_index_path(index_path: Sequence[IndexPathPoint]) -> tuple[IndexPathPoint, ...]:
    points = tuple(index_path)
    if not points:
        raise VrpFreeStressCoverageError(
            "missing_underlying_index_path",
            "index_path must be non-empty",
        )
    previous: int | None = None
    underlying: str | None = None
    for point in points:
        if not isinstance(point, IndexPathPoint):
            raise TypeError("index_path must contain IndexPathPoint values")
        if point.underlying.upper() != "ETH":
            raise VrpFreeStressCoverageError(
                "unexpected_underlying",
                "exact-underlying stress requires ETH index points only",
            )
        if underlying is None:
            underlying = point.underlying.upper()
        if point.underlying.upper() != underlying:
            raise VrpFreeStressCoverageError(
                "mixed_underlying_index_path",
                "index_path contains more than one underlying",
            )
        _require_positive("index_price", point.index_price)
        if previous is not None and point.timestamp_ms <= previous:
            raise VrpFreeStressCoverageError(
                "unsorted_underlying_index_path",
                "index_path timestamps must be strictly ascending",
            )
        previous = point.timestamp_ms
    return points


def _window_points(
    by_ts: Mapping[int, IndexPathPoint], window: StressWindow
) -> tuple[IndexPathPoint, ...]:
    return tuple(
        by_ts[ts]
        for ts in range(window.start_ts_ms, window.end_ts_ms + _HOUR_MS, _HOUR_MS)
    )


def _log_returns(prices: Sequence[float]) -> tuple[float, ...]:
    if len(prices) < 2:
        return ()
    returns: list[float] = []
    previous = _require_positive("index_price", prices[0])
    for price in prices[1:]:
        current = _require_positive("index_price", price)
        returns.append(math.log(current / previous))
        previous = current
    return tuple(returns)


def _overlaps(left: StressWindow, right: StressWindow) -> bool:
    return left.start_ts_ms < right.end_ts_ms and right.start_ts_ms < left.end_ts_ms


def _coverage_window_ms() -> tuple[int, int]:
    raw = PLAN_STRESS_RULE["coverage_window"]
    if not isinstance(raw, (list, tuple)) or len(raw) != 2:
        raise ValueError("PLAN_STRESS_RULE.coverage_window must contain start and end")
    return _parse_iso_ms(str(raw[0])), _parse_iso_ms(str(raw[1]))


def _parse_iso_ms(value: str) -> int:
    normalized = value[:-1] + "+00:00" if value.endswith("Z") else value
    parsed = datetime.fromisoformat(normalized)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return int(parsed.astimezone(UTC).timestamp() * 1000)


def _iso_ms(ts_ms: int) -> str:
    return datetime.fromtimestamp(ts_ms / 1000, tz=UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _require_positive(name: str, value: float) -> float:
    out = float(value)
    if not math.isfinite(out) or out <= 0.0:
        raise ValueError(f"{name} must be positive")
    return out


def _free_lineage() -> dict[str, Any]:
    return {
        "free_source_quality": PLAN_SOURCE_QUALITY_BRIDGE["free_source_quality"],
        "legacy_source_quality": PLAN_SOURCE_QUALITY_BRIDGE["legacy_source_quality"],
        "legacy_option_leg_source_quality": PLAN_SOURCE_QUALITY_BRIDGE[
            "legacy_option_leg_source_quality"
        ],
        "forbidden_option_leg_source_quality": PLAN_SOURCE_QUALITY_BRIDGE[
            "forbidden_reconstructed_option_leg_source_quality"
        ],
        "authorizing": bool(PLAN_SOURCE_QUALITY_BRIDGE["authorizing"]),
        "capital_go_allowed": bool(PLAN_SOURCE_QUALITY_BRIDGE["capital_go_allowed"]),
        "non_authorizing_reason": PLAN_SOURCE_QUALITY_BRIDGE["non_authorizing_reason"],
    }


def _inconclusive_result(
    *,
    scenario_id: str,
    lineage: Mapping[str, Any],
    reason_codes: tuple[str, ...],
) -> VrpFreeStressResult:
    return VrpFreeStressResult(
        scenario_id=scenario_id,
        selected_windows=(),
        max_loss_evidence=(),
        max_loss_ok=False,
        ran=False,
        status=VrpFreeStressStatus.INCONCLUSIVE,
        lineage=dict(lineage),
        reason_codes=tuple(dict.fromkeys(reason_codes)),
    )


def _assert_supported_plan() -> None:
    expected = {
        "method": "top_k_realized_vol_expansion",
        "k": 3,
        "window_hours": 24,
        "non_overlapping": True,
        "score": "rv_24h_over_trailing_30d_rv",
        "tie_break": ["max_abs_1h_return", "earliest_utc_start"],
        "inputs": "underlying_index_only",
        "missing_required_coverage": INCONCLUSIVE_STATUS,
    }
    for key, value in expected.items():
        if PLAN_STRESS_RULE.get(key) != value:
            raise ValueError(f"unsupported PLAN_STRESS_RULE[{key!r}]")
    if PLAN_SETTLEMENT.get("settlement_index") != "deribit_eth_index":
        raise ValueError("unsupported PLAN_SETTLEMENT.settlement_index")


__all__ = [
    "INCONCLUSIVE_STATUS",
    "SCHEMA_VERSION",
    "StressLedgerEventEvidence",
    "StressStructureEvidence",
    "StressWindow",
    "VrpFreeStressCoverageError",
    "VrpFreeStressError",
    "VrpFreeStressResult",
    "VrpFreeStressStatus",
    "evaluate_exact_underlying_stress",
    "select_exact_underlying_stress_windows",
]

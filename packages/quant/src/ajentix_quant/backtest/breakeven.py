"""Deterministic strategy-v2 breakeven analysis.

This module is read-only: it consumes a loaded ``MarketDataset`` and applies the
pre-registered A1 bar on the in-sample funding slice only. The cost surface is
shared with the engine-equivalent helpers in ``backtest.costs``.
"""

from __future__ import annotations

import math
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from typing import Any

from ajentix_quant.adapters.base import MarketDataset
from ajentix_quant.backtest.costs import (
    round_trip_cost_usd,
    round_trip_cost_usd_with_fee_bps,
    safety_margin_usd,
)
from ajentix_quant.backtest.engine import _dataset_periods
from ajentix_quant.config import Settings
from ajentix_quant.research.preregistration import (
    PLAN_A1_BAR,
    PLAN_DECISION_HORIZONS,
    PLAN_EQUITY_GRID,
    PLAN_INSAMPLE_UNTIL_MS,
    PLAN_PRIMARY_EQUITY,
)
from ajentix_quant.risk.engine import RiskEngine, RiskParams
from ajentix_quant.risk.margin import VenueMarginModel
from ajentix_quant.strategies.funding_harvest import FundingHarvest
from ajentix_quant.strategies.sizing import SmallCapitalSizingPolicy

A1_CLEARS = "CLEARS"
A1_NO_GO = "NO_GO"
A1_INCONCLUSIVE = "INCONCLUSIVE"
BRANCH_A1 = "A1"
BRANCH_A2 = "A2"
BRANCH_INCONCLUSIVE = "INCONCLUSIVE"
PRIMARY_COST_MODE = "taker_roundtrip_plus_slippage"
MAKER_SENSITIVITY_LABEL = "conservative_maker_sensitivity_non_authorizing"
DEFAULT_REALIZED_VOL_ANNUAL = 0.5


@dataclass(frozen=True)
class BreakevenWindow:
    start_index: int
    end_index: int
    start_timestamp_ms: int
    end_timestamp_ms: int
    leverage: float
    notional_usd: float
    funding_sum: float
    funding_income_usd: float
    round_trip_cost_usd: float
    safety_margin_usd: float
    edge_usd: float
    qualifying: bool
    valid: bool = True
    reason: str = "valid"

    def as_dict(self) -> dict[str, Any]:
        return {
            "start_index": self.start_index,
            "end_index": self.end_index,
            "start_timestamp_ms": self.start_timestamp_ms,
            "end_timestamp_ms": self.end_timestamp_ms,
            "leverage": self.leverage,
            "notional_usd": self.notional_usd,
            "funding_sum": self.funding_sum,
            "funding_income_usd": self.funding_income_usd,
            "round_trip_cost_usd": self.round_trip_cost_usd,
            "safety_margin_usd": self.safety_margin_usd,
            "edge_usd": self.edge_usd,
            "qualifying": self.qualifying,
            "valid": self.valid,
            "reason": self.reason,
        }


@dataclass(frozen=True)
class ClusterMetrics:
    cluster_count: int
    cluster_edge_usd: tuple[float, ...]
    max_single_cluster_share: float
    top3_cluster_share: float

    def as_dict(self) -> dict[str, Any]:
        return {
            "cluster_count": self.cluster_count,
            "cluster_edge_usd": list(self.cluster_edge_usd),
            "max_single_cluster_share": self.max_single_cluster_share,
            "top3_cluster_share": self.top3_cluster_share,
        }


@dataclass(frozen=True)
class HorizonEquityBreakeven:
    horizon: int
    equity_usd: float
    total_windows: int
    valid_windows: int
    invalid_windows: int
    qualifying_windows: int
    qualifying_pct: float
    qualifying_edge_usd: float
    cluster_metrics: ClusterMetrics
    min_leverage: float | None
    max_leverage: float | None
    min_notional_usd: float | None
    max_notional_usd: float | None
    min_round_trip_cost_usd: float | None
    max_round_trip_cost_usd: float | None
    clears_availability: bool
    clears_concentration: bool
    clears_horizon_bar: bool
    reason_codes: tuple[str, ...]
    windows: tuple[BreakevenWindow, ...]

    def as_dict(self, *, include_windows: bool = False) -> dict[str, Any]:
        out: dict[str, Any] = {
            "horizon": self.horizon,
            "equity_usd": self.equity_usd,
            "total_windows": self.total_windows,
            "valid_windows": self.valid_windows,
            "invalid_windows": self.invalid_windows,
            "qualifying_windows": self.qualifying_windows,
            "qualifying_pct": self.qualifying_pct,
            "qualifying_edge_usd": self.qualifying_edge_usd,
            "cluster_metrics": self.cluster_metrics.as_dict(),
            "min_leverage": self.min_leverage,
            "max_leverage": self.max_leverage,
            "min_notional_usd": self.min_notional_usd,
            "max_notional_usd": self.max_notional_usd,
            "min_round_trip_cost_usd": self.min_round_trip_cost_usd,
            "max_round_trip_cost_usd": self.max_round_trip_cost_usd,
            "clears_availability": self.clears_availability,
            "clears_concentration": self.clears_concentration,
            "clears_horizon_bar": self.clears_horizon_bar,
            "reason_codes": list(self.reason_codes),
        }
        if include_windows:
            out["windows"] = [w.as_dict() for w in self.windows]
        return out


@dataclass(frozen=True)
class MakerSensitivity:
    label: str
    can_authorize: bool
    metrics: tuple[HorizonEquityBreakeven, ...]
    would_clear_horizons: tuple[int, ...]

    def metric_for(self, *, horizon: int, equity_usd: float) -> HorizonEquityBreakeven:
        for metric in self.metrics:
            if metric.horizon == horizon and metric.equity_usd == float(equity_usd):
                return metric
        raise KeyError((horizon, equity_usd))

    def as_dict(self, *, include_windows: bool = False) -> dict[str, Any]:
        return {
            "label": self.label,
            "can_authorize": self.can_authorize,
            "would_clear_horizons": list(self.would_clear_horizons),
            "metrics": [m.as_dict(include_windows=include_windows) for m in self.metrics],
        }


@dataclass(frozen=True)
class BreakevenResult:
    symbol: str
    funding_rows_insample: int
    insample_until_ms: int
    primary_equity_usd: float
    equity_grid: tuple[float, ...]
    decision_horizons: tuple[int, ...]
    cost_mode: str
    a1_decision: str
    branch_decision: str
    authorizing_horizons: tuple[int, ...]
    reason_codes: tuple[str, ...]
    metrics: tuple[HorizonEquityBreakeven, ...]
    maker_sensitivity: MakerSensitivity | None
    leverage_note: str
    cluster_note: str

    def metric_for(self, *, horizon: int, equity_usd: float) -> HorizonEquityBreakeven:
        for metric in self.metrics:
            if metric.horizon == horizon and metric.equity_usd == float(equity_usd):
                return metric
        raise KeyError((horizon, equity_usd))

    def as_dict(self, *, include_windows: bool = False) -> dict[str, Any]:
        return {
            "symbol": self.symbol,
            "funding_rows_insample": self.funding_rows_insample,
            "insample_until_ms": self.insample_until_ms,
            "primary_equity_usd": self.primary_equity_usd,
            "equity_grid": list(self.equity_grid),
            "decision_horizons": list(self.decision_horizons),
            "cost_mode": self.cost_mode,
            "a1_decision": self.a1_decision,
            "branch_decision": self.branch_decision,
            "authorizing_horizons": list(self.authorizing_horizons),
            "reason_codes": list(self.reason_codes),
            "metrics": [m.as_dict(include_windows=include_windows) for m in self.metrics],
            "maker_sensitivity": None
            if self.maker_sensitivity is None
            else self.maker_sensitivity.as_dict(include_windows=include_windows),
            "leverage_note": self.leverage_note,
            "cluster_note": self.cluster_note,
        }


def risk_engine_from_settings(settings: Any) -> RiskEngine:
    return RiskEngine(
        RiskParams(
            base_leverage=float(getattr(settings, "base_leverage", 2.0)),
            max_leverage=float(getattr(settings, "max_leverage", 5.0)),
            min_liq_distance_pct=float(getattr(settings, "min_liq_distance_pct", 0.15)),
            reserve_pct=float(getattr(settings, "reserve_pct", 0.25)),
            max_drawdown_pct=float(getattr(settings, "max_drawdown_pct", 0.05)),
            funding_reversal_exit_hours=int(getattr(settings, "funding_reversal_exit_hours", 24)),
            max_position_pct=float(getattr(settings, "max_position_pct", 0.25)),
            health_factor_floor=float(getattr(settings, "health_factor_floor", 1.5)),
            vol_spike_annual=float(getattr(settings, "vol_spike_annual", 1.0)),
            funding_compression_8h=float(getattr(settings, "funding_compression_8h", 0.00005)),
            funding_reversal_imminent_8h=float(
                getattr(settings, "funding_reversal_imminent_8h", 0.0)
            ),
            max_net_delta_frac=float(getattr(settings, "max_net_delta_frac", 0.02)),
            gap_stress_pct=float(getattr(settings, "gap_stress_pct", 0.20)),
            adl_rank_threshold=int(getattr(settings, "adl_rank_threshold", 3)),
        )
    )


def sizing_policy_from_settings(
    settings: Any, margin_model: VenueMarginModel
) -> SmallCapitalSizingPolicy:
    return SmallCapitalSizingPolicy(
        max_position_pct=float(getattr(settings, "max_position_pct", 0.25)),
        reserve_pct=float(getattr(settings, "reserve_pct", 0.25)),
        min_notional_usd=float(margin_model.instrument.min_notional),
    )


def analyze_symbol(
    dataset: MarketDataset,
    *,
    symbol: str,
    margin_model: VenueMarginModel,
    settings: Any | None = None,
    risk: RiskEngine | None = None,
    sizing: SmallCapitalSizingPolicy | None = None,
    decision_horizons: Iterable[int] | None = None,
    equity_grid: Iterable[float] | None = None,
    primary_equity_usd: float | None = None,
    insample_until_ms: int = PLAN_INSAMPLE_UNTIL_MS,
    a1_bar: Mapping[str, Any] | None = None,
    realized_vol_annual: float = DEFAULT_REALIZED_VOL_ANNUAL,
    include_maker_sensitivity: bool = True,
) -> BreakevenResult:
    """Run the preregistered per-symbol A1 breakeven analysis."""

    settings_obj = settings or Settings()
    risk_obj = risk or risk_engine_from_settings(settings_obj)
    sizing_obj = sizing or sizing_policy_from_settings(settings_obj, margin_model)
    horizons = tuple(int(h) for h in (decision_horizons or PLAN_DECISION_HORIZONS))
    equities = tuple(float(e) for e in (equity_grid or PLAN_EQUITY_GRID))
    primary_equity = float(primary_equity_usd or PLAN_PRIMARY_EQUITY)
    bar = dict(a1_bar or PLAN_A1_BAR)

    periods = tuple(
        period
        for period in _dataset_periods(dataset, symbol)
        if period.timestamp_ms <= insample_until_ms
    )

    primary_metrics = _compute_metrics(
        periods=periods,
        horizons=horizons,
        equities=equities,
        settings=settings_obj,
        risk=risk_obj,
        margin_model=margin_model,
        sizing=sizing_obj,
        a1_bar=bar,
        realized_vol_annual=realized_vol_annual,
        maker_sensitivity=False,
    )
    decision, branch, authorizing, reason_codes = _decide_a1(
        funding_rows_insample=len(periods),
        metrics=primary_metrics,
        horizons=horizons,
        primary_equity_usd=primary_equity,
        a1_bar=bar,
    )
    maker = None
    if include_maker_sensitivity:
        maker_metrics = _compute_metrics(
            periods=periods,
            horizons=horizons,
            equities=equities,
            settings=settings_obj,
            risk=risk_obj,
            margin_model=margin_model,
            sizing=sizing_obj,
            a1_bar=bar,
            realized_vol_annual=realized_vol_annual,
            maker_sensitivity=True,
        )
        maker_decision, _, maker_horizons, _ = _decide_a1(
            funding_rows_insample=len(periods),
            metrics=maker_metrics,
            horizons=horizons,
            primary_equity_usd=primary_equity,
            a1_bar=bar,
        )
        maker = MakerSensitivity(
            label=MAKER_SENSITIVITY_LABEL,
            can_authorize=False,
            metrics=maker_metrics,
            would_clear_horizons=maker_horizons if maker_decision == A1_CLEARS else (),
        )

    return BreakevenResult(
        symbol=symbol,
        funding_rows_insample=len(periods),
        insample_until_ms=insample_until_ms,
        primary_equity_usd=primary_equity,
        equity_grid=equities,
        decision_horizons=horizons,
        cost_mode=PRIMARY_COST_MODE,
        a1_decision=decision,
        branch_decision=branch,
        authorizing_horizons=authorizing,
        reason_codes=reason_codes,
        metrics=primary_metrics,
        maker_sensitivity=maker,
        leverage_note=(
            "Per window, leverage is the engine entry leverage: "
            "RiskEngine.dynamic_leverage_capped(realized_vol_annual=0.5, "
            "funding_rate_8h=window_start_rate, margin_model, equity), capped by "
            "FundingHarvest._BASE_LEVERAGE_CAP and rejected when the gap cap is <1x."
        ),
        cluster_note=(
            "Qualifying windows are de-overlapped into one cluster while adjacent "
            "qualifying starts are separated by less than the horizon; a new cluster "
            "starts when the next qualifying start is at least one horizon later."
        ),
    )


def _compute_metrics(
    *,
    periods: tuple[Any, ...],
    horizons: tuple[int, ...],
    equities: tuple[float, ...],
    settings: Any,
    risk: RiskEngine,
    margin_model: VenueMarginModel,
    sizing: SmallCapitalSizingPolicy,
    a1_bar: Mapping[str, Any],
    realized_vol_annual: float,
    maker_sensitivity: bool,
) -> tuple[HorizonEquityBreakeven, ...]:
    metrics: list[HorizonEquityBreakeven] = []
    for horizon in horizons:
        if horizon <= 0:
            raise ValueError("decision horizon must be positive")
        for equity in equities:
            windows = _classify_windows(
                periods=periods,
                horizon=horizon,
                equity_usd=equity,
                settings=settings,
                risk=risk,
                margin_model=margin_model,
                sizing=sizing,
                realized_vol_annual=realized_vol_annual,
                maker_sensitivity=maker_sensitivity,
                safety_margin_bps=float(a1_bar.get("safety_margin_bps", 1.0)),
            )
            metrics.append(_summarize_windows(windows, horizon, equity, a1_bar))
    return tuple(metrics)


def _classify_windows(
    *,
    periods: tuple[Any, ...],
    horizon: int,
    equity_usd: float,
    settings: Any,
    risk: RiskEngine,
    margin_model: VenueMarginModel,
    sizing: SmallCapitalSizingPolicy,
    realized_vol_annual: float,
    maker_sensitivity: bool,
    safety_margin_bps: float,
) -> tuple[BreakevenWindow, ...]:
    if len(periods) < horizon:
        return ()
    windows: list[BreakevenWindow] = []
    for start in range(0, len(periods) - horizon + 1):
        entry = periods[start]
        end = periods[start + horizon - 1]
        leverage_cap = risk.dynamic_leverage_capped(
            realized_vol_annual=realized_vol_annual,
            funding_rate_8h=float(entry.funding_rate),
            margin_model=margin_model,
            equity=max(float(equity_usd), 1e-12),
        )
        if leverage_cap < 1.0:
            windows.append(
                BreakevenWindow(
                    start_index=start,
                    end_index=start + horizon - 1,
                    start_timestamp_ms=entry.timestamp_ms,
                    end_timestamp_ms=end.timestamp_ms,
                    leverage=0.0,
                    notional_usd=0.0,
                    funding_sum=0.0,
                    funding_income_usd=0.0,
                    round_trip_cost_usd=math.inf,
                    safety_margin_usd=math.inf,
                    edge_usd=-math.inf,
                    qualifying=False,
                    valid=False,
                    reason="leverage_cap_lt_1x",
                )
            )
            continue
        leverage = max(1.0, min(leverage_cap, FundingHarvest._BASE_LEVERAGE_CAP))
        notional = sizing.size(equity_usd=float(equity_usd), leverage=leverage)
        if not sizing.feasible(equity_usd=float(equity_usd), leverage=leverage):
            windows.append(
                BreakevenWindow(
                    start_index=start,
                    end_index=start + horizon - 1,
                    start_timestamp_ms=entry.timestamp_ms,
                    end_timestamp_ms=end.timestamp_ms,
                    leverage=leverage,
                    notional_usd=notional,
                    funding_sum=0.0,
                    funding_income_usd=0.0,
                    round_trip_cost_usd=math.inf,
                    safety_margin_usd=math.inf,
                    edge_usd=-math.inf,
                    qualifying=False,
                    valid=False,
                    reason="min_notional_infeasible",
                )
            )
            continue
        if maker_sensitivity:
            cost = round_trip_cost_usd_with_fee_bps(
                spot_notional=notional,
                perp_notional=notional,
                spot_volume_notional=entry.spot_volume_notional,
                perp_volume_notional=entry.perp_volume_notional,
                settings=settings,
                spot_fee_bps=float(getattr(settings, "spot_taker_fee_bps", 10.0)),
                perp_fee_bps=float(getattr(settings, "perp_maker_fee_bps", 2.0)),
            )
        else:
            cost = round_trip_cost_usd(
                spot_notional=notional,
                perp_notional=notional,
                spot_volume_notional=entry.spot_volume_notional,
                perp_volume_notional=entry.perp_volume_notional,
                settings=settings,
            )
        margin = safety_margin_usd(notional=notional, safety_margin_bps=safety_margin_bps)
        window = periods[start : start + horizon]
        funding_sum = float(sum(float(period.funding_rate) for period in window))
        funding_income = float(funding_sum * notional)
        edge = float(funding_income - cost - margin)
        windows.append(
            BreakevenWindow(
                start_index=start,
                end_index=start + horizon - 1,
                start_timestamp_ms=entry.timestamp_ms,
                end_timestamp_ms=end.timestamp_ms,
                leverage=float(leverage),
                notional_usd=float(notional),
                funding_sum=funding_sum,
                funding_income_usd=funding_income,
                round_trip_cost_usd=float(cost),
                safety_margin_usd=float(margin),
                edge_usd=edge,
                qualifying=edge > 0.0,
            )
        )
    return tuple(windows)


def _summarize_windows(
    windows: tuple[BreakevenWindow, ...],
    horizon: int,
    equity_usd: float,
    a1_bar: Mapping[str, Any],
) -> HorizonEquityBreakeven:
    valid = tuple(w for w in windows if w.valid)
    qualifying = tuple(w for w in valid if w.qualifying)
    total = len(windows)
    valid_count = len(valid)
    qualifying_count = len(qualifying)
    qualifying_pct = float(qualifying_count / valid_count) if valid_count else 0.0
    cluster_metrics = cluster_qualifying_windows(qualifying, horizon=horizon)
    leverages = [w.leverage for w in valid]
    notionals = [w.notional_usd for w in valid]
    costs = [w.round_trip_cost_usd for w in valid]
    clears_min_valid = valid_count >= int(a1_bar.get("min_valid_windows_per_horizon", 800))
    clears_availability = (
        qualifying_pct >= float(a1_bar.get("min_qualifying_pct", 0.10))
        and qualifying_count >= int(a1_bar.get("min_qualifying_windows", 80))
    )
    clears_concentration = (
        cluster_metrics.cluster_count >= int(a1_bar.get("min_clusters", 6))
        and cluster_metrics.max_single_cluster_share
        <= float(a1_bar.get("max_single_cluster_share", 0.35))
        and cluster_metrics.top3_cluster_share <= float(a1_bar.get("max_top3_cluster_share", 0.65))
    )
    clears_horizon_bar = clears_min_valid and clears_availability and clears_concentration
    reason_codes = _metric_reason_codes(
        horizon=horizon,
        equity_usd=equity_usd,
        valid_windows=valid_count,
        clears_availability=clears_availability,
        clears_concentration=clears_concentration,
        clears_horizon_bar=clears_horizon_bar,
        cluster_metrics=cluster_metrics,
        a1_bar=a1_bar,
    )
    return HorizonEquityBreakeven(
        horizon=horizon,
        equity_usd=float(equity_usd),
        total_windows=total,
        valid_windows=valid_count,
        invalid_windows=total - valid_count,
        qualifying_windows=qualifying_count,
        qualifying_pct=qualifying_pct,
        qualifying_edge_usd=float(sum(w.edge_usd for w in qualifying)),
        cluster_metrics=cluster_metrics,
        min_leverage=min(leverages) if leverages else None,
        max_leverage=max(leverages) if leverages else None,
        min_notional_usd=min(notionals) if notionals else None,
        max_notional_usd=max(notionals) if notionals else None,
        min_round_trip_cost_usd=min(costs) if costs else None,
        max_round_trip_cost_usd=max(costs) if costs else None,
        clears_availability=clears_availability,
        clears_concentration=clears_concentration,
        clears_horizon_bar=clears_horizon_bar,
        reason_codes=reason_codes,
        windows=windows,
    )


def cluster_qualifying_windows(
    qualifying_windows: Iterable[BreakevenWindow],
    *,
    horizon: int,
) -> ClusterMetrics:
    """Cluster overlapping qualifying windows and compute edge concentration."""

    ordered = sorted(
        (w for w in qualifying_windows if w.qualifying and w.valid and w.edge_usd > 0.0),
        key=lambda w: w.start_index,
    )
    if not ordered:
        return ClusterMetrics(
            cluster_count=0,
            cluster_edge_usd=(),
            max_single_cluster_share=0.0,
            top3_cluster_share=0.0,
        )
    clusters: list[float] = []
    current_edge = 0.0
    previous_start: int | None = None
    for window in ordered:
        if previous_start is None or window.start_index - previous_start < horizon:
            current_edge += window.edge_usd
        else:
            clusters.append(current_edge)
            current_edge = window.edge_usd
        previous_start = window.start_index
    clusters.append(current_edge)
    total_edge = float(sum(clusters))
    if total_edge <= 0.0:
        max_share = 0.0
        top3_share = 0.0
    else:
        shares = sorted((edge / total_edge for edge in clusters), reverse=True)
        max_share = float(shares[0])
        top3_share = float(sum(shares[:3]))
    return ClusterMetrics(
        cluster_count=len(clusters),
        cluster_edge_usd=tuple(float(edge) for edge in clusters),
        max_single_cluster_share=max_share,
        top3_cluster_share=top3_share,
    )


def _metric_reason_codes(
    *,
    horizon: int,
    equity_usd: float,
    valid_windows: int,
    clears_availability: bool,
    clears_concentration: bool,
    clears_horizon_bar: bool,
    cluster_metrics: ClusterMetrics,
    a1_bar: Mapping[str, Any],
) -> tuple[str, ...]:
    prefix = f"H{horizon}_E{equity_usd:g}"
    reasons: list[str] = []
    if valid_windows < int(a1_bar.get("min_valid_windows_per_horizon", 800)):
        reasons.append(f"{prefix}_VALID_WINDOWS_BELOW_MIN")
    if not clears_availability:
        reasons.append(f"{prefix}_AVAILABILITY_BELOW_A1_BAR")
    if cluster_metrics.cluster_count < int(a1_bar.get("min_clusters", 6)):
        reasons.append(f"{prefix}_CLUSTERS_BELOW_MIN")
    if cluster_metrics.max_single_cluster_share > float(
        a1_bar.get("max_single_cluster_share", 0.35)
    ):
        reasons.append(f"{prefix}_SINGLE_CLUSTER_CONCENTRATION_HIGH")
    if cluster_metrics.top3_cluster_share > float(a1_bar.get("max_top3_cluster_share", 0.65)):
        reasons.append(f"{prefix}_TOP3_CLUSTER_CONCENTRATION_HIGH")
    if clears_horizon_bar:
        reasons.append(f"{prefix}_CLEARS_HORIZON_BAR")
    return tuple(reasons)


def _decide_a1(
    *,
    funding_rows_insample: int,
    metrics: tuple[HorizonEquityBreakeven, ...],
    horizons: tuple[int, ...],
    primary_equity_usd: float,
    a1_bar: Mapping[str, Any],
) -> tuple[str, str, tuple[int, ...], tuple[str, ...]]:
    metric_by_key = {(m.horizon, m.equity_usd): m for m in metrics}
    reasons: list[str] = []
    min_rows = int(a1_bar.get("min_insample_funding_rows", 900))
    min_valid = int(a1_bar.get("min_valid_windows_per_horizon", 800))
    if funding_rows_insample < min_rows:
        reasons.append("MIN_INSAMPLE_FUNDING_ROWS_NOT_MET")
    for horizon in horizons:
        primary = metric_by_key[(horizon, float(primary_equity_usd))]
        if primary.valid_windows < min_valid:
            reasons.append(f"H{horizon}_PRIMARY_VALID_WINDOWS_NOT_MET")
    if reasons:
        return A1_INCONCLUSIVE, BRANCH_INCONCLUSIVE, (), tuple(reasons)

    robustness_equities = tuple(float(e) for e in a1_bar.get("capital_robustness_equities", ()))
    authorizing: list[int] = []
    for horizon in horizons:
        primary = metric_by_key[(horizon, float(primary_equity_usd))]
        robust_metrics = [
            metric_by_key[(horizon, equity)]
            for equity in robustness_equities
            if (horizon, equity) in metric_by_key
        ]
        # Capital robustness per the locked A1 bar: the symbol+horizon must clear at the
        # primary equity AND at >=1 of the robustness equities ({$500,$2000}) -> any(), not
        # all(). This is the approved-plan contract, not leniency.
        robust_clear = any(m.clears_horizon_bar for m in robust_metrics)
        if primary.clears_horizon_bar and robust_clear:
            authorizing.append(horizon)
        else:
            if not primary.clears_horizon_bar:
                reasons.append(f"H{horizon}_PRIMARY_A1_BAR_NOT_MET")
            if not robust_clear:
                reasons.append(f"H{horizon}_CAPITAL_ROBUSTNESS_NOT_MET")
    if authorizing:
        return A1_CLEARS, BRANCH_A1, tuple(authorizing), tuple(
            f"H{h}_A1_AUTHORIZING" for h in authorizing
        )
    return A1_NO_GO, BRANCH_A2, (), tuple(reasons or ("NO_A1_AUTHORIZING_HORIZON",))

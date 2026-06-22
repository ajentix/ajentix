"""TRAIN-only breakeven harness for defined-risk VRP credit spreads.

The module mirrors the strategy-v2 breakeven branch gate but consumes immutable
``DefinedRiskStructure`` values.  It is intentionally pure and no-network: callers pass
hand-built or replayed structures, and every cost/max-loss fact used for authorization
comes from ``evaluate_structure_costs``.
"""

from __future__ import annotations

import hashlib
import json
import math
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from ajentix_quant.backtest.option_costs import evaluate_structure_costs
from ajentix_quant.options.types import DefinedRiskStructure, OptionCostBreakdown
from ajentix_quant.research.vrp_preregistration import PLAN_PRIMARY_EQUITY, PLAN_RISK_LIMITS

VRP_BREAKEVEN_SCHEMA_VERSION = "vrp-breakeven-v1"

VRP_BREAKEVEN_CLEARS = "CLEARS"
VRP_BREAKEVEN_NO_GO = "NO_GO"
VRP_BREAKEVEN_INCONCLUSIVE = "INCONCLUSIVE"

VRP_BRANCH_WALK_FORWARD = "WALK_FORWARD"
VRP_BRANCH_NO_GO = "NO_GO"
VRP_BRANCH_INCONCLUSIVE = "INCONCLUSIVE"

DEFAULT_MIN_VALID_WINDOWS = 1
DEFAULT_MIN_QUALIFYING_WINDOWS = 1
DEFAULT_MIN_QUALIFYING_PCT = 0.50
DEFAULT_MAX_SINGLE_CLUSTER_SHARE = 0.35
DEFAULT_MAX_SINGLE_EXPIRY_SHARE = 0.50
DEFAULT_MAX_QUOTE_AGE_S = 60.0
_MS_PER_WEEK = 7 * 86_400_000
_EPSILON = 1e-12


@dataclass(frozen=True, kw_only=True)
class VrpBreakevenSample:
    """One candidate structure observed at a timestamp.

    ``fold_id`` is optional because breakeven is an in-sample branch gate; when supplied it
    is carried into reason/debug output only.  ``cluster_key`` lets tests or runners pin the
    de-overlap bucket; otherwise expiry-week is used.
    """

    timestamp_ms: int
    structure: DefinedRiskStructure
    fold_id: str = "TRAIN"
    cluster_key: str | None = None
    cost_mode: str = "taker"
    taker_fee_bps: float | None = None
    settlement_fee_bps: float | None = None
    safety_margin_bps: float = 1.0
    usd_conversion_rate: float = 1.0


@dataclass(frozen=True, kw_only=True)
class VrpBreakevenWindow:
    """Costed TRAIN candidate window."""

    timestamp_ms: int
    fold_id: str
    structure_id: str
    frozen_param_key: str
    expiry_ms: int
    cluster_key: str
    cost_mode: str
    cost_breakdown: OptionCostBreakdown
    net_credit_usd: float
    total_cost_usd: float
    max_loss_usd: float
    edge_usd: float
    qualifying: bool
    valid: bool
    reason: str

    def as_dict(self) -> dict[str, Any]:
        return {
            "timestamp_ms": self.timestamp_ms,
            "fold_id": self.fold_id,
            "structure_id": self.structure_id,
            "frozen_param_key": self.frozen_param_key,
            "expiry_ms": self.expiry_ms,
            "cluster_key": self.cluster_key,
            "cost_mode": self.cost_mode,
            "cost_assumptions_hash": self.cost_breakdown.assumptions_hash,
            "authorizing_cost": self.cost_breakdown.authorizing,
            "non_authorizing_reason": self.cost_breakdown.non_authorizing_reason,
            "net_credit_usd": self.net_credit_usd,
            "total_cost_usd": self.total_cost_usd,
            "max_loss_usd": self.max_loss_usd,
            "edge_usd": self.edge_usd,
            "qualifying": self.qualifying,
            "valid": self.valid,
            "reason": self.reason,
        }


@dataclass(frozen=True, kw_only=True)
class VrpConcentrationMetrics:
    """Positive-edge concentration by de-overlapped cluster and expiry."""

    cluster_count: int
    positive_edge_usd: float
    max_single_cluster_share: float
    top3_cluster_share: float
    max_single_expiry_share: float

    def as_dict(self) -> dict[str, Any]:
        return {
            "cluster_count": self.cluster_count,
            "positive_edge_usd": self.positive_edge_usd,
            "max_single_cluster_share": self.max_single_cluster_share,
            "top3_cluster_share": self.top3_cluster_share,
            "max_single_expiry_share": self.max_single_expiry_share,
        }


@dataclass(frozen=True, kw_only=True)
class VrpStructureBranchDecision:
    """Per frozen-parameter branch feasibility result."""

    frozen_param_key: str
    structure_ids: tuple[str, ...]
    valid_windows: int
    invalid_windows: int
    qualifying_windows: int
    qualifying_pct: float
    concentration: VrpConcentrationMetrics
    clears: bool
    reason_codes: tuple[str, ...]

    def as_dict(self) -> dict[str, Any]:
        return {
            "frozen_param_key": self.frozen_param_key,
            "structure_ids": list(self.structure_ids),
            "valid_windows": self.valid_windows,
            "invalid_windows": self.invalid_windows,
            "qualifying_windows": self.qualifying_windows,
            "qualifying_pct": self.qualifying_pct,
            "concentration": self.concentration.as_dict(),
            "clears": self.clears,
            "reason_codes": list(self.reason_codes),
        }


@dataclass(frozen=True, kw_only=True)
class VrpBreakevenResult:
    """TRAIN-only branch result with a held-out-invariant freeze hash."""

    schema_version: str
    train_start_ms: int
    train_end_ms: int
    total_samples: int
    train_samples: int
    decision: str
    branch_decision: str
    selected_param_keys: tuple[str, ...]
    param_freeze_hash: str
    reason_codes: tuple[str, ...]
    structure_decisions: tuple[VrpStructureBranchDecision, ...]
    windows: tuple[VrpBreakevenWindow, ...]

    def decision_for(self, frozen_param_key: str) -> VrpStructureBranchDecision:
        for decision in self.structure_decisions:
            if decision.frozen_param_key == frozen_param_key:
                return decision
        raise KeyError(frozen_param_key)

    def as_dict(self, *, include_windows: bool = False) -> dict[str, Any]:
        out: dict[str, Any] = {
            "schema_version": self.schema_version,
            "train_start_ms": self.train_start_ms,
            "train_end_ms": self.train_end_ms,
            "total_samples": self.total_samples,
            "train_samples": self.train_samples,
            "decision": self.decision,
            "branch_decision": self.branch_decision,
            "selected_param_keys": list(self.selected_param_keys),
            "param_freeze_hash": self.param_freeze_hash,
            "reason_codes": list(self.reason_codes),
            "structure_decisions": [d.as_dict() for d in self.structure_decisions],
        }
        if include_windows:
            out["windows"] = [window.as_dict() for window in self.windows]
        return out


def analyze_vrp_breakeven(
    samples: Sequence[VrpBreakevenSample],
    *,
    train_start_ms: int,
    train_end_ms: int,
    equity_usd: float = PLAN_PRIMARY_EQUITY,
    min_valid_windows: int = DEFAULT_MIN_VALID_WINDOWS,
    min_qualifying_windows: int = DEFAULT_MIN_QUALIFYING_WINDOWS,
    min_qualifying_pct: float = DEFAULT_MIN_QUALIFYING_PCT,
    max_single_cluster_share: float = DEFAULT_MAX_SINGLE_CLUSTER_SHARE,
    max_single_expiry_share: float = DEFAULT_MAX_SINGLE_EXPIRY_SHARE,
    max_quote_age_s: float = DEFAULT_MAX_QUOTE_AGE_S,
) -> VrpBreakevenResult:
    """Evaluate the frozen breakeven bar using TRAIN rows only.

    Rows with ``timestamp_ms >= train_end_ms`` are ignored completely.  The returned
    ``param_freeze_hash`` is computed from the costed TRAIN windows only, so mutating
    held-out rows cannot affect branch output or the freeze hash.
    """

    _require_int("train_start_ms", train_start_ms)
    _require_int("train_end_ms", train_end_ms)
    if train_end_ms <= train_start_ms:
        raise ValueError("train_end_ms must be after train_start_ms")
    equity_usd = _require_positive("equity_usd", equity_usd)

    train_samples = tuple(
        sample for sample in samples if train_start_ms <= sample.timestamp_ms < train_end_ms
    )
    windows = tuple(
        _cost_window(
            sample,
            equity_usd=equity_usd,
            max_quote_age_s=max_quote_age_s,
        )
        for sample in train_samples
    )
    decisions = _structure_decisions(
        windows,
        min_valid_windows=min_valid_windows,
        min_qualifying_windows=min_qualifying_windows,
        min_qualifying_pct=min_qualifying_pct,
        max_single_cluster_share=max_single_cluster_share,
        max_single_expiry_share=max_single_expiry_share,
    )
    selected = tuple(decision.frozen_param_key for decision in decisions if decision.clears)
    decision, branch, reasons = _overall_decision(windows, decisions, selected)
    freeze_hash = _param_freeze_hash(
        train_start_ms=train_start_ms,
        train_end_ms=train_end_ms,
        equity_usd=equity_usd,
        selected_param_keys=selected,
        windows=windows,
    )
    return VrpBreakevenResult(
        schema_version=VRP_BREAKEVEN_SCHEMA_VERSION,
        train_start_ms=train_start_ms,
        train_end_ms=train_end_ms,
        total_samples=len(samples),
        train_samples=len(train_samples),
        decision=decision,
        branch_decision=branch,
        selected_param_keys=selected,
        param_freeze_hash=freeze_hash,
        reason_codes=tuple(reasons),
        structure_decisions=decisions,
        windows=windows,
    )


def _cost_window(
    sample: VrpBreakevenSample,
    *,
    equity_usd: float,
    max_quote_age_s: float,
) -> VrpBreakevenWindow:
    structure = sample.structure
    breakdown = evaluate_structure_costs(
        structure,
        taker_fee_bps=sample.taker_fee_bps,
        settlement_fee_bps=sample.settlement_fee_bps,
        safety_margin_bps=sample.safety_margin_bps,
        usd_conversion_rate=sample.usd_conversion_rate,
        cost_mode=sample.cost_mode,
    )
    max_loss = float(breakdown.max_loss_usd)
    cap = equity_usd * float(PLAN_RISK_LIMITS["per_structure_max_loss_pct"])
    valid = True
    reason = "valid"
    if not breakdown.authorizing:
        valid = False
        reason = f"non_authorizing_{breakdown.non_authorizing_reason}"
    elif structure.max_quote_age_s > max_quote_age_s:
        valid = False
        reason = "quote_age_exceeds_max"
    elif max_loss > cap + _EPSILON:
        valid = False
        reason = "per_structure_max_loss_cap"

    edge = float(breakdown.net_credit_usd - breakdown.total_cost_usd)
    return VrpBreakevenWindow(
        timestamp_ms=sample.timestamp_ms,
        fold_id=sample.fold_id,
        structure_id=structure.structure_id,
        frozen_param_key=structure.frozen_param_key,
        expiry_ms=structure.expiry_ms,
        cluster_key=sample.cluster_key or _default_cluster_key(sample),
        cost_mode=sample.cost_mode,
        cost_breakdown=breakdown,
        net_credit_usd=float(breakdown.net_credit_usd),
        total_cost_usd=float(breakdown.total_cost_usd),
        max_loss_usd=max_loss,
        edge_usd=edge,
        qualifying=valid and edge > 0.0,
        valid=valid,
        reason=reason,
    )


def _structure_decisions(
    windows: tuple[VrpBreakevenWindow, ...],
    *,
    min_valid_windows: int,
    min_qualifying_windows: int,
    min_qualifying_pct: float,
    max_single_cluster_share: float,
    max_single_expiry_share: float,
) -> tuple[VrpStructureBranchDecision, ...]:
    by_key: dict[str, list[VrpBreakevenWindow]] = {}
    for window in windows:
        by_key.setdefault(window.frozen_param_key, []).append(window)

    decisions: list[VrpStructureBranchDecision] = []
    for key in sorted(by_key):
        rows = tuple(sorted(by_key[key], key=lambda row: (row.timestamp_ms, row.structure_id)))
        valid = tuple(row for row in rows if row.valid)
        qualifying = tuple(row for row in valid if row.qualifying)
        q_pct = float(len(qualifying) / len(valid)) if valid else 0.0
        concentration = concentration_metrics(qualifying)
        reasons = _decision_reasons(
            rows,
            valid_count=len(valid),
            qualifying_count=len(qualifying),
            qualifying_pct=q_pct,
            concentration=concentration,
            min_valid_windows=min_valid_windows,
            min_qualifying_windows=min_qualifying_windows,
            min_qualifying_pct=min_qualifying_pct,
            max_single_cluster_share=max_single_cluster_share,
            max_single_expiry_share=max_single_expiry_share,
        )
        clears = reasons == ("CLEARS_BREAKEVEN_BAR",)
        decisions.append(
            VrpStructureBranchDecision(
                frozen_param_key=key,
                structure_ids=tuple(sorted({row.structure_id for row in rows})),
                valid_windows=len(valid),
                invalid_windows=len(rows) - len(valid),
                qualifying_windows=len(qualifying),
                qualifying_pct=q_pct,
                concentration=concentration,
                clears=clears,
                reason_codes=reasons,
            )
        )
    return tuple(decisions)


def concentration_metrics(
    qualifying_windows: Sequence[VrpBreakevenWindow],
) -> VrpConcentrationMetrics:
    """Compute positive-edge concentration for already de-overlapped cluster keys."""

    cluster_edge = _sum_positive_by_key(
        (window.cluster_key, window.edge_usd) for window in qualifying_windows
    )
    expiry_edge = _sum_positive_by_key(
        (str(window.expiry_ms), window.edge_usd) for window in qualifying_windows
    )
    total = float(sum(cluster_edge.values()))
    cluster_shares = _shares(cluster_edge, total)
    expiry_shares = _shares(expiry_edge, total)
    return VrpConcentrationMetrics(
        cluster_count=len(cluster_edge),
        positive_edge_usd=total,
        max_single_cluster_share=float(cluster_shares[0]) if cluster_shares else 0.0,
        top3_cluster_share=float(sum(cluster_shares[:3])) if cluster_shares else 0.0,
        max_single_expiry_share=float(expiry_shares[0]) if expiry_shares else 0.0,
    )


def _decision_reasons(
    rows: tuple[VrpBreakevenWindow, ...],
    *,
    valid_count: int,
    qualifying_count: int,
    qualifying_pct: float,
    concentration: VrpConcentrationMetrics,
    min_valid_windows: int,
    min_qualifying_windows: int,
    min_qualifying_pct: float,
    max_single_cluster_share: float,
    max_single_expiry_share: float,
) -> tuple[str, ...]:
    reasons: list[str] = []
    non_auth = sorted(
        {
            row.cost_breakdown.non_authorizing_reason or row.reason
            for row in rows
            if not row.cost_breakdown.authorizing
        }
    )
    reasons.extend(f"NON_AUTHORIZING_{reason.upper()}" for reason in non_auth)
    if valid_count < min_valid_windows:
        reasons.append("VALID_WINDOWS_BELOW_MIN")
    if qualifying_count < min_qualifying_windows:
        reasons.append("QUALIFYING_WINDOWS_BELOW_MIN")
    if qualifying_pct + _EPSILON < min_qualifying_pct:
        reasons.append("QUALIFYING_PCT_BELOW_MIN")
    if concentration.max_single_cluster_share > max_single_cluster_share + _EPSILON:
        reasons.append("CLUSTER_CONCENTRATION_HIGH")
    if concentration.max_single_expiry_share > max_single_expiry_share + _EPSILON:
        reasons.append("EXPIRY_CONCENTRATION_HIGH")
    if not reasons:
        reasons.append("CLEARS_BREAKEVEN_BAR")
    return tuple(reasons)


def _overall_decision(
    windows: tuple[VrpBreakevenWindow, ...],
    decisions: tuple[VrpStructureBranchDecision, ...],
    selected: tuple[str, ...],
) -> tuple[str, str, list[str]]:
    if selected:
        return VRP_BREAKEVEN_CLEARS, VRP_BRANCH_WALK_FORWARD, ["BREAKEVEN_AUTHORIZES_TEST"]
    if not windows:
        return VRP_BREAKEVEN_INCONCLUSIVE, VRP_BRANCH_INCONCLUSIVE, ["NO_TRAIN_WINDOWS"]
    if not any(window.valid for window in windows):
        reasons = sorted({window.reason.upper() for window in windows})
        return VRP_BREAKEVEN_INCONCLUSIVE, VRP_BRANCH_INCONCLUSIVE, reasons
    reasons = sorted({reason for decision in decisions for reason in decision.reason_codes})
    return VRP_BREAKEVEN_NO_GO, VRP_BRANCH_NO_GO, reasons or ["NO_CLEARING_STRUCTURE"]


def _param_freeze_hash(
    *,
    train_start_ms: int,
    train_end_ms: int,
    equity_usd: float,
    selected_param_keys: tuple[str, ...],
    windows: tuple[VrpBreakevenWindow, ...],
) -> str:
    payload = {
        "schema": VRP_BREAKEVEN_SCHEMA_VERSION,
        "selection": "vrp-train-only-breakeven-v1",
        "train_start_ms": train_start_ms,
        "train_end_ms": train_end_ms,
        "equity_usd": equity_usd,
        "selected_param_keys": list(selected_param_keys),
        "train_windows": [
            {
                "timestamp_ms": window.timestamp_ms,
                "structure_id": window.structure_id,
                "frozen_param_key": window.frozen_param_key,
                "cost_assumptions_hash": window.cost_breakdown.assumptions_hash,
                "qualifying": window.qualifying,
                "valid": window.valid,
            }
            for window in sorted(windows, key=lambda row: (row.timestamp_ms, row.structure_id))
        ],
    }
    return _canonical_sha256(payload)


def _default_cluster_key(sample: VrpBreakevenSample) -> str:
    week = sample.timestamp_ms // _MS_PER_WEEK
    return f"expiry={sample.structure.expiry_ms}:week={week}"


def _sum_positive_by_key(items: Iterable[tuple[str, float]]) -> dict[str, float]:
    out: dict[str, float] = {}
    for key, value in items:
        if value > 0.0:
            out[str(key)] = out.get(str(key), 0.0) + float(value)
    return out


def _shares(values: Mapping[str, float], total: float) -> list[float]:
    if total <= 0.0:
        return []
    return sorted((value / total for value in values.values()), reverse=True)


def _canonical_sha256(obj: Any) -> str:
    return hashlib.sha256(
        json.dumps(obj, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()


def _require_int(name: str, value: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError(f"{name} must be an integer")
    return value


def _require_positive(name: str, value: float) -> float:
    value = float(value)
    if not math.isfinite(value) or value <= 0.0:
        raise ValueError(f"{name} must be finite and positive")
    return value

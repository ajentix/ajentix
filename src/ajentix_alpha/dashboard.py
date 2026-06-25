"""Compact, pure summary that folds every module's output into one dashboard dict.

`report.py` runs the pipeline (scan -> size -> monitor -> calibrate -> airdrops -> points ->
rebalance) and hands the resulting objects here; this collapses them into one small, render-ready
summary. Pure and deterministic: any section whose inputs were not produced is reported as None, so
the dashboard degrades gracefully when (say) there is only one snapshot or no holdings yet.
"""

from __future__ import annotations

from typing import Any

from .airdrops.model import ScoredCampaign
from .airdrops.points import CampaignPoints
from .yields.model import ScoredPool
from .yields.monitor import MonitorReport
from .yields.rebalance import RebalancePlan
from .yields.sizing import AllocationPlan
from .yields.validate import CalibrationReport


def _top_pools(pools: list[ScoredPool], top: int) -> list[dict[str, Any]]:
    return [
        {
            "project": s.pool.project,
            "symbol": s.pool.symbol,
            "chain": s.pool.chain,
            "net_apy_pct": round(s.net_apy, 2),
            "flags": list(s.flags),
        }
        for s in pools[:top]
    ]


def build_dashboard(
    *,
    snapshot: dict[str, Any],
    ranked: list[ScoredPool],
    plan: AllocationPlan | None = None,
    alerts: MonitorReport | None = None,
    calibration: CalibrationReport | None = None,
    airdrops: list[ScoredCampaign] | None = None,
    points: list[CampaignPoints] | None = None,
    rebalance: RebalancePlan | None = None,
    top: int = 5,
) -> dict[str, Any]:
    """Fold all module outputs into one compact, render-ready summary dict."""
    core = [s for s in ranked if s.tier == "core"]
    sat = [s for s in ranked if s.tier == "satellite"]

    out: dict[str, Any] = {
        "snapshot": snapshot,
        "universe": {
            "ranked": len(ranked),
            "core": len(core),
            "satellite": len(sat),
        },
        "top_core": _top_pools(core, top),
        "top_satellite": _top_pools(sat, top),
        "allocation": None,
        "alerts": None,
        "calibration": None,
        "airdrops": None,
        "points": None,
        "rebalance": None,
    }

    if plan is not None:
        out["allocation"] = {
            "budget_usd": plan.budget_usd,
            "deployed_usd": round(plan.core_usd + plan.satellite_usd, 2),
            "cash_usd": plan.cash_usd,
            "positions": len(plan.positions),
            "blended_net_apy_on_budget_pct": round(plan.blended_net_apy_on_budget, 2),
        }
    if alerts is not None:
        out["alerts"] = {
            "watched": alerts.watched,
            "critical": alerts.critical,
            "warning": alerts.warning,
            "info": alerts.info,
            "top": [
                {"severity": a.severity, "kind": a.kind, "symbol": a.symbol, "detail": a.detail}
                for a in alerts.alerts[:top]
            ],
        }
    if calibration is not None:
        out["calibration"] = {
            "matched": calibration.matched,
            "conservatism_rate": round(calibration.conservatism_rate, 4),
            "median_signed_error_pp": round(calibration.median_signed_error, 3),
            "spike_reversion_rate": round(calibration.spike_reversion_rate, 4),
        }
    if airdrops is not None:
        out["airdrops"] = {
            "count": len(airdrops),
            "positive_ev": sum(1 for s in airdrops if s.net_ev_usd > 0),
            "top": [
                {
                    "name": s.campaign.name,
                    "annualized_ev_pct": round(s.annualized_ev_pct, 1),
                    "net_ev_usd": round(s.net_ev_usd, 2),
                    "flags": list(s.flags),
                }
                for s in airdrops[:top]
            ],
        }
    if points is not None:
        out["points"] = {
            "campaigns": len(points),
            "top": [
                {
                    "campaign": s.campaign,
                    "implied_apy_pct": (
                        round(s.implied_apy_pct, 1) if s.implied_apy_pct is not None else None
                    ),
                    "points_per_day": round(s.points_per_day, 1),
                    "flags": list(s.flags),
                }
                for s in points[:top]
            ],
        }
    if rebalance is not None:
        out["rebalance"] = {
            "n_trades": rebalance.n_trades,
            "turnover_usd": rebalance.turnover_usd,
            "actions": [
                {
                    "action": a.action,
                    "symbol": a.symbol,
                    "delta_usd": a.delta_usd,
                    "reason": a.reason,
                }
                for a in rebalance.actions
                if a.action != "HOLD"
            ][:top],
        }
    return out

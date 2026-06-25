"""Deterministic position monitoring: diff two yield snapshots and surface degradation alerts.

The scanner finds opportunities; the sizer allocates; this module watches what you hold. Given two
snapshots (a baseline and a later one) it scores every pool in BOTH and reports what got worse on
the pools you care about: APY collapse, TVL drain (exit-liquidity / bank-run), reward emissions cut,
a pool vanishing from the feed (delist / exploit), a freshly-raised risk flag, or a CORE->SATELLITE
downgrade. Pure, deterministic, offline; no price oracle, so every alert is grounded only in what
the free yields feed actually reports. The agent raises the flag; the user decides whether to exit.

Note: monitoring does NOT apply the universe filter — a position draining below the tradeable TVL
floor is precisely the alert we must not silence.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from typing import Any

from .model import ScoredPool, parse_pool, passes_universe, score_pool

# --- frozen, documented alert thresholds ----------------------------------------------------------
APY_COLLAPSE_DROP = 0.40  # net APY fell >= 40% relative -> APY_COLLAPSE
APY_ZERO_FLOOR = 0.5  # net APY now below this (in %) after a collapse -> escalate to critical
TVL_DRAIN_DROP = 0.50  # TVL fell >= 50% relative -> TVL_DRAIN
TVL_UNTRADEABLE_USD = 1_000_000.0  # TVL now below this -> escalate to critical (exit unreliable)
REWARD_CUT_DROP = 0.50  # reward APR fell >= 50% relative -> REWARD_CUT
MATERIAL_REWARD_APY = 1.0  # ignore reward changes when prior reward APR < this (noise)
NEW_OPP_MIN_NET_APY = 8.0  # in universe mode, surface brand-new pools above this net APY
_EPS = 1e-9

# Flags whose first appearance is a genuine risk increase for a holder.
_RISK_FLAGS = ("UNSTABLE", "REWARD_DEPENDENT")
_SEVERITY_RANK = {"critical": 0, "warning": 1, "info": 2}


@dataclass(frozen=True, kw_only=True)
class Alert:
    pool_id: str
    project: str
    symbol: str
    chain: str
    kind: str
    severity: str  # "critical" | "warning" | "info"
    detail: str


@dataclass(frozen=True, kw_only=True)
class MonitorReport:
    alerts: tuple[Alert, ...]
    critical: int
    warning: int
    info: int
    watched: int  # number of pools actually compared


def _index(pools: Iterable[dict[str, Any]]) -> dict[str, ScoredPool]:
    out: dict[str, ScoredPool] = {}
    for row in pools:
        scored = score_pool(parse_pool(row))
        if scored.pool.pool_id:
            out[scored.pool.pool_id] = scored
    return out


def _rel_drop(old: float, new: float) -> float:
    """Fractional decrease from old to new (0 if old is non-positive or value rose)."""
    if old <= _EPS:
        return 0.0
    return max(0.0, (old - new) / old)


def _compare(prev: ScoredPool, cur: ScoredPool) -> list[Alert]:
    p, c = prev.pool, cur.pool

    def mk(kind: str, severity: str, detail: str) -> Alert:
        return Alert(
            pool_id=c.pool_id,
            project=c.project,
            symbol=c.symbol,
            chain=c.chain,
            kind=kind,
            severity=severity,
            detail=detail,
        )

    alerts: list[Alert] = []
    apy_drop = _rel_drop(prev.net_apy, cur.net_apy)
    if apy_drop >= APY_COLLAPSE_DROP and prev.net_apy > _EPS:
        sev = "critical" if cur.net_apy < APY_ZERO_FLOOR else "warning"
        alerts.append(
            mk(
                "APY_COLLAPSE",
                sev,
                f"net APY {prev.net_apy:.2f}% -> {cur.net_apy:.2f}% (-{apy_drop * 100:.0f}%)",
            )
        )
    tvl_drop = _rel_drop(p.tvl_usd, c.tvl_usd)
    if tvl_drop >= TVL_DRAIN_DROP:
        sev = "critical" if c.tvl_usd < TVL_UNTRADEABLE_USD else "warning"
        alerts.append(
            mk(
                "TVL_DRAIN",
                sev,
                f"TVL ${p.tvl_usd:,.0f} -> ${c.tvl_usd:,.0f} (-{tvl_drop * 100:.0f}%)",
            )
        )
    reward_drop = _rel_drop(p.apy_reward, c.apy_reward)
    if p.apy_reward >= MATERIAL_REWARD_APY and reward_drop >= REWARD_CUT_DROP:
        alerts.append(
            mk(
                "REWARD_CUT",
                "warning",
                f"reward APR {p.apy_reward:.2f}% -> {c.apy_reward:.2f}% "
                f"(-{reward_drop * 100:.0f}%)",
            )
        )
    raised = [f for f in _RISK_FLAGS if f in cur.flags and f not in prev.flags]
    for flag in raised:
        alerts.append(mk("FLAG_RAISED", "warning", f"new risk flag: {flag}"))
    if prev.tier == "core" and cur.tier == "satellite":
        alerts.append(
            mk("TIER_DOWNGRADE", "warning", "CORE -> SATELLITE (lost capital-preservation)")
        )
    return alerts


def diff_snapshots(
    prev_pools: Iterable[dict[str, Any]],
    cur_pools: Iterable[dict[str, Any]],
    *,
    watch: Iterable[str] | None = None,
    include_new: bool = False,
) -> MonitorReport:
    """Diff two snapshots. `watch` restricts to specific pool_ids (your held positions)."""
    prev = _index(prev_pools)
    cur = _index(cur_pools)
    watch_set = set(watch) if watch is not None else None
    targets = watch_set if watch_set is not None else set(prev)

    alerts: list[Alert] = []
    for pool_id in targets:
        p = prev.get(pool_id)
        c = cur.get(pool_id)
        if c is None:
            if p is not None:
                alerts.append(
                    Alert(
                        pool_id=pool_id,
                        project=p.pool.project,
                        symbol=p.pool.symbol,
                        chain=p.pool.chain,
                        kind="POOL_GONE",
                        severity="critical",
                        detail="pool disappeared from the feed (delist / exploit / renamed)",
                    )
                )
            elif watch_set is not None:
                alerts.append(
                    Alert(
                        pool_id=pool_id,
                        project="",
                        symbol="",
                        chain="",
                        kind="NOT_FOUND",
                        severity="warning",
                        detail="watched pool not present in either snapshot",
                    )
                )
            continue
        if p is not None:
            alerts.extend(_compare(p, c))

    if include_new and watch_set is None:
        for pool_id, c in cur.items():
            if pool_id not in prev and passes_universe(c.pool) and c.net_apy >= NEW_OPP_MIN_NET_APY:
                alerts.append(
                    Alert(
                        pool_id=pool_id,
                        project=c.pool.project,
                        symbol=c.pool.symbol,
                        chain=c.pool.chain,
                        kind="NEW_OPPORTUNITY",
                        severity="info",
                        detail=f"new pool, net APY {c.net_apy:.2f}% ({c.tier})",
                    )
                )

    alerts.sort(key=lambda a: (_SEVERITY_RANK.get(a.severity, 9), a.kind, a.pool_id))
    return MonitorReport(
        alerts=tuple(alerts),
        critical=sum(1 for a in alerts if a.severity == "critical"),
        warning=sum(1 for a in alerts if a.severity == "warning"),
        info=sum(1 for a in alerts if a.severity == "info"),
        watched=len(targets),
    )

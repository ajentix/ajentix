"""Turn current holdings + a fresh ranked universe into a churn-aware rebalance plan.

The sizer says where capital *should* be; this diffs that target against what you *actually hold*
and emits concrete BUY / SELL / INCREASE / REDUCE / HOLD actions. Two disciplines keep it from
churning a small account to death on gas:

  - a minimum-trade threshold: dollar adjustments below it are left as HOLD (not worth the gas);
  - risk exits always fire: a held pool that has dropped out of the ranked universe (degraded /
    gone) or is on a forced-exit list (e.g. a critical monitor alert) is SOLD regardless of size.

Forced-exit and no-longer-ranked pools are removed from the universe before the target is sized, so
their capital is redeployed into what survives. Pure and deterministic. The agent plans; you sign.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterable
from dataclasses import dataclass, replace
from typing import Any

from . import costs
from .model import ScoredPool
from .sizing import DEFAULT_POLICY, SizingPolicy, build_plan

MIN_REBALANCE_USD = 50.0  # ignore adjustments smaller than this (gas / churn floor)
_EPS = 1e-9

_ACTION_ORDER = {"SELL": 0, "BUY": 1, "INCREASE": 2, "REDUCE": 3, "HOLD": 4}

def is_pool_id(value: object) -> bool:
    """True iff value is a real DefiLlama pool id (a canonical UUID).

    DefiLlama pool ids are UUIDs (e.g. ``d85a7f5f-3624-4b6b-b3a7-eefb42b2a5e9``). The shipped
    ``data/holdings.json`` template uses non-UUID placeholders (``REPLACE-with-a-real-pool-uuid``);
    this lets callers drop an unedited template instead of treating placeholders as real positions.
    """
    if not isinstance(value, str):
        return False
    try:
        uuid.UUID(value)
    except ValueError:
        return False
    return True


def real_holdings(rows: Iterable[Any]) -> list[dict[str, Any]]:
    """Keep only holding rows whose pool_id is a real UUID; drop template placeholders / junk."""
    return [r for r in rows if isinstance(r, dict) and is_pool_id(r.get("pool_id"))]


@dataclass(frozen=True, kw_only=True)
class RebalanceAction:
    pool_id: str
    project: str
    symbol: str
    chain: str
    action: str  # BUY | SELL | INCREASE | REDUCE | HOLD
    current_usd: float
    target_usd: float
    delta_usd: float  # target - current
    net_apy: float
    reason: str


@dataclass(frozen=True, kw_only=True)
class RebalancePlan:
    budget_usd: float
    actions: tuple[RebalanceAction, ...]
    turnover_usd: float  # sum of |delta| over acting moves (a gas-exposure proxy)
    n_trades: int


def build_rebalance(
    holdings: list[dict[str, Any]],
    ranked: list[ScoredPool],
    *,
    budget_usd: float | None = None,
    force_exit: Iterable[str] | None = None,
    min_trade_usd: float = MIN_REBALANCE_USD,
    policy: SizingPolicy = DEFAULT_POLICY,
    payback_days: float = 120.0,
    chain_costs: dict[str, float] | None = None,
) -> RebalancePlan:
    """Diff current holdings against a freshly-sized target into churn-aware rebalance actions."""
    held: dict[str, float] = {}
    for h in holdings:
        pid = str(h.get("pool_id", ""))
        if pid:
            held[pid] = held.get(pid, 0.0) + max(0.0, float(h.get("usd", 0.0)))

    forced = set(force_exit or ())
    ranked_ids = {s.pool.pool_id for s in ranked}
    ranked_by_id = {s.pool.pool_id: s for s in ranked}
    # Capital can come from current holdings; default the budget to what is already deployed.
    budget = float(budget_usd) if budget_usd is not None else sum(held.values())

    # Size the target over the universe MINUS forced/degraded names, so their capital redeploys.
    # Disable sizing's gas-payback filter here: the rebalancer applies its own per-move gas guard
    # below, so a held position on a costly chain is HELD rather than force-sold out of the target.
    investable = [s for s in ranked if s.pool.pool_id not in forced]
    target_plan = build_plan(
        investable, budget, policy=replace(policy, gas_payback_days=float("inf"))
    )
    target = {p.pool_id: p.usd for p in target_plan.positions}

    actions: list[RebalanceAction] = []
    turnover = 0.0
    trades = 0
    for pid in sorted(set(held) | set(target)):
        cur = held.get(pid, 0.0)
        tgt = target.get(pid, 0.0)
        delta = tgt - cur
        s = ranked_by_id.get(pid)
        net_apy = s.net_apy if s is not None else 0.0
        project = s.pool.project if s is not None else ""
        symbol = s.pool.symbol if s is not None else ""
        chain = s.pool.chain if s is not None else ""

        if pid in forced and cur > _EPS:
            action, reason = "SELL", "forced exit (alert)"
        elif cur > _EPS and pid not in ranked_ids:
            action, reason = "SELL", "no longer ranked (degraded / gone)"
        elif cur <= _EPS and tgt > _EPS:
            if tgt < min_trade_usd:
                action, reason = "HOLD", "target below min-trade; skip dust entry"
            else:
                action, reason = "BUY", "enter target position"
        elif tgt <= _EPS and cur > _EPS:
            action, reason = "SELL", "dropped from target (outranked)"
        elif abs(delta) < min_trade_usd:
            action, reason = "HOLD", "within churn threshold"
        elif delta > 0:
            action, reason = "INCREASE", "raise toward target"
        else:
            action, reason = "REDUCE", "trim toward target"

        # Cost-aware churn guard: skip a capital move whose yield can't repay round-trip gas.
        if action in ("BUY", "INCREASE", "REDUCE"):
            cost = costs.round_trip_cost(chain, chain_costs=chain_costs)
            if not costs.worth_moving(delta, net_apy, cost, payback_days=payback_days):
                action, reason = "HOLD", f"gas payback not met (~${cost:.0f} on {chain})"

        actions.append(
            RebalanceAction(
                pool_id=pid,
                project=project,
                symbol=symbol,
                chain=chain,
                action=action,
                current_usd=round(cur, 2),
                target_usd=round(tgt, 2),
                delta_usd=round(delta, 2),
                net_apy=net_apy,
                reason=reason,
            )
        )
        if action != "HOLD":
            trades += 1
            turnover += abs(delta) if action in ("INCREASE", "REDUCE") else max(cur, tgt)

    actions.sort(key=lambda a: (_ACTION_ORDER[a.action], -abs(a.delta_usd), a.pool_id))
    return RebalancePlan(
        budget_usd=round(budget, 2),
        actions=tuple(actions),
        turnover_usd=round(turnover, 2),
        n_trades=trades,
    )

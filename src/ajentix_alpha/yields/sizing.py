"""Deterministic position sizing: turn a ranked opportunity sheet into a capped $ allocation plan.

The scanner answers *which* pools are worth it. This module answers *how much* of a small retail
budget ($500-2000) to place in each, under hard risk caps. Same discipline as the ranking: every
limit is an explicit, documented constant; nothing is optimized against history; undeployed capital
is reported as cash earning 0 (no optimistic blending). The agent produces the plan; the user signs.

Sizing logic, in order:
  1. Reserve a satellite sleeve capped at SATELLITE_CAP_SHARE of budget; everything else is core.
     Unused satellite budget flows DOWN into the (safer) core sleeve, never the reverse.
  2. Within each sleeve, weight candidate pools by their conservative net APY, then water-fill under
     a per-pool cap (excess on a capped pool is redistributed to the uncapped ones).
  3. Drop any position below MIN_POSITION_USD (gas / min-deposit / ops floor) OR whose round-trip
     gas cannot be repaid within GAS_PAYBACK_DAYS at its size+chain+APY, and redistribute — so a
     small budget concentrates into a few gas-efficient positions (e.g. an Ethereum core pool whose
     capped size needs years to repay ~$30 gas is dropped in favour of cheap-chain pools).
  4. Cap the number of positions per sleeve (ops overhead is real at retail scale).
Anything that cannot be deployed under these caps stays as cash.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from . import costs
from .model import ScoredPool

# --- frozen, documented sizing policy -------------------------------------------------------------
SATELLITE_CAP_SHARE = 0.30  # hard cap: satellite (higher-risk) sleeve never exceeds 30% of budget
CORE_MAX_PER_POOL_SHARE = 0.34  # no single core position above 34% of total budget
SATELLITE_MAX_PER_POOL_SHARE = 0.10  # a single satellite bet is capped tight (10% of budget)
MIN_POSITION_USD = 50.0  # below this a position is not worth gas / min-deposit -> dropped
MAX_CORE_POSITIONS = 4  # ops/diversification balance for small capital
MAX_SATELLITE_POSITIONS = 3
GAS_PAYBACK_DAYS = 120.0  # round-trip gas must be repaid within this window, else position dropped
_EPS = 1e-9


@dataclass(frozen=True, kw_only=True)
class SizingPolicy:
    satellite_cap_share: float = SATELLITE_CAP_SHARE
    core_max_per_pool_share: float = CORE_MAX_PER_POOL_SHARE
    satellite_max_per_pool_share: float = SATELLITE_MAX_PER_POOL_SHARE
    min_position_usd: float = MIN_POSITION_USD
    max_core_positions: int = MAX_CORE_POSITIONS
    max_satellite_positions: int = MAX_SATELLITE_POSITIONS
    gas_payback_days: float = GAS_PAYBACK_DAYS  # set to inf to disable the gas-payback filter


DEFAULT_POLICY = SizingPolicy()


@dataclass(frozen=True, kw_only=True)
class Position:
    pool_id: str
    chain: str
    project: str
    symbol: str
    tier: str  # "core" | "satellite"
    usd: float
    weight_of_budget: float  # usd / budget
    net_apy: float  # conservative modeled APY for this pool
    flags: tuple[str, ...]


@dataclass(frozen=True, kw_only=True)
class AllocationPlan:
    budget_usd: float
    positions: tuple[Position, ...]
    core_usd: float
    satellite_usd: float
    cash_usd: float
    blended_net_apy_on_budget: float  # expected net APY counting idle cash as 0 (honest)
    blended_net_apy_on_allocated: float  # expected net APY of the deployed capital only
    policy: dict[str, float] = field(default_factory=dict)


def _proportional_capped(pools: list[ScoredPool], budget: float, cap_usd: float) -> list[float]:
    """Weight by net APY, then water-fill: cap each pool at cap_usd, redistribute the excess.

    Returns one dollar amount per input pool. The sum is min(budget, len*cap_usd); any shortfall
    (every pool clamped at its cap) is deliberately left undeployed by the caller as cash.
    """
    n = len(pools)
    if n == 0 or budget <= _EPS or cap_usd <= _EPS:
        return [0.0] * n
    score = [max(p.net_apy, 0.0) for p in pools]
    total = sum(score)
    weights = [s / total for s in score] if total > _EPS else [1.0 / n] * n
    alloc = [min(budget * w, cap_usd) for w in weights]
    # Redistribute excess from capped pools onto the still-uncapped ones, to convergence.
    for _ in range(n + 1):
        deployed = sum(alloc)
        remaining = budget - deployed
        if remaining <= _EPS:
            break
        free = [i for i in range(n) if alloc[i] < cap_usd - _EPS]
        if not free:
            break
        free_score = sum(score[i] for i in free)
        add = (
            {i: remaining * score[i] / free_score for i in free}
            if free_score > _EPS
            else {i: remaining / len(free) for i in free}
        )
        for i in free:
            alloc[i] = min(alloc[i] + add[i], cap_usd)
    return alloc


def _gas_ok(
    pool: ScoredPool, usd: float, payback_days: float, chain_costs: dict[str, float] | None
) -> bool:
    """True iff a `usd` position in `pool` repays its round-trip gas within `payback_days`."""
    cost = costs.round_trip_cost(pool.pool.chain, chain_costs=chain_costs)
    return costs.worth_moving(usd, pool.net_apy, cost, payback_days=payback_days)


def _allocate_sleeve(
    pools: list[ScoredPool],
    budget: float,
    cap_usd: float,
    min_usd: float,
    max_positions: int,
    *,
    payback_days: float = GAS_PAYBACK_DAYS,
    chain_costs: dict[str, float] | None = None,
) -> list[tuple[ScoredPool, float]]:
    """Allocate one sleeve, dropping positions that fall below the min size OR cannot repay their
    round-trip gas within the payback window, redistributing until every survivor clears both."""
    active = pools[:max_positions]
    while active:
        alloc = _proportional_capped(active, budget, cap_usd)
        failing = [
            i
            for i, (s, a) in enumerate(zip(active, alloc, strict=True))
            if a < min_usd - _EPS or not _gas_ok(s, a, payback_days, chain_costs)
        ]
        if not failing:
            return [(p, a) for p, a in zip(active, alloc, strict=True) if a > _EPS]
        # Drop the lowest-net-APY failing pool (input is sorted desc, so the largest index),
        # freeing its budget for higher-ranked pools that may then clear gas / the min size.
        drop = max(failing)
        active = active[:drop] + active[drop + 1 :]
    return []


def build_plan(
    ranked: list[ScoredPool], budget_usd: float, *, policy: SizingPolicy = DEFAULT_POLICY
) -> AllocationPlan:
    """Build a capped, deterministic allocation plan over an already-ranked pool list."""
    budget = max(0.0, float(budget_usd))
    core_pools = [s for s in ranked if s.tier == "core"]
    sat_pools = [s for s in ranked if s.tier == "satellite"]

    sat_alloc = _allocate_sleeve(
        sat_pools,
        budget * policy.satellite_cap_share,
        budget * policy.satellite_max_per_pool_share,
        policy.min_position_usd,
        policy.max_satellite_positions,
        payback_days=policy.gas_payback_days,
    )
    sat_used = sum(a for _, a in sat_alloc)
    # Unused satellite budget flows into the safer core sleeve (never the other way).
    core_alloc = _allocate_sleeve(
        core_pools,
        budget - sat_used,
        budget * policy.core_max_per_pool_share,
        policy.min_position_usd,
        policy.max_core_positions,
        payback_days=policy.gas_payback_days,
    )
    core_used = sum(a for _, a in core_alloc)

    def _mk(s: ScoredPool, usd: float) -> Position:
        p = s.pool
        return Position(
            pool_id=p.pool_id,
            chain=p.chain,
            project=p.project,
            symbol=p.symbol,
            tier=s.tier,
            usd=round(usd, 2),
            weight_of_budget=(usd / budget) if budget > _EPS else 0.0,
            net_apy=s.net_apy,
            flags=s.flags,
        )

    positions = [_mk(s, a) for s, a in core_alloc] + [_mk(s, a) for s, a in sat_alloc]
    deployed = core_used + sat_used
    cash = max(0.0, budget - deployed)
    yield_usd = sum(a * s.net_apy for s, a in (*core_alloc, *sat_alloc))
    return AllocationPlan(
        budget_usd=round(budget, 2),
        positions=tuple(positions),
        core_usd=round(core_used, 2),
        satellite_usd=round(sat_used, 2),
        cash_usd=round(cash, 2),
        blended_net_apy_on_budget=(yield_usd / budget) if budget > _EPS else 0.0,
        blended_net_apy_on_allocated=(yield_usd / deployed) if deployed > _EPS else 0.0,
        policy={
            "satellite_cap_share": policy.satellite_cap_share,
            "core_max_per_pool_share": policy.core_max_per_pool_share,
            "satellite_max_per_pool_share": policy.satellite_max_per_pool_share,
            "min_position_usd": policy.min_position_usd,
            "max_core_positions": float(policy.max_core_positions),
            "max_satellite_positions": float(policy.max_satellite_positions),
            "gas_payback_days": float(policy.gas_payback_days),
        },
    )

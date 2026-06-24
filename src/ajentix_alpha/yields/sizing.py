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
  3. Drop any position below MIN_POSITION_USD (gas / min-deposit / ops floor) and redistribute, so a
     tiny budget concentrates into a few real positions instead of unspendable dust.
  4. Cap the number of positions per sleeve (ops overhead is real at retail scale).
Anything that cannot be deployed under these caps stays as cash.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from .model import ScoredPool

# --- frozen, documented sizing policy -------------------------------------------------------------
SATELLITE_CAP_SHARE = 0.30  # hard cap: satellite (higher-risk) sleeve never exceeds 30% of budget
CORE_MAX_PER_POOL_SHARE = 0.34  # no single core position above 34% of total budget
SATELLITE_MAX_PER_POOL_SHARE = 0.10  # a single satellite bet is capped tight (10% of budget)
MIN_POSITION_USD = 50.0  # below this a position is not worth gas / min-deposit -> dropped
MAX_CORE_POSITIONS = 4  # ops/diversification balance for small capital
MAX_SATELLITE_POSITIONS = 3
_EPS = 1e-9


@dataclass(frozen=True, kw_only=True)
class SizingPolicy:
    satellite_cap_share: float = SATELLITE_CAP_SHARE
    core_max_per_pool_share: float = CORE_MAX_PER_POOL_SHARE
    satellite_max_per_pool_share: float = SATELLITE_MAX_PER_POOL_SHARE
    min_position_usd: float = MIN_POSITION_USD
    max_core_positions: int = MAX_CORE_POSITIONS
    max_satellite_positions: int = MAX_SATELLITE_POSITIONS


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


def _allocate_sleeve(
    pools: list[ScoredPool], budget: float, cap_usd: float, min_usd: float, max_positions: int
) -> list[tuple[ScoredPool, float]]:
    """Allocate one sleeve, dropping sub-min positions and redistributing until all survive."""
    active = pools[:max_positions]
    while active:
        alloc = _proportional_capped(active, budget, cap_usd)
        below = [i for i, a in enumerate(alloc) if a < min_usd - _EPS]
        if not below:
            return [(p, a) for p, a in zip(active, alloc, strict=True) if a > _EPS]
        # Drop the weakest sub-min pool (lowest net APY == last, since input is sorted desc).
        active = active[: max(below)] + active[max(below) + 1 :]
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
    )
    sat_used = sum(a for _, a in sat_alloc)
    # Unused satellite budget flows into the safer core sleeve (never the other way).
    core_alloc = _allocate_sleeve(
        core_pools,
        budget - sat_used,
        budget * policy.core_max_per_pool_share,
        policy.min_position_usd,
        policy.max_core_positions,
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
        },
    )

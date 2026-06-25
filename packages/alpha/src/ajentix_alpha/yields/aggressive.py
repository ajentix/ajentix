"""Aggressive 'max-yield' lens: rank the tradeable universe by QUOTED APY, not conservative net.

The deliberately-risky counterpart to model.py + sizing.py. It chases the headline APY -- and is
brutally explicit about how each pick can lose you money. Every position is a lottery ticket you can
afford to zero: hard per-pool caps, a small number of bets, and an enumerated list of loss modes per
pool. It does NOT relax the tradeable floor (TVL >= $1M etc. from passes_universe): sub-floor pools
you cannot realistically enter or exit are still excluded.

Not financial advice. Expected value is NOT the quoted APY; it is far lower, with a fat left tail.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from . import costs
from .model import ScoredPool

# Each risk flag -> the concrete way it loses you money. Iteration order = severity/likelihood.
LOSS_MODES: dict[str, str] = {
    "IL_EXPOSED": "impermanent loss: divergence between the paired assets eats principal",
    "THIN_TVL": "exit-liquidity / rug: shallow pool, slippage or no exit when you need it",
    "SPIKE": "unsustainable APY: already above its 30d mean, reverting as TVL floods in",
    "REWARD_DEPENDENT": "reward-token dump: most yield is emissions in a token that can collapse",
    "EXOTIC_STABLE": "synthetic-dollar risk: issuer / collateral / depeg-under-stress",
    "DEPEG_WATCH": "drifting peg: the underlying is already off $1",
    "DEPEG": "broken peg: principal loss in progress",
    "UNAUDITED": "no audit on record: smart-contract exploit risk",
    "YOUNG_PROTOCOL": "freshly listed: unproven, thin track record",
    "UNKNOWN_PROTOCOL": "unrecognized protocol: cannot assess audit / age",
    "IMMATURE_CHAIN": "young chain: sequencer / bridge / validator-set failure risk",
}


def loss_modes(flags: tuple[str, ...]) -> tuple[str, ...]:
    """The concrete loss modes implied by a pool's risk flags (in LOSS_MODES order)."""
    present = set(flags)
    return tuple(reason for flag, reason in LOSS_MODES.items() if flag in present)


# --- frozen degen sizing policy -------------------------------------------------------------------
DEGEN_MAX_POSITIONS = 5  # spread the lottery across several tickets, never single-bet
DEGEN_PER_POOL_CAP_SHARE = 0.25  # no single ticket above 25% of the degen budget
DEGEN_MIN_POSITION_USD = 50.0  # below this, gas / min-deposit makes it pointless
DEGEN_GAS_PAYBACK_DAYS = 120.0
_EPS = 1e-9

_WARNING = (
    "AGGRESSIVE / DEGEN MODE -- a lottery, not capital preservation. Expected value is NOT the "
    "quoted APY; it is far lower, with a real chance each position goes to ZERO (rug, exploit, "
    "impermanent loss, reward-token collapse, depeg). Size only money you can afford to lose."
)


@dataclass(frozen=True, kw_only=True)
class DegenPolicy:
    max_positions: int = DEGEN_MAX_POSITIONS
    per_pool_cap_share: float = DEGEN_PER_POOL_CAP_SHARE
    min_position_usd: float = DEGEN_MIN_POSITION_USD
    gas_payback_days: float = DEGEN_GAS_PAYBACK_DAYS


DEFAULT_DEGEN_POLICY = DegenPolicy()


@dataclass(frozen=True, kw_only=True)
class MaxYieldPick:
    pool_id: str
    chain: str
    project: str
    symbol: str
    quoted_apy: float  # raw advertised APY -- the bait
    conservative_net_apy: float  # what the conservative model trusts after haircuts
    danger_score: int  # number of distinct loss modes
    loss_modes: tuple[str, ...]
    flags: tuple[str, ...]
    usd: float = 0.0
    weight_of_budget: float = 0.0


@dataclass(frozen=True, kw_only=True)
class DegenPlan:
    budget_usd: float
    deployed_usd: float
    cash_usd: float
    blended_quoted_apy: float  # nominal headline -- NOT an expectation
    blended_net_apy: float  # the conservative model's view of the same basket
    positions: tuple[MaxYieldPick, ...]
    policy: dict[str, float] = field(default_factory=dict)


def _pick(s: ScoredPool, *, usd: float = 0.0, weight: float = 0.0) -> MaxYieldPick:
    lm = loss_modes(s.flags)
    return MaxYieldPick(
        pool_id=s.pool.pool_id,
        chain=s.pool.chain,
        project=s.pool.project,
        symbol=s.pool.symbol,
        quoted_apy=s.pool.apy,
        conservative_net_apy=s.net_apy,
        danger_score=len(lm),
        loss_modes=lm,
        flags=s.flags,
        usd=usd,
        weight_of_budget=weight,
    )


def _by_quoted_apy(s: ScoredPool) -> tuple[float, float, str]:
    return (-s.pool.apy, -s.net_apy, s.pool.pool_id)


def rank_max_yield(ranked: list[ScoredPool], *, top: int = 20) -> list[MaxYieldPick]:
    """Top pools by QUOTED APY (desc), each annotated with its loss modes. Unsized."""
    ordered = sorted(ranked, key=_by_quoted_apy)
    return [_pick(s) for s in ordered[: max(0, top)]]


def build_degen_plan(
    ranked: list[ScoredPool],
    budget_usd: float,
    *,
    policy: DegenPolicy = DEFAULT_DEGEN_POLICY,
    chain_costs: dict[str, float] | None = None,
) -> DegenPlan:
    """Equal-weight up to `max_positions` highest-quoted-APY pools, hard-capped per pool.

    Each ticket is sized budget/max_positions, capped at per_pool_cap_share, dropped if below the
    min size or unable to repay round-trip gas within the payback window. Undeployable budget stays
    cash (blended at 0%). Ordered by raw APY, not conviction -- there is no conviction here. Pools
    the conservative model has zeroed (net <= 0, e.g. a broken peg) are skipped: that is an active
    loss, not a bet, even in degen mode -- they still appear in the unsized menu, fully flagged.
    """
    budget = max(0.0, float(budget_usd))
    if budget <= 0.0 or policy.max_positions <= 0:
        return DegenPlan(
            budget_usd=budget,
            deployed_usd=0.0,
            cash_usd=budget,
            blended_quoted_apy=0.0,
            blended_net_apy=0.0,
            positions=(),
            policy=_policy_dict(policy),
        )
    per = min(budget / policy.max_positions, budget * policy.per_pool_cap_share)
    picks: list[MaxYieldPick] = []
    remaining = budget
    for s in sorted(ranked, key=_by_quoted_apy):
        if len(picks) >= policy.max_positions or remaining < policy.min_position_usd - _EPS:
            break
        if s.net_apy <= 0.0:
            continue  # model zeroed it (e.g. broken peg / hard depeg) -- active loss, not a bet
        usd = min(per, remaining)
        if usd < policy.min_position_usd - _EPS:
            continue
        cost = costs.round_trip_cost(s.pool.chain, chain_costs=chain_costs)
        if not costs.worth_moving(usd, s.pool.apy, cost, payback_days=policy.gas_payback_days):
            continue
        picks.append(_pick(s, usd=usd, weight=usd / budget))
        remaining -= usd
    deployed = sum(p.usd for p in picks)
    return DegenPlan(
        budget_usd=budget,
        deployed_usd=deployed,
        cash_usd=max(0.0, budget - deployed),
        blended_quoted_apy=sum(p.usd * p.quoted_apy for p in picks) / budget,
        blended_net_apy=sum(p.usd * p.conservative_net_apy for p in picks) / budget,
        positions=tuple(picks),
        policy=_policy_dict(policy),
    )


def _policy_dict(p: DegenPolicy) -> dict[str, float]:
    return {
        "max_positions": float(p.max_positions),
        "per_pool_cap_share": p.per_pool_cap_share,
        "min_position_usd": p.min_position_usd,
        "gas_payback_days": p.gas_payback_days,
    }


def _pick_dict(p: MaxYieldPick) -> dict[str, Any]:
    return {
        "pool_id": p.pool_id,
        "chain": p.chain,
        "project": p.project,
        "symbol": p.symbol,
        "quoted_apy_pct": round(p.quoted_apy, 3),
        "conservative_net_apy_pct": round(p.conservative_net_apy, 3),
        "danger_score": p.danger_score,
        "loss_modes": list(p.loss_modes),
        "flags": list(p.flags),
        "usd": round(p.usd, 2),
        "weight_of_budget": round(p.weight_of_budget, 4),
    }


def degen_payload(
    *, fetched_at: str, sha: str, ranked_count: int, plan: DegenPlan, menu: list[MaxYieldPick]
) -> dict[str, Any]:
    """The aggressive_plan.json payload: sized lottery bets + the full top-by-yield menu."""
    return {
        "fetched_at_utc": fetched_at,
        "snapshot_sha256": sha,
        "ranked_count": ranked_count,
        "warning": _WARNING,
        "plan": {
            "budget_usd": plan.budget_usd,
            "deployed_usd": plan.deployed_usd,
            "cash_usd": plan.cash_usd,
            "blended_quoted_apy_pct": round(plan.blended_quoted_apy, 3),
            "blended_conservative_net_apy_pct": round(plan.blended_net_apy, 3),
            "policy": plan.policy,
            "positions": [_pick_dict(p) for p in plan.positions],
        },
        "menu_top_by_quoted_apy": [_pick_dict(p) for p in menu],
        "disclaimer": _WARNING,
    }


def degen_md(plan: DegenPlan, menu: list[MaxYieldPick], fetched_at: str, sha: str) -> str:
    """The aggressive_plan.md sheet: loud banner, sized bets with loss modes, then the menu."""
    lines = [
        "# Aggressive max-yield plan (DEGEN — high risk of total loss)",
        "",
        f"> {_WARNING}",
        "",
        f"- source: DefiLlama free yields | fetched {fetched_at} | sha {sha[:12]}",
        f"- budget: ${plan.budget_usd:,.2f} | deployed ${plan.deployed_usd:,.2f} | "
        f"cash ${plan.cash_usd:,.2f}",
        f"- blended QUOTED APY {plan.blended_quoted_apy:.1f}% (headline, NOT expected) | "
        f"conservative model says {plan.blended_net_apy:.1f}%",
        f"- caps: <= {plan.policy['max_positions']:.0f} positions; <= "
        f"{plan.policy['per_pool_cap_share'] * 100:.0f}% of budget per pool; "
        f"min ${plan.policy['min_position_usd']:,.0f}.",
        "",
        "## Sized bets",
        "",
        "| $ | quoted APY% | model net% | danger | chain | project | symbol | loss modes |",
        "| ---: | ---: | ---: | :---: | --- | --- | --- | --- |",
    ]
    for p in plan.positions:
        lines.append(
            f"| {p.usd:,.2f} | {p.quoted_apy:.1f} | {p.conservative_net_apy:.1f} | "
            f"{'!' * p.danger_score or '-'} | {p.chain} | {p.project} | {p.symbol} | "
            f"{'; '.join(p.loss_modes) or '-'} |"
        )
    if plan.cash_usd > 0.0:
        lines.append(
            f"| {plan.cash_usd:,.2f} | 0.0 | 0.0 | - | - | undeployed | - | "
            "not enough qualifying tickets -> stays cash |"
        )
    lines += [
        "",
        "## Menu — top by quoted APY (the bait; most decay or break)",
        "",
        "| quoted APY% | model net% | danger | chain | project | symbol | flags |",
        "| ---: | ---: | :---: | --- | --- | --- | --- |",
    ]
    for p in menu:
        lines.append(
            f"| {p.quoted_apy:.1f} | {p.conservative_net_apy:.1f} | "
            f"{'!' * p.danger_score or '-'} | {p.chain} | {p.project} | {p.symbol} | "
            f"{', '.join(p.flags) or '-'} |"
        )
    return "\n".join(lines) + "\n"

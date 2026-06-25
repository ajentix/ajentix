"""Render the ranked opportunity sheet and capped allocation plan as JSON dicts + Markdown.

Single source of truth for these two artifacts so every CLI that emits them (scan_yields, report)
produces identical output from the same data — a standalone sheet can never lag the one-command
dashboard. Pure/deterministic: no I/O, no network, no clock.
"""

from __future__ import annotations

from typing import Any

from . import costs as cost
from .model import ScoredPool
from .sizing import AllocationPlan

_OPP_DISCLAIMER = (
    "Agent-built decision support; user executes all on-chain actions. Not advice. "
    "Conservative net APY is a model estimate after haircuts, not a guaranteed return."
)
_PLAN_DISCLAIMER = (
    "Capped, deterministic sizing over modeled net APY. Idle cash earns 0%. "
    "Agent builds the plan; the user signs every transaction. Not financial advice."
)


def opportunity_row(s: ScoredPool) -> dict[str, Any]:
    """One ranked pool as a flat, JSON-safe record."""
    p = s.pool
    return {
        "rank_key_net_apy_pct": round(s.net_apy, 3),
        "raw_apy_pct": round(p.apy, 3),
        "apy_base_pct": round(p.apy_base, 3),
        "apy_reward_pct": round(p.apy_reward, 3),
        "apy_mean_30d_pct": round(p.apy_mean_30d, 3),
        "tvl_usd": round(p.tvl_usd, 0),
        "chain": p.chain,
        "project": p.project,
        "symbol": p.symbol,
        "stablecoin": p.stablecoin,
        "il_risk": p.il_risk,
        "exposure": p.exposure,
        "history_days": p.count,
        "il_factor": s.il_factor,
        "peg_deviation": round(s.peg_deviation, 5),
        "flags": list(s.flags),
        "pool_id": p.pool_id,
    }


def breakeven_days(usd: float, net_apy: float, chain: str) -> float | None:
    """Round-trip-cost breakeven in days; None when the position earns nothing (avoids JSON inf)."""
    be = cost.breakeven_days(usd, net_apy, cost.round_trip_cost(chain))
    return None if be == float("inf") else round(be, 1)


def opportunities_payload(
    *,
    fetched_at: str,
    sha: str,
    pool_count: int,
    ranked: list[ScoredPool],
    core: list[ScoredPool],
    sat: list[ScoredPool],
    top: int,
) -> dict[str, Any]:
    """The yield_opportunities.json payload (top-sliced CORE + SATELLITE rows)."""
    return {
        "fetched_at_utc": fetched_at,
        "snapshot_sha256": sha,
        "pool_count": pool_count,
        "ranked_count": len(ranked),
        "core_count": len(core),
        "satellite_count": len(sat),
        "core": [opportunity_row(s) for s in core[:top]],
        "satellite": [opportunity_row(s) for s in sat[:top]],
        "disclaimer": _OPP_DISCLAIMER,
    }


def opportunities_md(
    core: list[ScoredPool], sat: list[ScoredPool], fetched_at: str, sha: str, top: int
) -> str:
    """The yield_opportunities.md sheet (CORE + SATELLITE tables)."""
    lines = [
        "# Yield opportunity sheet (risk-adjusted, conservative)",
        "",
        f"- source: DefiLlama free yields | fetched {fetched_at} | sha {sha[:12]}",
        "- ranking key: conservative net APY = raw APY minus a reward-stickiness haircut, "
        "capped at the 30d mean (anti-spike), times an IL factor. NOT raw APY.",
        "- flags: REWARD_DEPENDENT, SPIKE (spot > 1.5x 30d mean), UNSTABLE (high APY vol), "
        "IL_EXPOSED, THIN_TVL (< $25M).",
        "- Agent builds the sheet; you execute. Not financial advice. DeFi = total-loss risk "
        "(exploits, depegs, reward collapse); model numbers, not guarantees.",
        "",
        f"## CORE (capital-preservation: stablecoin, no-IL, deep TVL) — top {top}",
        "",
        "| net APY% | raw APY% | TVL $ | chain | project | symbol | flags |",
        "| ---: | ---: | ---: | --- | --- | --- | --- |",
    ]
    for s in core[:top]:
        p = s.pool
        lines.append(
            f"| {s.net_apy:.2f} | {p.apy:.2f} | {p.tvl_usd:,.0f} | {p.chain} | {p.project} | "
            f"{p.symbol} | {', '.join(s.flags) or '-'} |"
        )
    lines += [
        "",
        f"## SATELLITE (higher yield / higher risk — hard-cap your allocation) — top {top}",
        "",
        "| net APY% | raw APY% | TVL $ | chain | project | symbol | flags |",
        "| ---: | ---: | ---: | --- | --- | --- | --- |",
    ]
    for s in sat[:top]:
        p = s.pool
        lines.append(
            f"| {s.net_apy:.2f} | {p.apy:.2f} | {p.tvl_usd:,.0f} | {p.chain} | {p.project} | "
            f"{p.symbol} | {', '.join(s.flags) or '-'} |"
        )
    return "\n".join(lines) + "\n"


def allocation_payload(*, fetched_at: str, sha: str, plan: AllocationPlan) -> dict[str, Any]:
    """The allocation_plan.json payload (capped, deterministic sizing)."""
    return {
        "fetched_at_utc": fetched_at,
        "snapshot_sha256": sha,
        "budget_usd": plan.budget_usd,
        "core_usd": plan.core_usd,
        "satellite_usd": plan.satellite_usd,
        "cash_usd": plan.cash_usd,
        "blended_net_apy_on_budget_pct": round(plan.blended_net_apy_on_budget, 3),
        "blended_net_apy_on_allocated_pct": round(plan.blended_net_apy_on_allocated, 3),
        "policy": plan.policy,
        "positions": [
            {
                "tier": p.tier,
                "usd": p.usd,
                "weight_of_budget": round(p.weight_of_budget, 4),
                "net_apy_pct": round(p.net_apy, 3),
                "chain": p.chain,
                "project": p.project,
                "symbol": p.symbol,
                "flags": list(p.flags),
                "est_roundtrip_cost_usd": round(cost.round_trip_cost(p.chain), 2),
                "breakeven_days": breakeven_days(p.usd, p.net_apy, p.chain),
                "pool_id": p.pool_id,
            }
            for p in plan.positions
        ],
        "disclaimer": _PLAN_DISCLAIMER,
    }


def allocation_md(plan: AllocationPlan, fetched_at: str, sha: str) -> str:
    """The allocation_plan.md sheet (per-position $ table + leftover cash)."""
    b = plan.budget_usd
    lines = [
        "# Allocation plan (capped, deterministic)",
        "",
        f"- source: DefiLlama free yields | fetched {fetched_at} | sha {sha[:12]}",
        f"- budget: ${b:,.2f} | deployed ${plan.core_usd + plan.satellite_usd:,.2f} "
        f"(core ${plan.core_usd:,.2f} / satellite ${plan.satellite_usd:,.2f}) | "
        f"cash ${plan.cash_usd:,.2f}",
        f"- expected net APY: {plan.blended_net_apy_on_budget:.2f}% on budget "
        f"(idle cash counted as 0%) | {plan.blended_net_apy_on_allocated:.2f}% on deployed capital",
        "- hard caps: satellite sleeve <= "
        f"{plan.policy['satellite_cap_share'] * 100:.0f}% of budget; per-pool <= "
        f"{plan.policy['core_max_per_pool_share'] * 100:.0f}% core / "
        f"{plan.policy['satellite_max_per_pool_share'] * 100:.0f}% satellite; "
        f"min position ${plan.policy['min_position_usd']:,.0f}.",
        "- Agent builds the plan; you sign every transaction. Not financial advice. "
        "DeFi = total-loss risk; modeled numbers, not guarantees.",
        "",
        "| tier | $ | % budget | net APY% | breakeven d | chain | project | symbol | flags |",
        "| --- | ---: | ---: | ---: | ---: | --- | --- | --- | --- |",
    ]
    for p in plan.positions:
        be = breakeven_days(p.usd, p.net_apy, p.chain)
        be_str = f"{be:,.0f}" if be is not None else "inf"
        lines.append(
            f"| {p.tier} | {p.usd:,.2f} | {p.weight_of_budget * 100:.1f} | {p.net_apy:.2f} | "
            f"{be_str} | {p.chain} | {p.project} | {p.symbol} | {', '.join(p.flags) or '-'} |"
        )
    if plan.cash_usd > 0.0:
        lines.append(
            f"| cash | {plan.cash_usd:,.2f} | {plan.cash_usd / b * 100 if b else 0:.1f} | 0.00 | "
            "- | - | undeployed | - | uncapped capacity reached |"
        )
    return "\n".join(lines) + "\n"

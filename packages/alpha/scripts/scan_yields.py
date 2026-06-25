#!/usr/bin/env python3
"""Produce a risk-adjusted DeFi yield opportunity sheet from free DefiLlama data.

Fetches (or reuses a hashed snapshot of) the free DefiLlama yields api, ranks pools by a
conservative net APY, splits a capital-preservation CORE from a hard-capped SATELLITE, and writes
JSON + Markdown. The agent produces the sheet; the user executes. Not financial advice.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from ajentix_alpha.yields import costs as cost  # noqa: E402
from ajentix_alpha.yields import prices as px  # noqa: E402
from ajentix_alpha.yields import protocols as pr  # noqa: E402
from ajentix_alpha.yields.client import (  # noqa: E402
    archive_snapshot,
    fetch_pools,
    load_snapshot,
    write_snapshot,
)
from ajentix_alpha.yields.model import ScoredPool, rank_pools  # noqa: E402
from ajentix_alpha.yields.sizing import AllocationPlan, build_plan  # noqa: E402


def _row(s: ScoredPool) -> dict[str, Any]:
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


def _md(core: list[ScoredPool], sat: list[ScoredPool], fetched_at: str, sha: str, top: int) -> str:
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


def _breakeven_days(usd: float, net_apy: float, chain: str) -> float | None:
    """Round-trip-cost breakeven in days; None when the position earns nothing (avoids JSON inf)."""
    be = cost.breakeven_days(usd, net_apy, cost.round_trip_cost(chain))
    return None if be == float("inf") else round(be, 1)


def _alloc_md(plan: AllocationPlan, fetched_at: str, sha: str) -> str:
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
        be = _breakeven_days(p.usd, p.net_apy, p.chain)
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


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--fetch", action="store_true", help="Fetch live; else reuse cached snapshot."
    )
    parser.add_argument("--cache-dir", default="data/cache/yields")
    parser.add_argument("--reports-dir", default="reports")
    parser.add_argument("--top", type=int, default=20)
    parser.add_argument("--min-net-apy", type=float, default=0.0)
    parser.add_argument(
        "--budget",
        type=float,
        default=0.0,
        help="If > 0, also emit a capped $ allocation plan for this budget (USD).",
    )
    parser.add_argument(
        "--prices", action="store_true", help="Add depeg risk via free token prices (coins.llama)."
    )
    parser.add_argument(
        "--protocols",
        action="store_true",
        help="Add protocol risk (audits/age) via the free DefiLlama protocols list.",
    )
    parser.add_argument("--prices-dir", default="data/cache/prices")
    parser.add_argument("--protocols-dir", default="data/cache/protocols")
    args = parser.parse_args(argv)

    repo_root = Path(__file__).resolve().parents[1]
    cache_dir = repo_root / args.cache_dir
    if args.fetch:
        snap = write_snapshot(cache_dir, fetch_pools())
        archive_snapshot(cache_dir, snap)  # retain a history point for over-time monitoring
    else:
        snap = load_snapshot(cache_dir)

    price_map: dict[str, dict[str, Any]] | None = None
    if args.prices:
        prices_dir = repo_root / args.prices_dir
        if args.fetch:
            keys = px.stablecoin_coin_keys(list(snap.pools))
            price_map = px.fetch_prices(keys)
            px.write_snapshot(prices_dir, price_map)
        else:
            price_map = px.load_snapshot(prices_dir).prices

    proto_index: dict[str, dict[str, Any]] | None = None
    proto_now: float | None = None
    if args.protocols:
        protocols_dir = repo_root / args.protocols_dir
        if args.fetch:
            proto_index = pr.fetch_protocols()
            psnap = pr.write_snapshot(protocols_dir, proto_index)
            proto_now = psnap.fetched_at_epoch
        else:
            psnap = pr.load_snapshot(protocols_dir)
            proto_index = psnap.by_slug
            proto_now = psnap.fetched_at_epoch

    ranked = [
        s
        for s in rank_pools(
            list(snap.pools), prices=price_map, protocols=proto_index, now_ts=proto_now
        )
        if s.net_apy >= args.min_net_apy
    ]
    core = [s for s in ranked if s.tier == "core"]
    sat = [s for s in ranked if s.tier == "satellite"]

    payload = {
        "fetched_at_utc": snap.fetched_at_utc,
        "snapshot_sha256": snap.sha256,
        "pool_count": snap.pool_count,
        "ranked_count": len(ranked),
        "core_count": len(core),
        "satellite_count": len(sat),
        "core": [_row(s) for s in core[: args.top]],
        "satellite": [_row(s) for s in sat[: args.top]],
        "disclaimer": (
            "Agent-built decision support; user executes all on-chain actions. Not advice. "
            "Conservative net APY is a model estimate after haircuts, not a guaranteed return."
        ),
    }
    reports = repo_root / args.reports_dir
    reports.mkdir(parents=True, exist_ok=True)
    (reports / "yield_opportunities.json").write_text(
        json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    (reports / "yield_opportunities.md").write_text(
        _md(core, sat, snap.fetched_at_utc, snap.sha256, args.top), encoding="utf-8"
    )
    print(f"pools={snap.pool_count} ranked={len(ranked)} core={len(core)} satellite={len(sat)}")
    print(f"wrote={reports / 'yield_opportunities.json'}")
    print(f"wrote={reports / 'yield_opportunities.md'}")

    if args.budget > 0.0:
        plan = build_plan(ranked, args.budget)
        plan_payload = {
            "fetched_at_utc": snap.fetched_at_utc,
            "snapshot_sha256": snap.sha256,
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
                    "breakeven_days": _breakeven_days(p.usd, p.net_apy, p.chain),
                    "pool_id": p.pool_id,
                }
                for p in plan.positions
            ],
            "disclaimer": (
                "Capped, deterministic sizing over modeled net APY. Idle cash earns 0%. "
                "Agent builds the plan; the user signs every transaction. Not financial advice."
            ),
        }
        (reports / "allocation_plan.json").write_text(
            json.dumps(plan_payload, indent=2, sort_keys=True) + "\n", encoding="utf-8"
        )
        (reports / "allocation_plan.md").write_text(
            _alloc_md(plan, snap.fetched_at_utc, snap.sha256), encoding="utf-8"
        )
        print(
            f"budget={plan.budget_usd:.2f} deployed={plan.core_usd + plan.satellite_usd:.2f} "
            f"cash={plan.cash_usd:.2f} positions={len(plan.positions)}"
        )
        print(f"wrote={reports / 'allocation_plan.json'}")
        print(f"wrote={reports / 'allocation_plan.md'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

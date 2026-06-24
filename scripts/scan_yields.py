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

from ajentix_alpha.yields.client import (  # noqa: E402
    fetch_pools,
    load_snapshot,
    write_snapshot,
)
from ajentix_alpha.yields.model import ScoredPool, rank_pools  # noqa: E402


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


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--fetch", action="store_true", help="Fetch live; else reuse cached snapshot."
    )
    parser.add_argument("--cache-dir", default="data/cache/yields")
    parser.add_argument("--reports-dir", default="reports")
    parser.add_argument("--top", type=int, default=20)
    parser.add_argument("--min-net-apy", type=float, default=0.0)
    args = parser.parse_args(argv)

    repo_root = Path(__file__).resolve().parents[1]
    cache_dir = repo_root / args.cache_dir
    snap = write_snapshot(cache_dir, fetch_pools()) if args.fetch else load_snapshot(cache_dir)

    ranked = [s for s in rank_pools(list(snap.pools)) if s.net_apy >= args.min_net_apy]
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
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

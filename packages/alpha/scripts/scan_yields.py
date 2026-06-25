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

from ajentix_alpha.yields import prices as px  # noqa: E402
from ajentix_alpha.yields import protocols as pr  # noqa: E402
from ajentix_alpha.yields import render  # noqa: E402
from ajentix_alpha.yields.client import (  # noqa: E402
    archive_snapshot,
    fetch_pools,
    load_snapshot,
    write_snapshot,
)
from ajentix_alpha.yields.model import rank_pools  # noqa: E402
from ajentix_alpha.yields.sizing import build_plan  # noqa: E402


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

    payload = render.opportunities_payload(
        fetched_at=snap.fetched_at_utc,
        sha=snap.sha256,
        pool_count=snap.pool_count,
        ranked=ranked,
        core=core,
        sat=sat,
        top=args.top,
    )
    reports = repo_root / args.reports_dir
    reports.mkdir(parents=True, exist_ok=True)
    (reports / "yield_opportunities.json").write_text(
        json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    (reports / "yield_opportunities.md").write_text(
        render.opportunities_md(core, sat, snap.fetched_at_utc, snap.sha256, args.top),
        encoding="utf-8",
    )
    print(f"pools={snap.pool_count} ranked={len(ranked)} core={len(core)} satellite={len(sat)}")
    print(f"wrote={reports / 'yield_opportunities.json'}")
    print(f"wrote={reports / 'yield_opportunities.md'}")

    if args.budget > 0.0:
        plan = build_plan(ranked, args.budget)
        plan_payload = render.allocation_payload(
            fetched_at=snap.fetched_at_utc, sha=snap.sha256, plan=plan
        )
        (reports / "allocation_plan.json").write_text(
            json.dumps(plan_payload, indent=2, sort_keys=True) + "\n", encoding="utf-8"
        )
        (reports / "allocation_plan.md").write_text(
            render.allocation_md(plan, snap.fetched_at_utc, snap.sha256), encoding="utf-8"
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

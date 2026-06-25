#!/usr/bin/env python3
"""Aggressive 'max-yield' scan: rank the tradeable universe by QUOTED APY and size a degen lottery.

The high-risk counterpart to scan_yields.py. Emits reports/aggressive_plan.{json,md}: a small number
of hard-capped lottery bets in the highest-quoted-APY pools, each annotated with exactly how it can
go to zero. Expected value is NOT the quoted APY -- size only money you can afford to lose entirely.
This is decision support; you sign every transaction. Not financial advice.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from ajentix_alpha.yields import aggressive as agg  # noqa: E402
from ajentix_alpha.yields import prices as px  # noqa: E402
from ajentix_alpha.yields import protocols as pr  # noqa: E402
from ajentix_alpha.yields.client import (  # noqa: E402
    archive_snapshot,
    fetch_pools,
    load_snapshot,
    write_snapshot,
)
from ajentix_alpha.yields.model import rank_pools  # noqa: E402


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--fetch", action="store_true", help="Fetch live; else reuse cached.")
    parser.add_argument("--cache-dir", default="data/cache/yields")
    parser.add_argument("--reports-dir", default="reports")
    parser.add_argument("--budget", type=float, default=1000.0, help="Degen lottery budget (USD).")
    parser.add_argument("--top", type=int, default=20, help="Menu size (top pools by quoted APY).")
    parser.add_argument("--max-positions", type=int, default=agg.DEGEN_MAX_POSITIONS)
    parser.add_argument("--prices", action="store_true", help="Add depeg/exotic-stable flags.")
    parser.add_argument("--protocols", action="store_true", help="Add audit/age flags.")
    parser.add_argument("--prices-dir", default="data/cache/prices")
    parser.add_argument("--protocols-dir", default="data/cache/protocols")
    args = parser.parse_args(argv)

    repo_root = Path(__file__).resolve().parents[1]
    cache_dir = repo_root / args.cache_dir
    if args.fetch:
        snap = write_snapshot(cache_dir, fetch_pools())
        archive_snapshot(cache_dir, snap)
    else:
        snap = load_snapshot(cache_dir)

    price_map: dict[str, dict[str, Any]] | None = None
    if args.prices:
        prices_dir = repo_root / args.prices_dir
        if args.fetch:
            price_map = px.fetch_prices(px.stablecoin_coin_keys(list(snap.pools)))
            px.write_snapshot(prices_dir, price_map)
        else:
            price_map = px.load_snapshot(prices_dir).prices

    proto_index: dict[str, dict[str, Any]] | None = None
    proto_now: float | None = None
    if args.protocols:
        protocols_dir = repo_root / args.protocols_dir
        if args.fetch:
            proto_index = pr.fetch_protocols()
            proto_now = pr.write_snapshot(protocols_dir, proto_index).fetched_at_epoch
        else:
            psnap = pr.load_snapshot(protocols_dir)
            proto_index, proto_now = psnap.by_slug, psnap.fetched_at_epoch

    ranked = rank_pools(
        list(snap.pools), prices=price_map, protocols=proto_index, now_ts=proto_now
    )
    policy = agg.DegenPolicy(max_positions=args.max_positions)
    plan = agg.build_degen_plan(ranked, args.budget, policy=policy)
    menu = agg.rank_max_yield(ranked, top=args.top)
    payload = agg.degen_payload(
        fetched_at=snap.fetched_at_utc,
        sha=snap.sha256,
        ranked_count=len(ranked),
        plan=plan,
        menu=menu,
    )

    reports = repo_root / args.reports_dir
    reports.mkdir(parents=True, exist_ok=True)
    (reports / "aggressive_plan.json").write_text(
        json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    (reports / "aggressive_plan.md").write_text(
        agg.degen_md(plan, menu, snap.fetched_at_utc, snap.sha256), encoding="utf-8"
    )
    print(
        f"ranked={len(ranked)} bets={len(plan.positions)} "
        f"deployed={plan.deployed_usd:.2f} cash={plan.cash_usd:.2f} "
        f"quoted_apy={plan.blended_quoted_apy:.1f}% model_net={plan.blended_net_apy:.1f}%"
    )
    print(f"wrote={reports / 'aggressive_plan.json'}")
    print(f"wrote={reports / 'aggressive_plan.md'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

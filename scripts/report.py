#!/usr/bin/env python3
"""One command: run the whole ajentix-alpha pipeline and write a single combined dashboard.

Runs scan -> size -> monitor -> calibrate -> airdrops -> points -> rebalance in-process against the
cached (or freshly fetched) free data, then folds every result into reports/dashboard.{json,md}.
Sections degrade gracefully: monitoring/calibration need >= 2 archived snapshots; airdrops, points,
and rebalance run only when their input file exists. Agent builds the dashboard; you execute.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from ajentix_alpha.airdrops.model import rank_campaigns  # noqa: E402
from ajentix_alpha.airdrops.points import summarize as summarize_points  # noqa: E402
from ajentix_alpha.dashboard import build_dashboard  # noqa: E402
from ajentix_alpha.yields import prices as px  # noqa: E402
from ajentix_alpha.yields import protocols as pr  # noqa: E402
from ajentix_alpha.yields.client import (  # noqa: E402
    archive_snapshot,
    fetch_pools,
    list_history,
    load_snapshot,
    write_snapshot,
)
from ajentix_alpha.yields.model import rank_pools  # noqa: E402
from ajentix_alpha.yields.monitor import diff_snapshots  # noqa: E402
from ajentix_alpha.yields.rebalance import build_rebalance, real_holdings  # noqa: E402
from ajentix_alpha.yields.sizing import build_plan  # noqa: E402
from ajentix_alpha.yields.validate import calibrate  # noqa: E402


def _read_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def _md(d: dict[str, Any]) -> str:
    u = d["universe"]
    lines = [
        "# ajentix-alpha dashboard",
        "",
        f"- snapshot: fetched {d['snapshot'].get('fetched_at')} | "
        f"sha {str(d['snapshot'].get('sha', ''))[:12]} | pools {d['snapshot'].get('pool_count')}",
        f"- universe: {u['ranked']} ranked ({u['core']} core / {u['satellite']} satellite)",
        "- Agent builds the dashboard; you execute all on-chain actions. Not financial advice.",
        "",
        "## Top CORE (capital-preservation)",
        "",
        "| net APY% | chain | project | symbol | flags |",
        "| ---: | --- | --- | --- | --- |",
    ]
    for p in d["top_core"]:
        lines.append(
            f"| {p['net_apy_pct']:.2f} | {p['chain']} | {p['project']} | {p['symbol']} | "
            f"{', '.join(p['flags']) or '-'} |"
        )
    if d["allocation"]:
        a = d["allocation"]
        lines += [
            "",
            "## Allocation",
            "",
            f"- budget ${a['budget_usd']:,.0f} | deployed ${a['deployed_usd']:,.0f} | "
            f"cash ${a['cash_usd']:,.0f} | {a['positions']} positions | "
            f"blended {a['blended_net_apy_on_budget_pct']:.2f}% net APY on budget",
        ]
    if d["alerts"]:
        al = d["alerts"]
        lines += [
            "",
            "## Alerts",
            "",
            f"- {al['critical']} critical / {al['warning']} warning / {al['info']} info "
            f"across {al['watched']} watched",
        ]
        for a in al["top"]:
            lines.append(f"  - [{a['severity']}] {a['kind']} {a['symbol']}: {a['detail']}")
    if d["calibration"]:
        c = d["calibration"]
        lines += [
            "",
            "## Calibration",
            "",
            f"- conservatism {c['conservatism_rate'] * 100:.1f}% over {c['matched']} matched | "
            f"median error {c['median_signed_error_pp']:+.2f}pp | "
            f"SPIKE reversion {c['spike_reversion_rate'] * 100:.0f}%",
        ]
    if d["airdrops"]:
        ad = d["airdrops"]
        lines += ["", "## Airdrops (top by annualized EV)", ""]
        for s in ad["top"]:
            lines.append(
                f"  - {s['name']}: {s['annualized_ev_pct']:.1f}% ann. EV "
                f"(net ${s['net_ev_usd']:,.2f}) {', '.join(s['flags']) or ''}".rstrip()
            )
    if d["points"]:
        lines += ["", "## Points farming", ""]
        for s in d["points"]["top"]:
            apy = f"{s['implied_apy_pct']:.1f}%" if s["implied_apy_pct"] is not None else "n/a"
            lines.append(
                f"  - {s['campaign']}: implied APY {apy} | {s['points_per_day']:,.1f} pts/day"
            )
    if d["rebalance"]:
        rb = d["rebalance"]
        lines += [
            "",
            "## Rebalance",
            "",
            f"- {rb['n_trades']} trades | turnover ${rb['turnover_usd']:,.2f}",
        ]
        for a in rb["actions"]:
            lines.append(f"  - {a['action']} {a['symbol']} ${a['delta_usd']:+,.2f} ({a['reason']})")
    return "\n".join(lines) + "\n"


def main(argv: list[str] | None = None) -> int:  # noqa: PLR0915 - linear orchestration
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--fetch", action="store_true", help="Fetch live; else reuse cached.")
    parser.add_argument("--prices", action="store_true")
    parser.add_argument("--protocols", action="store_true")
    parser.add_argument("--budget", type=float, default=1000.0)
    parser.add_argument("--cache-dir", default="data/cache/yields")
    parser.add_argument("--prices-dir", default="data/cache/prices")
    parser.add_argument("--protocols-dir", default="data/cache/protocols")
    parser.add_argument("--campaigns", default="data/airdrops/campaigns.json")
    parser.add_argument("--points-log", default="data/airdrops/points_log.json")
    parser.add_argument("--holdings", default="data/holdings.json")
    parser.add_argument("--reports-dir", default="reports")
    parser.add_argument("--top", type=int, default=5)
    args = parser.parse_args(argv)

    root = Path(__file__).resolve().parents[1]
    cache_dir = root / args.cache_dir
    if args.fetch:
        snap = write_snapshot(cache_dir, fetch_pools())
        archive_snapshot(cache_dir, snap)
    else:
        snap = load_snapshot(cache_dir)

    price_map = None
    if args.prices:
        if args.fetch:
            price_map = px.fetch_prices(px.stablecoin_coin_keys(list(snap.pools)))
            px.write_snapshot(root / args.prices_dir, price_map)
        else:
            price_map = px.load_snapshot(root / args.prices_dir).prices
    proto_index = None
    proto_now = None
    if args.protocols:
        if args.fetch:
            proto_index = pr.fetch_protocols()
            proto_now = pr.write_snapshot(root / args.protocols_dir, proto_index).fetched_at_epoch
        else:
            psnap = pr.load_snapshot(root / args.protocols_dir)
            proto_index, proto_now = psnap.by_slug, psnap.fetched_at_epoch

    ranked = rank_pools(
        list(snap.pools), prices=price_map, protocols=proto_index, now_ts=proto_now
    )
    plan = build_plan(ranked, args.budget) if args.budget > 0 else None

    # Monitoring + calibration need two archived snapshots.
    alerts = None
    calibration = None
    history = list_history(cache_dir)
    if len(history) >= 2:
        prev_pools = list(load_snapshot(history[-2]).pools)
        cur_pools = list(load_snapshot(history[-1]).pools)
        watch = [p.pool_id for p in plan.positions] if plan is not None else None
        alerts = diff_snapshots(prev_pools, cur_pools, watch=watch)
        calibration = calibrate(prev_pools, cur_pools)

    # Best CORE net APY is the airdrop opportunity-cost baseline.
    core = [s for s in ranked if s.tier == "core"]
    baseline_apy = core[0].net_apy if core else (ranked[0].net_apy if ranked else 0.0)
    airdrops = None
    campaigns_path = root / args.campaigns
    if campaigns_path.is_file():
        data = _read_json(campaigns_path)
        rows = data.get("campaigns") if isinstance(data, dict) else data
        if isinstance(rows, list):
            airdrops = rank_campaigns(
                [r for r in rows if isinstance(r, dict)], baseline_apy_pct=baseline_apy
            )

    points = None
    points_path = root / args.points_log
    if points_path.is_file():
        data = _read_json(points_path)
        rows = data.get("entries") if isinstance(data, dict) else data
        if isinstance(rows, list):
            points = summarize_points([r for r in rows if isinstance(r, dict)])

    rebalance = None
    holdings_path = root / args.holdings
    if holdings_path.is_file():
        data = _read_json(holdings_path)
        rows = data.get("holdings") if isinstance(data, dict) else data
        held = real_holdings(rows if isinstance(rows, list) else [])
        if held:
            rebalance = build_rebalance(held, ranked)

    summary = build_dashboard(
        snapshot={
            "fetched_at": snap.fetched_at_utc,
            "sha": snap.sha256,
            "pool_count": snap.pool_count,
        },
        ranked=ranked,
        plan=plan,
        alerts=alerts,
        calibration=calibration,
        airdrops=airdrops,
        points=points,
        rebalance=rebalance,
        top=args.top,
    )
    reports = root / args.reports_dir
    reports.mkdir(parents=True, exist_ok=True)
    (reports / "dashboard.json").write_text(
        json.dumps(summary, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    (reports / "dashboard.md").write_text(_md(summary), encoding="utf-8")
    print(
        f"ranked={summary['universe']['ranked']} "
        f"alloc={'y' if plan else 'n'} alerts={'y' if alerts else 'n'} "
        f"calib={'y' if calibration else 'n'} airdrops={'y' if airdrops else 'n'} "
        f"points={'y' if points else 'n'} rebalance={'y' if rebalance else 'n'}"
    )
    print(f"wrote={reports / 'dashboard.json'}")
    print(f"wrote={reports / 'dashboard.md'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

#!/usr/bin/env python3
"""Diff your current holdings against a freshly-sized target into a churn-aware rebalance plan.

Loads the cached yields snapshot, ranks it, sizes a target for your budget, and diffs it against
`data/holdings.json` (what you actually hold) -> BUY / SELL / INCREASE / REDUCE / HOLD actions, with
a minimum-trade floor so a small account is not churned to death on gas. Point `--alerts` at an
alerts.json to force-exit any pool with a critical monitor alert. Pass `--prices` / `--protocols`
to apply the same depeg / protocol-risk layers as the scanner (from their cached snapshots) so the
standalone plan ranks against the identical filtered universe as the dashboard. The agent plans;
you sign.
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
from ajentix_alpha.yields.client import load_snapshot  # noqa: E402
from ajentix_alpha.yields.model import rank_pools  # noqa: E402
from ajentix_alpha.yields.rebalance import (  # noqa: E402
    MIN_REBALANCE_USD,
    RebalancePlan,
    build_rebalance,
    real_holdings,
)


def _load_holdings(path: Path) -> list[dict[str, Any]]:
    data = json.loads(path.read_text(encoding="utf-8"))
    rows = data.get("holdings") if isinstance(data, dict) else data
    if not isinstance(rows, list):
        raise ValueError(f"{path}: expected a list of holdings or {{'holdings': [...]}}")
    return real_holdings(rows)


def _load_forced_exits(path: Path) -> set[str]:
    """Pool ids with a critical alert (from monitor's alerts.json) -> force-exit."""
    data = json.loads(path.read_text(encoding="utf-8"))
    alerts = data.get("alerts", []) if isinstance(data, dict) else []
    out: set[str] = set()
    if isinstance(alerts, list):
        for a in alerts:
            if isinstance(a, dict) and a.get("severity") == "critical" and a.get("pool_id"):
                out.add(str(a["pool_id"]))
    return out


def _md(plan: RebalancePlan, holdings_total: float) -> str:
    lines = [
        "# Rebalance plan (churn-aware)",
        "",
        f"- current holdings: ${holdings_total:,.2f} | budget sized: ${plan.budget_usd:,.2f}",
        f"- trades: {plan.n_trades} | turnover (gas exposure proxy): ${plan.turnover_usd:,.2f} | "
        f"min-trade floor: ${MIN_REBALANCE_USD:,.0f}",
        "- SELL/exits fire on degraded or alerted pools regardless of size; tiny moves HOLD.",
        "- Agent plans; you sign every transaction. Not financial advice.",
        "",
        "| action | $ now | $ target | delta | net APY% | chain | project | symbol | reason |",
        "| --- | ---: | ---: | ---: | ---: | --- | --- | --- | --- |",
    ]
    for a in plan.actions:
        lines.append(
            f"| {a.action} | {a.current_usd:,.2f} | {a.target_usd:,.2f} | {a.delta_usd:+,.2f} | "
            f"{a.net_apy:.2f} | {a.chain} | {a.project} | {a.symbol} | {a.reason} |"
        )
    return "\n".join(lines) + "\n"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--holdings", default="data/holdings.json")
    parser.add_argument("--cache-dir", default="data/cache/yields")
    parser.add_argument("--alerts", help="alerts.json; critical-alert pools are force-exited.")
    parser.add_argument(
        "--budget",
        type=float,
        default=None,
        help="Total USD to size the target to. Default: sum of current holdings.",
    )
    parser.add_argument("--reports-dir", default="reports")
    parser.add_argument(
        "--prices",
        action="store_true",
        help="Apply depeg risk from the cached free token-price snapshot (coins.llama).",
    )
    parser.add_argument(
        "--protocols",
        action="store_true",
        help="Apply protocol risk (audits/age) from the cached DefiLlama protocols snapshot.",
    )
    parser.add_argument("--prices-dir", default="data/cache/prices")
    parser.add_argument("--protocols-dir", default="data/cache/protocols")
    args = parser.parse_args(argv)

    repo_root = Path(__file__).resolve().parents[1]
    holdings = _load_holdings(repo_root / args.holdings)
    holdings_total = sum(max(0.0, float(h.get("usd", 0.0))) for h in holdings)
    price_map = px.load_snapshot(repo_root / args.prices_dir).prices if args.prices else None
    proto_index = None
    proto_now = None
    if args.protocols:
        psnap = pr.load_snapshot(repo_root / args.protocols_dir)
        proto_index, proto_now = psnap.by_slug, psnap.fetched_at_epoch
    ranked = rank_pools(
        list(load_snapshot(repo_root / args.cache_dir).pools),
        prices=price_map,
        protocols=proto_index,
        now_ts=proto_now,
    )
    forced = _load_forced_exits(repo_root / args.alerts) if args.alerts else set()

    plan = build_rebalance(holdings, ranked, budget_usd=args.budget, force_exit=forced)

    payload: dict[str, Any] = {
        "holdings_total_usd": round(holdings_total, 2),
        "budget_usd": plan.budget_usd,
        "n_trades": plan.n_trades,
        "turnover_usd": plan.turnover_usd,
        "forced_exits": sorted(forced),
        "actions": [
            {
                "action": a.action,
                "pool_id": a.pool_id,
                "chain": a.chain,
                "project": a.project,
                "symbol": a.symbol,
                "current_usd": a.current_usd,
                "target_usd": a.target_usd,
                "delta_usd": a.delta_usd,
                "net_apy_pct": round(a.net_apy, 3),
                "reason": a.reason,
            }
            for a in plan.actions
        ],
        "disclaimer": "Churn-aware diff of holdings vs a sized target. Agent plans; user signs.",
    }
    reports = repo_root / args.reports_dir
    reports.mkdir(parents=True, exist_ok=True)
    (reports / "rebalance_plan.json").write_text(
        json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    (reports / "rebalance_plan.md").write_text(_md(plan, holdings_total), encoding="utf-8")
    print(
        f"holdings={holdings_total:.2f} budget={plan.budget_usd:.2f} trades={plan.n_trades} "
        f"turnover={plan.turnover_usd:.2f} forced_exits={len(forced)}"
    )
    print(f"wrote={reports / 'rebalance_plan.json'}")
    print(f"wrote={reports / 'rebalance_plan.md'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

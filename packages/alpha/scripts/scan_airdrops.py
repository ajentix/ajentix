#!/usr/bin/env python3
"""Rank airdrop / points campaigns by risk-adjusted, capital-efficient EV (satellite module).

Reads YOUR modeled campaign inputs (capital, lock, modeled airdrop value, probability, costs,
confidence) and ranks them by annualized EV per dollar, after haircutting for probability +
confidence and subtracting the safe baseline yield you forgo by locking capital. The baseline yield
is taken from the cached DefiLlama yields snapshot (best CORE net APY) unless you pass
--baseline-apy.
There is no free live airdrop feed; every number here is a modeled input, not a scraped fact. The
agent models the EV; the user does the farming and signs every transaction. Not financial advice.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from ajentix_alpha.airdrops.model import ScoredCampaign, rank_campaigns  # noqa: E402
from ajentix_alpha.yields.client import load_snapshot  # noqa: E402
from ajentix_alpha.yields.model import rank_pools  # noqa: E402


def _baseline_from_snapshot(cache_dir: Path) -> float | None:
    """Best CORE net APY from the cached yields snapshot (fallback: best overall, else None)."""
    try:
        snap = load_snapshot(cache_dir)
    except (FileNotFoundError, ValueError):
        return None
    ranked = rank_pools(list(snap.pools))
    core = [s for s in ranked if s.tier == "core"]
    best = core[0] if core else (ranked[0] if ranked else None)
    return best.net_apy if best is not None else None


def _load_campaigns(path: Path) -> list[dict[str, Any]]:
    data = json.loads(path.read_text(encoding="utf-8"))
    rows = data.get("campaigns") if isinstance(data, dict) else data
    if not isinstance(rows, list):
        raise ValueError(f"{path}: expected a list of campaigns or {{'campaigns': [...]}}")
    return [r for r in rows if isinstance(r, dict)]


def _md(scored: list[ScoredCampaign], baseline_apy: float, source: str) -> str:
    lines = [
        "# Airdrop / points EV sheet (risk-adjusted, capital-efficient)",
        "",
        f"- safe baseline yield (opportunity cost): {baseline_apy:.2f}% APY ({source})",
        "- net EV is *in excess of* parking the capital in the safe baseline; NEGATIVE_EV means "
        "the lock-up is not worth it vs the CORE stablecoin yield.",
        "- ranking key: annualized EV per dollar. Every input is YOUR modeled estimate, not a "
        "scraped fact. Agent models; you farm and sign. Not financial advice.",
        "",
        "| ann.EV% | net EV $ | EV/$ | capital $ | lock d | p | conf | chain | campaign | flags |",
        "| ---: | ---: | ---: | ---: | ---: | ---: | --- | --- | --- | --- |",
    ]
    for s in scored:
        c = s.campaign
        lines.append(
            f"| {s.annualized_ev_pct:.1f} | {s.net_ev_usd:,.2f} | {s.ev_per_dollar:.3f} | "
            f"{c.capital_usd:,.0f} | {c.lock_days} | {c.probability:.2f} | {c.confidence} | "
            f"{c.chain} | {c.name} | {', '.join(s.flags) or '-'} |"
        )
    return "\n".join(lines) + "\n"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--campaigns", default="data/airdrops/campaigns.json")
    parser.add_argument("--cache-dir", default="data/cache/yields")
    parser.add_argument(
        "--baseline-apy",
        type=float,
        default=None,
        help="Opportunity-cost APY percent. Default: best CORE net APY from the snapshot.",
    )
    parser.add_argument("--reports-dir", default="reports")
    args = parser.parse_args(argv)

    repo_root = Path(__file__).resolve().parents[1]
    if args.baseline_apy is not None:
        baseline_apy, source = float(args.baseline_apy), "override"
    else:
        derived = _baseline_from_snapshot(repo_root / args.cache_dir)
        if derived is None:
            raise SystemExit(
                "no cached yields snapshot to derive a baseline APY; run "
                "`scan_yields.py --fetch` first or pass --baseline-apy."
            )
        baseline_apy, source = derived, "best CORE net APY from cached snapshot"

    rows = _load_campaigns(repo_root / args.campaigns)
    scored = rank_campaigns(rows, baseline_apy_pct=baseline_apy)

    payload: dict[str, Any] = {
        "baseline_apy_pct": round(baseline_apy, 3),
        "baseline_source": source,
        "campaign_count": len(scored),
        "positive_ev_count": sum(1 for s in scored if s.net_ev_usd > 0),
        "campaigns": [
            {
                "name": s.campaign.name,
                "chain": s.campaign.chain,
                "capital_usd": s.campaign.capital_usd,
                "lock_days": s.campaign.lock_days,
                "deadline_days": s.campaign.deadline_days,
                "probability": s.campaign.probability,
                "confidence": s.campaign.confidence,
                "confidence_haircut": s.confidence_haircut,
                "expected_gross_usd": round(s.expected_gross_usd, 2),
                "opportunity_cost_usd": round(s.opportunity_cost_usd, 2),
                "net_ev_usd": round(s.net_ev_usd, 2),
                "ev_per_dollar": round(s.ev_per_dollar, 4),
                "annualized_ev_pct": round(s.annualized_ev_pct, 3),
                "flags": list(s.flags),
            }
            for s in scored
        ],
        "disclaimer": (
            "Every input is a user-modeled estimate, not scraped airdrop data. Net EV is net of "
            "the safe-yield opportunity cost. Agent models; user farms and signs. Not advice."

        ),
    }
    reports = repo_root / args.reports_dir
    reports.mkdir(parents=True, exist_ok=True)
    (reports / "airdrop_ev.json").write_text(
        json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    (reports / "airdrop_ev.md").write_text(
        _md(scored, baseline_apy, source), encoding="utf-8"
    )
    print(
        f"baseline_apy={baseline_apy:.2f} campaigns={len(scored)} "
        f"positive_ev={payload['positive_ev_count']}"
    )
    print(f"wrote={reports / 'airdrop_ev.json'}")
    print(f"wrote={reports / 'airdrop_ev.md'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

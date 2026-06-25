#!/usr/bin/env python3
"""Summarize a points-farming log into accrual velocity and capital efficiency.

Reads YOUR dated point-balance log (`data/airdrops/points_log.json`) and reports, per campaign,
points/day, points per dollar-day, and — when you supply a modeled value-per-point — an implied
APY-equivalent so a farm can be weighed against the safe yield it ties capital up against. Every
value-per-point is a user-modeled estimate, not a quoted price. The agent reports; you farm.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from ajentix_alpha.airdrops.points import CampaignPoints, summarize  # noqa: E402


def _load(path: Path) -> list[dict[str, Any]]:
    data = json.loads(path.read_text(encoding="utf-8"))
    rows = data.get("entries") if isinstance(data, dict) else data
    if not isinstance(rows, list):
        raise ValueError(f"{path}: expected a list of entries or {{'entries': [...]}}")
    return [r for r in rows if isinstance(r, dict)]


def _fmt(x: float | None, spec: str) -> str:
    return format(x, spec) if x is not None else "-"


def _md(rows: list[CampaignPoints]) -> str:
    lines = [
        "# Points-farming status (accrual + capital efficiency)",
        "",
        "- implied APY = modeled point value annualized over capital-days; compare it against the "
        "CORE stablecoin yield you forgo. Every value-per-point is YOUR estimate. Not advice.",
        "",
        "| implied APY% | pts/day | pts/$-day | points | $ value | capital $ | days | "
        "campaign | flags |",
        "| ---: | ---: | ---: | ---: | ---: | ---: | ---: | --- | --- |",
    ]
    for s in rows:
        lines.append(
            f"| {_fmt(s.implied_apy_pct, '.1f')} | {s.points_per_day:,.1f} | "
            f"{s.points_per_dollar_day:.3f} | {s.latest_points:,.0f} | "
            f"{_fmt(s.modeled_value_usd, ',.2f')} | {s.latest_capital_usd:,.0f} | "
            f"{s.days_active} | {s.campaign} | {', '.join(s.flags) or '-'} |"
        )
    return "\n".join(lines) + "\n"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--points-log", default="data/airdrops/points_log.json")
    parser.add_argument("--reports-dir", default="reports")
    args = parser.parse_args(argv)

    repo_root = Path(__file__).resolve().parents[1]
    rows = summarize(_load(repo_root / args.points_log))

    payload: dict[str, Any] = {
        "campaign_count": len(rows),
        "campaigns": [
            {
                "campaign": s.campaign,
                "entries": s.entries,
                "first_date": s.first_date,
                "last_date": s.last_date,
                "days_active": s.days_active,
                "latest_points": s.latest_points,
                "points_gained": s.points_gained,
                "points_per_day": round(s.points_per_day, 4),
                "latest_capital_usd": s.latest_capital_usd,
                "capital_days": round(s.capital_days, 2),
                "points_per_dollar_day": round(s.points_per_dollar_day, 6),
                "value_per_point": s.value_per_point,
                "modeled_value_usd": (
                    round(s.modeled_value_usd, 2) if s.modeled_value_usd is not None else None
                ),
                "implied_apy_pct": (
                    round(s.implied_apy_pct, 3) if s.implied_apy_pct is not None else None
                ),
                "flags": list(s.flags),
            }
            for s in rows
        ],
        "disclaimer": "User-logged points; modeled value-per-point, not a quote. Not advice.",
    }
    reports = repo_root / args.reports_dir
    reports.mkdir(parents=True, exist_ok=True)
    (reports / "points_status.json").write_text(
        json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    (reports / "points_status.md").write_text(_md(rows), encoding="utf-8")
    print(f"campaigns={len(rows)}")
    print(f"wrote={reports / 'points_status.json'}")
    print(f"wrote={reports / 'points_status.md'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

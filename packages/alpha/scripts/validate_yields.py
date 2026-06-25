#!/usr/bin/env python3
"""Calibrate the conservative net-APY model against what pools actually did between two snapshots.

NOT a backtest and NOT a performance claim — a feedback loop. Diffs an earlier archived snapshot
against a later one (default: the two most recent history points from `scan_yields.py --fetch`) and
reports conservatism rate, signed-error distribution, SPIKE reversion, CORE-vs-SATELLITE TVL
persistence, and survival. Over a short retained window the signal is weak; treat it as directional.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from ajentix_alpha.yields.client import list_history, load_snapshot  # noqa: E402
from ajentix_alpha.yields.validate import CalibrationReport, calibrate  # noqa: E402


def _resolve_pair(args: argparse.Namespace, repo_root: Path) -> tuple[Path, Path]:
    if args.baseline and args.current:
        return repo_root / args.baseline, repo_root / args.current
    history = repo_root / args.history_dir
    points = list_history(history.parent if history.name == "history" else history)
    if len(points) < 2:
        raise SystemExit(
            f"need >= 2 archived snapshots in {history} to calibrate (found {len(points)}); "
            "run `scan_yields.py --fetch` at two points in time, or pass --baseline/--current."
        )
    return points[-2], points[-1]


def _md(rep: CalibrationReport, base: str, cur: str) -> str:
    lines = [
        "# Model calibration (net-APY conservatism over time)",
        "",
        f"- baseline: {base}  ->  current: {cur}",
        f"- baseline-ranked pools: {rep.baseline_ranked} | matched later: {rep.matched} | "
        f"survival {rep.survival_rate * 100:.1f}%",
        f"- conservatism rate (realized >= quoted net APY): **{rep.conservatism_rate * 100:.1f}%** "
        f"(higher = haircuts under-promised as designed)",
        f"- signed error (realized - quoted), median {rep.median_signed_error:+.2f} pp / "
        f"mean {rep.mean_signed_error:+.2f} pp",
        f"- SPIKE pools: {rep.spike_count} | reverted lower: "
        f"{rep.spike_reversion_rate * 100:.1f}%",
        f"- TVL median change: CORE {rep.core_tvl_median_change_pct:+.1f}% vs "
        f"SATELLITE {rep.satellite_tvl_median_change_pct:+.1f}%",
        "- NOT a backtest / not a performance claim; short windows are weak signal. Not advice.",
        "",
        "## Worst over-predictions (model was too rosy: realized fell most below quoted)",
        "",
        "| signed err pp | quoted net APY | realized APY | tier | project | symbol | flags |",
        "| ---: | ---: | ---: | --- | --- | --- | --- |",
    ]
    for o in rep.worst_overpredictions:
        lines.append(
            f"| {o.signed_error:+.2f} | {o.predicted_net_apy:.2f} | {o.realized_apy:.2f} | "
            f"{o.tier} | {o.project} | {o.symbol} | {', '.join(o.flags) or '-'} |"
        )
    return "\n".join(lines) + "\n"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--cache-dir", default="data/cache/yields")
    parser.add_argument("--history-dir", default="data/cache/yields/history")
    parser.add_argument("--baseline", help="Explicit baseline snapshot dir.")
    parser.add_argument("--current", help="Explicit current snapshot dir.")
    parser.add_argument("--reports-dir", default="reports")
    args = parser.parse_args(argv)

    repo_root = Path(__file__).resolve().parents[1]
    base_dir, cur_dir = _resolve_pair(args, repo_root)
    base_snap = load_snapshot(base_dir)
    cur_snap = load_snapshot(cur_dir)
    rep = calibrate(list(base_snap.pools), list(cur_snap.pools))

    payload: dict[str, Any] = {
        "baseline": {"dir": str(base_dir), "at": base_snap.fetched_at_utc, "sha": base_snap.sha256},
        "current": {"dir": str(cur_dir), "at": cur_snap.fetched_at_utc, "sha": cur_snap.sha256},
        "baseline_ranked": rep.baseline_ranked,
        "matched": rep.matched,
        "survival_rate": round(rep.survival_rate, 4),
        "conservatism_rate": round(rep.conservatism_rate, 4),
        "median_signed_error_pp": round(rep.median_signed_error, 4),
        "mean_signed_error_pp": round(rep.mean_signed_error, 4),
        "spike_count": rep.spike_count,
        "spike_reversion_rate": round(rep.spike_reversion_rate, 4),
        "core_tvl_median_change_pct": round(rep.core_tvl_median_change_pct, 3),
        "satellite_tvl_median_change_pct": round(rep.satellite_tvl_median_change_pct, 3),
        "worst_overpredictions": [
            {
                "pool_id": o.pool_id,
                "project": o.project,
                "symbol": o.symbol,
                "tier": o.tier,
                "predicted_net_apy_pct": round(o.predicted_net_apy, 3),
                "realized_apy_pct": round(o.realized_apy, 3),
                "signed_error_pp": round(o.signed_error, 3),
                "flags": list(o.flags),
            }
            for o in rep.worst_overpredictions
        ],
        "disclaimer": "Calibration feedback, not a backtest or performance claim. Not advice.",
    }
    reports = repo_root / args.reports_dir
    reports.mkdir(parents=True, exist_ok=True)
    (reports / "calibration.json").write_text(
        json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    (reports / "calibration.md").write_text(
        _md(rep, base_snap.fetched_at_utc, cur_snap.fetched_at_utc), encoding="utf-8"
    )
    print(
        f"matched={rep.matched}/{rep.baseline_ranked} "
        f"conservatism={rep.conservatism_rate * 100:.1f}% "
        f"median_err={rep.median_signed_error:+.2f}pp "
        f"spike_revert={rep.spike_reversion_rate * 100:.0f}%"
    )
    print(f"wrote={reports / 'calibration.json'}")
    print(f"wrote={reports / 'calibration.md'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

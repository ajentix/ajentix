#!/usr/bin/env python3
"""Run the non-authorizing USD-consistent VRP skew measurement from the local cache (network-free).

Measures the clean fold-level economics of the ETH OTM put-skew credit-spread edge that the frozen
ETH-credit-vs-USD-width unit bug left unmeasured (every official fold selected zero structures). It
reuses the frozen search space, leg selection, and entry bar on USD-projected snapshots, then runs
an enter-all settlement backtest per held-out fold with a documented effective-spread haircut. It
NEVER authorizes capital: reconstructed source quality precludes a GO regardless of the numbers.

Reads only the reconstructed-chain cache (the 1.2GB raw-trade cache is not needed: the effective
spread enters as a documented haircut from the full real-data run).
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

REPO_ROOT_DEFAULT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT_DEFAULT))
sys.path.insert(0, str(REPO_ROOT_DEFAULT / "src"))

from ajentix_quant.research.vrp_usd_eval import (  # noqa: E402
    DEFAULT_SCENARIO_ID,
    REPORT_STEM,
    run_usd_eval,
)


def _resolve(repo_root: Path, path: str | Path) -> Path:
    p = Path(path)
    return p if p.is_absolute() else repo_root / p


def _markdown(payload: dict[str, Any]) -> str:
    econ = payload["economics_summary"]
    agg = econ["aggregate"]
    inp = payload["inputs"]
    lines = [
        "# VRP skew — USD-consistent measurement (non-authorizing)",
        "",
        "- purpose: measure the clean fold economics the frozen ETH/USD unit bug left unmeasured",
        f"- authorizing: **false** | capital_go_allowed: **false** | label: "
        f"{payload['non_authorizing_label']}",
        f"- coverage: {inp['coverage_start']} -> {inp['coverage_end']} | "
        f"snapshots {inp['snapshot_count']} (USD-projected {inp['usd_projected_snapshot_count']})",
        f"- equity ${inp['equity_usd']:,.0f} | folds {inp['fold_count']} | spread haircut "
        f"p50 ${agg['effective_spread_p50_usd']}/p75 ${agg['effective_spread_p75_usd']}",
        "",
        "## Measurement signal",
        "",
        f"- **{econ['measurement_signal']}**",
        "",
        "## Aggregate economics (enter-all, per-fold held-out)",
        "",
        f"- folds: {agg['fold_count']} ({agg['folds_with_entries']} with entries) | "
        f"total entries: {agg['total_entries']:,}",
        f"- gross PnL: ${agg['total_gross_pnl_usd']:,.2f} | net p50: "
        f"${agg['total_net_p50_pnl_usd']:,.2f} | net p75: ${agg['total_net_p75_pnl_usd']:,.2f}",
        f"- fold Sharpe (return-on-risk) gross: {agg['fold_sharpe_return_on_risk_gross']:.2f} | "
        f"net p50: {agg['fold_sharpe_return_on_risk_net_p50']:.2f} "
        f"(bar {agg['sharpe_bar']}, {agg['periods_per_year']:.1f} periods/yr)",
        "",
        f"> {econ['weak_signal_caveat']}",
        "",
        "## Per-fold",
        "",
        "| fold | entries | gross $ | net p50 $ | net p75 $ | ror gross | ror net p50 | mean c/w |",
        "| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: |",
    ]
    for row in econ["per_fold"]:
        lines.append(
            f"| {row['fold_id']} | {row['entries']:,} | {row['gross_pnl_usd']:,.2f} | "
            f"{row['net_p50_pnl_usd']:,.2f} | {row['net_p75_pnl_usd']:,.2f} | "
            f"{row['return_on_risk_gross']:.4f} | {row['return_on_risk_net_p50']:.4f} | "
            f"{row['mean_credit_to_width']} |"
        )
    lines += ["", f"_method: {payload['method_note']}_", "", f"_{payload['disclaimer']}_", ""]
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo-root", default=str(REPO_ROOT_DEFAULT))
    parser.add_argument("--reconstructed-cache-root", default="data/cache/full_recon")
    parser.add_argument("--raw-source-root", default="data/cache/full_combined")
    parser.add_argument("--scenario-id", default=DEFAULT_SCENARIO_ID)
    parser.add_argument("--symbol", default="ETH")
    parser.add_argument("--reports-dir", default="reports")
    args = parser.parse_args(argv)

    repo_root = Path(args.repo_root).resolve()
    payload = run_usd_eval(
        repo_root=str(repo_root),
        raw_source_root=str(_resolve(repo_root, args.raw_source_root)),
        reconstructed_cache_root=str(_resolve(repo_root, args.reconstructed_cache_root)),
        scenario_id=args.scenario_id,
        symbol=args.symbol,
    )

    reports = _resolve(repo_root, args.reports_dir)
    reports.mkdir(parents=True, exist_ok=True)
    json_path = reports / f"{REPORT_STEM}.json"
    md_path = reports / f"{REPORT_STEM}.md"
    json_path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    md_path.write_text(_markdown(payload), encoding="utf-8")

    agg = payload["economics_summary"]["aggregate"]
    print(
        f"signal={payload['economics_summary']['measurement_signal']} "
        f"entries={agg['total_entries']} gross=${agg['total_gross_pnl_usd']:.2f} "
        f"net_p50=${agg['total_net_p50_pnl_usd']:.2f} "
        f"sharpe_net={agg['fold_sharpe_return_on_risk_net_p50']:.2f}"
    )
    print(f"wrote={json_path}")
    print(f"wrote={md_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

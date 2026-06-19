#!/usr/bin/env python3
"""Run the strategy-v2 G002 breakeven analysis on the committed real cache."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
from pathlib import Path
from typing import Any

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from ajentix_quant.backtest.breakeven import (  # noqa: E402
    BRANCH_A1,
    BRANCH_A2,
    BRANCH_INCONCLUSIVE,
    analyze_symbol,
)
from ajentix_quant.config import Settings  # noqa: E402
from ajentix_quant.data.cache import load_dataset  # noqa: E402
from ajentix_quant.research.preregistration import (  # noqa: E402
    PLAN_DECISION_HORIZONS,
    PLAN_EQUITY_GRID,
    PLAN_PRIMARY_EQUITY,
    load_preregistration,
    verify_preregistration,
)
from ajentix_quant.risk.margin import (  # noqa: E402
    VenueMarginModel,
    bybit_btc_eth_instruments,
    bybit_btc_eth_risk_limits,
)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Run preregistered strategy-v2 breakeven analysis."
    )
    parser.add_argument(
        "--repo-root",
        default=str(Path(__file__).resolve().parents[1]),
        help="Repository root (defaults to the checkout containing this script).",
    )
    parser.add_argument(
        "--preregistration",
        default=None,
        help="Path to docs/preregistration/stratv2-*.json. Defaults to the single artifact.",
    )
    parser.add_argument(
        "--reports-dir",
        default="reports",
        help="Output directory for breakeven reports relative to repo root.",
    )
    args = parser.parse_args(argv)

    repo_root = Path(args.repo_root).resolve()
    prereg_path = _resolve_preregistration(repo_root, args.preregistration)
    artifact = load_preregistration(prereg_path)
    verify = verify_preregistration(artifact, repo_root)
    prereg_sha = _sha256_file(prereg_path)
    if not verify.valid:
        print(f"run_status={verify.run_status}")
        print(f"run_id={artifact.get('run_id')}")
        print(f"preregistration={prereg_path}")
        print(f"preregistration_sha256={prereg_sha}")
        print("decision=REFUSED_INVALID_PREREGISTRATION")
        for mismatch in verify.mismatches:
            print(f"mismatch={mismatch}", file=sys.stderr)
        return 1

    settings = Settings()
    cache_root = str(artifact.get("cache_root", "data/cache/bybit"))
    scenarios = dict(artifact["plan"]["scenarios"])
    instruments = bybit_btc_eth_instruments()
    risk_limits = bybit_btc_eth_risk_limits()

    results = []
    for symbol, scenario_id in scenarios.items():
        dataset = load_dataset(repo_root / cache_root, scenario_id)
        margin_model = VenueMarginModel(instruments[symbol], risk_limits[symbol])
        result = analyze_symbol(
            dataset,
            symbol=symbol,
            margin_model=margin_model,
            settings=settings,
            decision_horizons=PLAN_DECISION_HORIZONS,
            equity_grid=PLAN_EQUITY_GRID,
            primary_equity_usd=PLAN_PRIMARY_EQUITY,
        )
        results.append((scenario_id, result))

    branch_summary = _branch_summary(results)
    reports_dir = repo_root / args.reports_dir
    reports_dir.mkdir(parents=True, exist_ok=True)
    report_paths: list[Path] = []
    for scenario_id, result in results:
        stem = _symbol_stem(result.symbol)
        json_path = reports_dir / f"breakeven_{stem}_v2.json"
        md_path = reports_dir / f"breakeven_{stem}_v2.md"
        payload = _report_payload(
            artifact=artifact,
            preregistration_path=prereg_path,
            preregistration_sha256=prereg_sha,
            scenario_id=scenario_id,
            result=result,
            branch_summary=branch_summary,
        )
        json_path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        md_path.write_text(_markdown_report(payload), encoding="utf-8")
        report_paths.extend([json_path, md_path])

    print(f"run_status={verify.run_status}")
    print(f"run_id={artifact.get('run_id')}")
    print(f"preregistration_sha256={prereg_sha}")
    print(_summary_table(results))
    print(
        "branch_decision="
        f"A1:{','.join(branch_summary['a1']) or '-'} "
        f"A2:{','.join(branch_summary['a2']) or '-'} "
        f"INCONCLUSIVE:{','.join(branch_summary['inconclusive']) or '-'}"
    )
    for path in report_paths:
        print(f"wrote={path.relative_to(repo_root)}")
    return 0


def _resolve_preregistration(repo_root: Path, explicit: str | None) -> Path:
    if explicit:
        path = Path(explicit)
        return path if path.is_absolute() else repo_root / path
    prereg_dir = repo_root / "docs" / "preregistration"
    artifacts = sorted(prereg_dir.glob("stratv2-*.json")) if prereg_dir.is_dir() else []
    if len(artifacts) != 1:
        raise SystemExit(
            "expected exactly one docs/preregistration/stratv2-*.json artifact, "
            f"found {len(artifacts)}"
        )
    return artifacts[0]


def _report_payload(
    *,
    artifact: dict[str, Any],
    preregistration_path: Path,
    preregistration_sha256: str,
    scenario_id: str,
    result: Any,
    branch_summary: dict[str, Any],
) -> dict[str, Any]:
    return {
        "run_status": "valid",
        "run_id": artifact.get("run_id"),
        "content_hash": artifact.get("content_hash"),
        "preregistration_path": preregistration_path.as_posix(),
        "preregistration_sha256": preregistration_sha256,
        "source_hashes": artifact.get("source_hashes", {}),
        "settings_snapshot": artifact.get("settings_snapshot", {}),
        "plan": artifact.get("plan", {}),
        "plan_sha256": _canonical_sha256(artifact.get("plan", {})),
        "cache_root": artifact.get("cache_root"),
        "cache_manifest_sha256": artifact.get("cache_manifest_sha256", {}),
        "scenario_id": scenario_id,
        "cost_modes": {
            "primary": "taker-primary authorizing",
            "maker_sensitivity": "non-authorizing; maker_can_authorize=false",
        },
        "equity_grid": list(PLAN_EQUITY_GRID),
        "decision_horizons": list(PLAN_DECISION_HORIZONS),
        "primary_equity_usd": PLAN_PRIMARY_EQUITY,
        "breakeven": result.as_dict(include_windows=False),
        "branch_summary": branch_summary,
    }


def _markdown_report(payload: dict[str, Any]) -> str:
    result = payload["breakeven"]
    metrics = result["metrics"]
    by_key = {(m["horizon"], m["equity_usd"]): m for m in metrics}
    lines = [
        f"# Breakeven report: {result['symbol']}",
        "",
        f"- run_status: {payload['run_status']}",
        f"- run_id: {payload['run_id']}",
        f"- preregistration_sha256: {payload['preregistration_sha256']}",
        f"- content_hash: {payload['content_hash']}",
        f"- scenario_id: {payload['scenario_id']}",
        f"- cost_mode_primary: {payload['cost_modes']['primary']}",
        f"- maker_sensitivity: {payload['cost_modes']['maker_sensitivity']}",
        f"- A1 decision: {result['a1_decision']}",
        f"- branch decision: {result['branch_decision']}",
        f"- reason_codes: {', '.join(result['reason_codes']) or '-'}",
        "",
        result["leverage_note"],
        "",
        result["cluster_note"],
        "",
        "| Horizon | Equity | Valid | Qualifying | Qualifying % | Clusters | "
        "Max cluster | Top-3 clusters | Clears bar |",
        "|---:|---:|---:|---:|---:|---:|---:|---:|:---:|",
    ]
    for horizon in payload["decision_horizons"]:
        for equity in payload["equity_grid"]:
            m = by_key[(horizon, float(equity))]
            c = m["cluster_metrics"]
            lines.append(
                f"| {horizon} | {equity:.0f} | {m['valid_windows']} | "
                f"{m['qualifying_windows']} | {m['qualifying_pct']:.2%} | "
                f"{c['cluster_count']} | {c['max_single_cluster_share']:.2%} | "
                f"{c['top3_cluster_share']:.2%} | {m['clears_horizon_bar']} |"
            )
    lines.extend(
        [
            "",
            "## Branch summary",
            "",
            f"- A1: {', '.join(payload['branch_summary']['a1']) or '-'}",
            f"- A2: {', '.join(payload['branch_summary']['a2']) or '-'}",
            f"- INCONCLUSIVE: {', '.join(payload['branch_summary']['inconclusive']) or '-'}",
            "",
        ]
    )
    return "\n".join(lines)


def _summary_table(results: list[tuple[str, Any]]) -> str:
    lines = [
        "symbol | h21_qual_pct_1000 | h21_clusters | h42_qual_pct_1000 | "
        "h42_clusters | A1 | branch | reasons",
        "--- | ---: | ---: | ---: | ---: | --- | --- | ---",
    ]
    for _, result in results:
        h21 = result.metric_for(horizon=21, equity_usd=1000.0)
        h42 = result.metric_for(horizon=42, equity_usd=1000.0)
        lines.append(
            f"{result.symbol} | {h21.qualifying_pct:.2%} | "
            f"{h21.cluster_metrics.cluster_count} | {h42.qualifying_pct:.2%} | "
            f"{h42.cluster_metrics.cluster_count} | {result.a1_decision} | "
            f"{result.branch_decision} | {','.join(result.reason_codes) or '-'}"
        )
    return "\n".join(lines)


def _branch_summary(results: list[tuple[str, Any]]) -> dict[str, Any]:
    summary = {"a1": [], "a2": [], "inconclusive": [], "by_symbol": {}}
    for _, result in results:
        if result.branch_decision == BRANCH_A1:
            summary["a1"].append(result.symbol)
        elif result.branch_decision == BRANCH_A2:
            summary["a2"].append(result.symbol)
        elif result.branch_decision == BRANCH_INCONCLUSIVE:
            summary["inconclusive"].append(result.symbol)
        summary["by_symbol"][result.symbol] = {
            "a1_decision": result.a1_decision,
            "branch_decision": result.branch_decision,
            "reason_codes": list(result.reason_codes),
        }
    return summary


def _symbol_stem(symbol: str) -> str:
    base = symbol.split("/", 1)[0]
    return base.lower()


def _sha256_file(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _canonical_sha256(obj: Any) -> str:
    return hashlib.sha256(
        json.dumps(obj, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()


if __name__ == "__main__":
    raise SystemExit(main())

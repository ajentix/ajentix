#!/usr/bin/env python3
"""Diff two DefiLlama yield snapshots and report degradation on the pools you hold.

Compares a baseline snapshot against a later one (by default the two most recent archived history
points written by `scan_yields.py --fetch`) and writes an alert sheet: APY collapse, TVL drain,
reward cut, a pool vanishing, a new risk flag, or a CORE->SATELLITE downgrade. Point `--watch` at an
allocation_plan.json to focus only on the positions you actually entered. The agent raises the flag;
the user decides whether to exit. Not financial advice.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from ajentix_alpha.yields.client import list_history, load_snapshot  # noqa: E402
from ajentix_alpha.yields.monitor import MonitorReport, diff_snapshots  # noqa: E402
from ajentix_alpha.yields.notify import alert_payload, try_post  # noqa: E402


def _load_watch(path: Path) -> list[str]:
    """Read pool_ids to watch from an allocation_plan.json (or a bare JSON list of ids)."""
    data = json.loads(path.read_text(encoding="utf-8"))
    if isinstance(data, dict) and isinstance(data.get("positions"), list):
        return [
            str(p["pool_id"])
            for p in data["positions"]
            if isinstance(p, dict) and "pool_id" in p
        ]
    if isinstance(data, list):
        return [str(x) for x in data]
    raise ValueError(f"{path}: expected an allocation plan or a JSON list of pool ids")


def _md(report: MonitorReport, base: str, cur: str, watch_n: int | None) -> str:
    scope = f"{watch_n} watched positions" if watch_n is not None else f"{report.watched} pools"
    lines = [
        "# Yield position alerts",
        "",
        f"- baseline: {base}",
        f"- current:  {cur}",
        f"- scope: {scope} | critical {report.critical} / warning {report.warning} / "
        f"info {report.info}",
        "- Agent raises the flag; you decide whether to exit. Not financial advice.",
        "",
    ]
    if not report.alerts:
        lines.append("No alerts: nothing tracked got materially worse.")
        return "\n".join(lines) + "\n"
    lines += [
        "| severity | kind | chain | project | symbol | detail |",
        "| --- | --- | --- | --- | --- | --- |",
    ]
    for a in report.alerts:
        lines.append(
            f"| {a.severity} | {a.kind} | {a.chain} | {a.project} | {a.symbol} | {a.detail} |"
        )
    return "\n".join(lines) + "\n"


def _resolve_pair(args: argparse.Namespace, repo_root: Path) -> tuple[Path, Path]:
    if args.baseline and args.current:
        return repo_root / args.baseline, repo_root / args.current
    history = repo_root / args.history_dir
    points = list_history(history.parent if history.name == "history" else history)
    if len(points) < 2:
        raise SystemExit(
            f"need >= 2 archived snapshots in {history} to diff "
            f"(found {len(points)}); run `scan_yields.py --fetch` at two points in time, "
            "or pass --baseline DIR --current DIR explicitly."
        )
    return points[-2], points[-1]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--cache-dir", default="data/cache/yields")
    parser.add_argument("--history-dir", default="data/cache/yields/history")
    parser.add_argument("--baseline", help="Explicit baseline snapshot dir (overrides history).")
    parser.add_argument("--current", help="Explicit current snapshot dir (overrides history).")
    parser.add_argument("--watch", help="allocation_plan.json (or JSON list) of pool ids to watch.")
    parser.add_argument(
        "--include-new", action="store_true", help="Also surface brand-new high-APY pools."
    )
    parser.add_argument(
        "--webhook",
        help="POST a JSON alert payload here (default: AJENTIX_WEBHOOK_URL env var).",
    )
    parser.add_argument(
        "--notify-min",
        choices=("critical", "warning", "info"),
        default="critical",
        help="Only POST when an alert at or above this severity is present (default: critical).",
    )
    parser.add_argument("--reports-dir", default="reports")
    args = parser.parse_args(argv)

    repo_root = Path(__file__).resolve().parents[1]
    base_dir, cur_dir = _resolve_pair(args, repo_root)
    base_snap = load_snapshot(base_dir)
    cur_snap = load_snapshot(cur_dir)

    watch: list[str] | None = None
    if args.watch:
        watch = _load_watch(repo_root / args.watch)

    report = diff_snapshots(
        list(base_snap.pools),
        list(cur_snap.pools),
        watch=watch,
        include_new=args.include_new,
    )

    payload: dict[str, Any] = {
        "baseline": {
            "dir": str(base_dir),
            "sha256": base_snap.sha256,
            "at": base_snap.fetched_at_utc,
        },
        "current": {
            "dir": str(cur_dir),
            "sha256": cur_snap.sha256,
            "at": cur_snap.fetched_at_utc,
        },
        "watched": report.watched,
        "counts": {"critical": report.critical, "warning": report.warning, "info": report.info},
        "alerts": [
            {
                "severity": a.severity,
                "kind": a.kind,
                "chain": a.chain,
                "project": a.project,
                "symbol": a.symbol,
                "detail": a.detail,
                "pool_id": a.pool_id,
            }
            for a in report.alerts
        ],
        "disclaimer": "Alerts grounded only in the free yields feed; no price oracle. Not advice.",
    }
    reports = repo_root / args.reports_dir
    reports.mkdir(parents=True, exist_ok=True)
    (reports / "alerts.json").write_text(
        json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    watch_n = len(watch) if watch else None
    (reports / "alerts.md").write_text(
        _md(report, base_snap.fetched_at_utc, cur_snap.fetched_at_utc, watch_n),
        encoding="utf-8",
    )
    print(
        f"baseline={base_snap.fetched_at_utc} current={cur_snap.fetched_at_utc} "
        f"watched={report.watched} critical={report.critical} warning={report.warning} "
        f"info={report.info}"
    )
    print(f"wrote={reports / 'alerts.json'}")
    print(f"wrote={reports / 'alerts.md'}")

    webhook = args.webhook or os.environ.get("AJENTIX_WEBHOOK_URL")
    threshold = {"critical": report.critical, "warning": report.critical + report.warning,
                 "info": report.critical + report.warning + report.info}[args.notify_min]
    if webhook and threshold > 0:
        ok, detail = try_post(
            webhook,
            alert_payload(
                report, baseline=base_snap.fetched_at_utc, current=cur_snap.fetched_at_utc
            ),
        )
        print(f"webhook={'ok' if ok else 'FAILED'} ({detail})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

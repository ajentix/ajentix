"""Deliver monitor alerts to a generic webhook (Slack / Discord / Telegram-webhook / custom).

Dependency-free: a plain JSON POST over stdlib urllib. The payload carries both a human `text`
summary (what Slack/Discord render) and a structured `counts` + `alerts` block (what a custom
handler parses). The payload builder is pure and testable; only `post_webhook` touches the network.
The URL is a secret — pass it via `--webhook` or the AJENTIX_WEBHOOK_URL env var, never commit it.
"""

from __future__ import annotations

import json
import urllib.error
import urllib.request

from .monitor import MonitorReport

_SEVERITY_EMOJI = {"critical": "🔴", "warning": "🟠", "info": "🔵"}


def summary_text(report: MonitorReport, *, baseline: str, current: str) -> str:
    """One-line human summary of a monitor run."""
    return (
        f"ajentix-alpha alerts: {report.critical} critical / {report.warning} warning / "
        f"{report.info} info across {report.watched} watched ({baseline} -> {current})"
    )


def alert_payload(
    report: MonitorReport, *, baseline: str, current: str, max_items: int = 10
) -> dict[str, object]:
    """Build a generic webhook payload from a monitor report (alerts already severity-sorted)."""
    top = report.alerts[:max_items]
    detail_lines = [
        f"{_SEVERITY_EMOJI.get(a.severity, '•')} [{a.kind}] {a.project} {a.symbol} ({a.chain}): "
        f"{a.detail}"
        for a in top
    ]
    summary = summary_text(report, baseline=baseline, current=current)
    text = summary if not detail_lines else summary + "\n" + "\n".join(detail_lines)
    return {
        "text": text,
        "summary": summary,
        "counts": {
            "critical": report.critical,
            "warning": report.warning,
            "info": report.info,
            "watched": report.watched,
        },
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
            for a in top
        ],
        "truncated": len(report.alerts) > len(top),
    }


def post_webhook(url: str, payload: dict[str, object], *, timeout: int = 15) -> int:
    """POST a JSON payload to a webhook URL. Returns HTTP status; raises on transport failure."""
    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        url,
        data=data,
        headers={"Content-Type": "application/json", "User-Agent": "ajentix-alpha/0.1 (research)"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:  # noqa: S310 - caller-supplied URL
        return int(resp.status)


def try_post(url: str, payload: dict[str, object], *, timeout: int = 15) -> tuple[bool, str]:
    """Best-effort POST: never raises. Returns (ok, detail) so a CLI can report without crashing."""
    try:
        status = post_webhook(url, payload, timeout=timeout)
    except (urllib.error.URLError, TimeoutError, ValueError) as exc:
        return False, str(exc)
    return 200 <= status < 300, f"HTTP {status}"

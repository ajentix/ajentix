"""Render the dashboard summary dict into a self-contained static HTML page.

Pure / deterministic, stdlib-only. No external CSS / JS / CDN: the page works offline and carries no
supply-chain surface. Every dynamic string is HTML-escaped. Built to be published as a free,
auto-updating static site (e.g. GitHub Pages) from the same summary the pipeline already emits.
"""

from __future__ import annotations

import html
from typing import Any

_REPO_URL = "https://github.com/ajentix/ajentix"

_CSS = """
:root { color-scheme: dark; }
* { box-sizing: border-box; }
body {
  margin: 0;
  padding: 0 1rem 4rem;
  font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, Helvetica, Arial, sans-serif;
  font-size: 15px;
  line-height: 1.55;
  color: #e6e9ef;
  background: #0d1117;
}
main { max-width: 960px; margin: 0 auto; }
header { padding: 2.5rem 0 1.25rem; border-bottom: 1px solid #21262d; }
h1 { margin: 0; font-size: 1.7rem; letter-spacing: -0.01em; }
h1 .dim { color: #7d8590; font-weight: 500; }
.tagline { margin: .5rem 0 0; color: #adbac7; max-width: 62ch; }
.meta {
  margin-top: .75rem;
  font-size: .82rem;
  color: #7d8590;
  font-family: ui-monospace, monospace;
}
.disclaimer {
  margin: 1rem 0 0;
  padding: .6rem .85rem;
  border-radius: 8px;
  font-size: .82rem;
  background: #2d2212;
  border: 1px solid #5c3d10;
  color: #e3b341;
}
.cards { display: flex; flex-wrap: wrap; gap: .75rem; margin: 1.5rem 0; }
.card {
  flex: 1 1 130px;
  padding: .85rem 1rem;
  border-radius: 10px;
  background: #161b22;
  border: 1px solid #21262d;
}
.card .k {
  font-size: .72rem;
  text-transform: uppercase;
  letter-spacing: .05em;
  color: #7d8590;
}
.card .v {
  font-size: 1.45rem;
  font-weight: 650;
  margin-top: .15rem;
  font-family: ui-monospace, monospace;
}
section { margin-top: 2.25rem; }
h2 {
  font-size: 1.05rem;
  margin: 0 0 .6rem;
  padding-bottom: .35rem;
  border-bottom: 1px solid #21262d;
}
h2 .sub { color: #7d8590; font-weight: 400; font-size: .82rem; }
table { width: 100%; border-collapse: collapse; font-size: .88rem; }
th, td { text-align: left; padding: .45rem .6rem; border-bottom: 1px solid #1b2129; }
th {
  color: #7d8590;
  font-weight: 600;
  font-size: .76rem;
  text-transform: uppercase;
  letter-spacing: .03em;
}
td.num { text-align: right; font-family: ui-monospace, monospace; }
tr:hover td { background: #11161d; }
.flag {
  display: inline-block;
  margin: 1px 2px;
  padding: 1px 6px;
  border-radius: 999px;
  font-size: .68rem;
  background: #3a1d1d;
  border: 1px solid #6e2b2b;
  color: #ff9a9a;
  font-family: ui-monospace, monospace;
}
.ok { color: #3fb950; }
.sev-critical { color: #ff7b72; font-weight: 600; }
.sev-warning { color: #e3b341; font-weight: 600; }
.sev-info { color: #6cb6ff; }
.empty { color: #7d8590; font-style: italic; }
footer {
  margin-top: 3rem;
  padding-top: 1rem;
  border-top: 1px solid #21262d;
  font-size: .8rem;
  color: #7d8590;
}
a { color: #6cb6ff; text-decoration: none; }
a:hover { text-decoration: underline; }
"""


def _esc(value: Any) -> str:
    return html.escape(str(value))


def _flags(flags: list[Any]) -> str:
    if not flags:
        return '<span class="ok">—</span>'
    return "".join(f'<span class="flag">{_esc(f)}</span>' for f in flags)


def _card(label: str, value: str) -> str:
    return f'<div class="card"><div class="k">{_esc(label)}</div><div class="v">{value}</div></div>'


def _pool_section(title: str, sub: str, pools: list[dict[str, Any]]) -> str:
    head = f'<section><h2>{_esc(title)} <span class="sub">{_esc(sub)}</span></h2>'
    if not pools:
        return head + '<p class="empty">No pools.</p></section>'
    rows = "".join(
        "<tr>"
        f'<td class="num">{_esc(p.get("net_apy_pct"))}</td>'
        f"<td>{_esc(p.get('chain'))}</td>"
        f"<td>{_esc(p.get('project'))}</td>"
        f"<td>{_esc(p.get('symbol'))}</td>"
        f"<td>{_flags(p.get('flags') or [])}</td>"
        "</tr>"
        for p in pools
    )
    header_row = (
        "<thead><tr><th>net APY %</th><th>chain</th><th>project</th>"
        "<th>symbol</th><th>flags</th></tr></thead>"
    )
    return f"{head}<table>{header_row}<tbody>{rows}</tbody></table></section>"


def _alerts_section(alerts: dict[str, Any] | None) -> str:
    head = '<section><h2>Alerts <span class="sub">on watched positions</span></h2>'
    if not alerts:
        note = "Needs two snapshots — monitoring populates over time."
        return f'{head}<p class="empty">{note}</p></section>'
    counts = (
        f'<p>{alerts.get("critical", 0)} critical · {alerts.get("warning", 0)} warning · '
        f'{alerts.get("info", 0)} info across {alerts.get("watched", 0)} watched.</p>'
    )
    top = alerts.get("top") or []
    if not top:
        return f'{head}{counts}<p class="ok">No degradation detected.</p></section>'
    rows = "".join(
        "<tr>"
        f'<td class="sev-{_esc(a.get("severity"))}">{_esc(a.get("severity"))}</td>'
        f"<td>{_esc(a.get('kind'))}</td><td>{_esc(a.get('symbol'))}</td>"
        f"<td>{_esc(a.get('detail'))}</td></tr>"
        for a in top
    )
    header_row = (
        "<thead><tr><th>severity</th><th>kind</th><th>symbol</th><th>detail</th></tr></thead>"
    )
    return f"{head}{counts}<table>{header_row}<tbody>{rows}</tbody></table></section>"


def _allocation_section(alloc: dict[str, Any] | None) -> str:
    if not alloc:
        return ""
    line = (
        f'Budget ${_esc(alloc.get("budget_usd"))} · deployed ${_esc(alloc.get("deployed_usd"))} '
        f'· cash ${_esc(alloc.get("cash_usd"))} · {_esc(alloc.get("positions"))} positions · '
        f'blended {_esc(alloc.get("blended_net_apy_on_budget_pct"))}% net APY on budget.'
    )
    return (
        '<section><h2>Allocation <span class="sub">capped, deterministic</span></h2>'
        f"<p>{line}</p></section>"
    )


def _calibration_section(cal: dict[str, Any] | None) -> str:
    head = '<section><h2>Calibration <span class="sub">is the conservatism real?</span></h2>'
    if not cal:
        note = "Needs history — short windows are weak signal."
        return f'{head}<p class="empty">{note}</p></section>'
    conservatism = round(float(cal.get("conservatism_rate", 0)) * 100, 1)
    reversion = round(float(cal.get("spike_reversion_rate", 0)) * 100, 0)
    line = (
        f'{conservatism}% conservative over {_esc(cal.get("matched"))} matched · median error '
        f'{_esc(cal.get("median_signed_error_pp"))}pp · SPIKE reversion {reversion}%.'
    )
    return f"{head}<p>{line}</p></section>"


def _simple_list_section(title: str, sub: str, items: list[str]) -> str:
    head = f'<section><h2>{_esc(title)} <span class="sub">{_esc(sub)}</span></h2>'
    if not items:
        return f'{head}<p class="empty">No data (supply your own input file).</p></section>'
    lis = "".join(f"<li>{item}</li>" for item in items)
    return f"{head}<ul>{lis}</ul></section>"


def render_html(summary: dict[str, Any]) -> str:
    """Render the full self-contained dashboard HTML from a summary dict (see build_dashboard)."""
    snap = summary.get("snapshot") or {}
    uni = summary.get("universe") or {}
    alloc = summary.get("allocation") or {}
    airdrops = summary.get("airdrops") or {}
    points = summary.get("points") or {}

    cards = [
        _card("Ranked pools", _esc(uni.get("ranked", 0))),
        _card("CORE", _esc(uni.get("core", 0))),
        _card("Satellite", _esc(uni.get("satellite", 0))),
    ]
    if alloc:
        blended = _esc(alloc.get("blended_net_apy_on_budget_pct"))
        cards.append(_card("Blended net APY", f"{blended}%"))

    airdrop_items = [
        f'{_esc(a.get("name"))} — {_esc(a.get("annualized_ev_pct"))}% ann. EV '
        f'(net ${_esc(a.get("net_ev_usd"))}) {_flags(a.get("flags") or [])}'
        for a in (airdrops.get("top") or [])
    ]
    points_items = [
        f'{_esc(s.get("campaign"))} — implied APY '
        f'{_esc(s.get("implied_apy_pct")) if s.get("implied_apy_pct") is not None else "n/a"} · '
        f'{_esc(s.get("points_per_day"))} pts/day'
        for s in (points.get("top") or [])
    ]

    sha = _esc(str(snap.get("sha", ""))[:12])
    meta = (
        f'snapshot {_esc(snap.get("fetched_at"))} · sha {sha} · '
        f'{_esc(snap.get("pool_count"))} pools scanned'
    )
    tagline = (
        "Risk-adjusted, conservative yield ranking from free DefiLlama data. CORE = deep, on a "
        "mature chain, in a recognized stable, audited &amp; on-peg. The agent builds the sheet; "
        "you sign every transaction."
    )
    disclaimer = (
        "⚠ Not financial advice. DeFi carries total-loss risk (exploits, depegs, reward "
        "collapse). Every number is modelled, not guaranteed."
    )
    footer = (
        f'Generated by <a href="{_REPO_URL}">ajentix</a> · open-source, deterministic, '
        "runtime LLM = 0 · read-only research, you execute every on-chain action."
    )

    body = "".join(
        [
            "<header>",
            '<h1>ajentix <span class="dim">· DeFi yield dashboard</span></h1>',
            f'<p class="tagline">{tagline}</p>',
            f'<p class="meta">{meta}</p>',
            f'<p class="disclaimer">{disclaimer}</p>',
            "</header>",
            '<div class="cards">',
            *cards,
            "</div>",
            _allocation_section(summary.get("allocation")),
            _pool_section("Top CORE", "capital-preservation", summary.get("top_core") or []),
            _pool_section(
                "Top SATELLITE", "higher yield / higher risk", summary.get("top_satellite") or []
            ),
            _alerts_section(summary.get("alerts")),
            _calibration_section(summary.get("calibration")),
            _simple_list_section("Airdrops", "top by annualized EV", airdrop_items),
            _simple_list_section("Points farming", "accrual + capital efficiency", points_items),
            f"<footer>{footer}</footer>",
        ]
    )

    return (
        '<!doctype html><html lang="en"><head><meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width, initial-scale=1">'
        "<title>ajentix · DeFi yield dashboard</title>"
        f"<style>{_CSS}</style></head><body><main>{body}</main></body></html>\n"
    )

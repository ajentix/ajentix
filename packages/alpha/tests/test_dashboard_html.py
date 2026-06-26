from __future__ import annotations

from ajentix_alpha.dashboard import build_dashboard
from ajentix_alpha.dashboard_html import render_html
from ajentix_alpha.yields import model as m
from ajentix_alpha.yields.sizing import build_plan


def _row(**kw: object) -> dict[str, object]:
    base: dict[str, object] = {
        "pool": "p1",
        "chain": "Base",
        "project": "demo",
        "symbol": "USDC",
        "tvlUsd": 50_000_000.0,
        "apy": 10.0,
        "apyBase": 10.0,
        "apyReward": 0.0,
        "apyMean30d": 10.0,
        "mu": 10.0,
        "sigma": 1.0,
        "count": 200,
        "stablecoin": True,
        "ilRisk": "no",
        "exposure": "single",
        "outlier": False,
        "rewardTokens": None,
    }
    base.update(kw)
    return base


def _summary(ranked: list[m.ScoredPool], **kw: object) -> dict[str, object]:
    snap = {
        "fetched_at": "2026-06-25T00:00:00Z",
        "sha": "deadbeefcafe00",
        "pool_count": len(ranked),
    }
    return build_dashboard(snapshot=snap, ranked=ranked, **kw)  # type: ignore[arg-type]


def test_renders_full_self_contained_html_document() -> None:
    out = render_html(_summary(m.rank_pools([_row()])))
    assert out.startswith("<!doctype html>")
    assert "</html>" in out.rstrip()[-20:]
    assert "<style>" in out and "http" not in out.split("<style>")[1].split("</style>")[0]
    assert "DeFi yield dashboard" in out
    assert "Not financial advice" in out
    assert "Top CORE" in out and "USDC" in out


def test_escapes_hostile_external_strings() -> None:
    rows = [_row(pool="x", project="<script>alert(1)</script>", symbol="<img src=x>")]
    out = render_html(_summary(m.rank_pools(rows)))
    assert "<script>alert(1)</script>" not in out  # never rendered as live markup
    assert "&lt;script&gt;" in out


def test_optional_sections_degrade_gracefully() -> None:
    out = render_html(_summary([]))
    assert "Allocation" not in out  # no plan -> section omitted entirely
    assert "Needs two snapshots" in out  # alerts placeholder
    assert "Needs history" in out  # calibration placeholder


def test_allocation_section_renders_when_plan_present() -> None:
    ranked = m.rank_pools([_row(pool="c1", apy=12.0, apyBase=12.0, apyMean30d=12.0)])
    out = render_html(_summary(ranked, plan=build_plan(ranked, 1000.0)))
    assert "Allocation" in out
    assert "net APY on budget" in out

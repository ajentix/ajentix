from __future__ import annotations

from ajentix_alpha.yields import model as m
from ajentix_alpha.yields import render
from ajentix_alpha.yields.sizing import build_plan


def _row(**kw: object) -> dict[str, object]:
    base: dict[str, object] = {
        "pool": "p1",
        "chain": "Ethereum",
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


def test_opportunity_row_is_flat_and_json_safe() -> None:
    s = m.score_pool(m.parse_pool(_row()))
    row = render.opportunity_row(s)
    assert row["chain"] == "Ethereum"
    assert row["pool_id"] == "p1"
    assert row["rank_key_net_apy_pct"] == round(s.net_apy, 3)
    assert row["flags"] == list(s.flags)


def test_opportunities_payload_counts_and_top_slice() -> None:
    ranked = m.rank_pools(
        [
            _row(pool="c1", apy=12.0, apyBase=12.0, apyMean30d=12.0),
            _row(pool="c2", apy=8.0, apyBase=8.0, apyMean30d=8.0),
            _row(pool="s1", apy=40.0, apyBase=40.0, stablecoin=False, symbol="ETH", ilRisk="yes"),
        ]
    )
    core = [s for s in ranked if s.tier == "core"]
    sat = [s for s in ranked if s.tier == "satellite"]
    payload = render.opportunities_payload(
        fetched_at="t", sha="deadbeefcafe", pool_count=3, ranked=ranked, core=core, sat=sat, top=1
    )
    assert payload["ranked_count"] == 3
    assert payload["core_count"] == len(core)
    assert len(payload["core"]) == 1  # top=1 slices the rows, not the counts


def test_opportunities_md_has_both_tier_tables() -> None:
    ranked = m.rank_pools([_row()])
    core = [s for s in ranked if s.tier == "core"]
    md = render.opportunities_md(core, [], "t", "deadbeefcafe", 5)
    assert "## CORE" in md and "## SATELLITE" in md
    assert "| Ethereum | demo | USDC |" in md


def test_breakeven_days_none_on_zero_yield() -> None:
    assert render.breakeven_days(100.0, 0.0, "Ethereum") is None
    be = render.breakeven_days(1000.0, 50.0, "Base")
    assert be is not None and be > 0.0


def test_allocation_payload_and_md_agree_with_plan() -> None:
    ranked = m.rank_pools([_row(pool="c1", apy=12.0, apyBase=12.0, apyMean30d=12.0, chain="Base")])
    plan = build_plan(ranked, 1000.0)
    payload = render.allocation_payload(fetched_at="t", sha="deadbeefcafe", plan=plan)
    assert payload["budget_usd"] == 1000.0
    assert len(payload["positions"]) == len(plan.positions)
    assert payload["positions"][0]["pool_id"] == "c1"
    md = render.allocation_md(plan, "t", "deadbeefcafe")
    assert "# Allocation plan (capped, deterministic)" in md
    assert "| core |" in md

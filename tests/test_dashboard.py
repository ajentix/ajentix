from __future__ import annotations

from ajentix_alpha import dashboard as d
from ajentix_alpha.yields import model as m
from ajentix_alpha.yields.monitor import diff_snapshots
from ajentix_alpha.yields.sizing import build_plan

_SNAP = {"fetched_at": "t", "sha": "abc", "pool_count": 10}


def _row(**kw: object) -> dict[str, object]:
    base: dict[str, object] = {
        "pool": "p",
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


def _ranked() -> list[m.ScoredPool]:
    return m.rank_pools(
        [
            _row(pool="c1", apy=12.0, apyBase=12.0, apyMean30d=12.0),
            _row(
                pool="s1", apy=40.0, apyBase=40.0, apyMean30d=40.0, stablecoin=False, symbol="ETH"
            ),
        ]
    )


def test_minimal_dashboard_degrades_to_none_sections() -> None:
    summary = d.build_dashboard(snapshot=_SNAP, ranked=_ranked())
    assert summary["universe"] == {"ranked": 2, "core": 1, "satellite": 1}
    assert len(summary["top_core"]) == 1
    assert len(summary["top_satellite"]) == 1
    for section in ("allocation", "alerts", "calibration", "airdrops", "points", "rebalance"):
        assert summary[section] is None


def test_allocation_section_present_when_plan_given() -> None:
    ranked = _ranked()
    summary = d.build_dashboard(snapshot=_SNAP, ranked=ranked, plan=build_plan(ranked, 1000.0))
    alloc = summary["allocation"]
    assert alloc is not None
    assert alloc["budget_usd"] == 1000.0
    assert alloc["positions"] >= 1


def test_alerts_section_summarizes_monitor() -> None:
    prev = [_row(pool="g", tvlUsd=50_000_000.0)]
    cur = [_row(pool="other", tvlUsd=50_000_000.0)]
    report = diff_snapshots(prev, cur)  # 'g' vanished -> 1 critical
    summary = d.build_dashboard(snapshot=_SNAP, ranked=_ranked(), alerts=report)
    assert summary["alerts"]["critical"] == 1
    assert summary["alerts"]["top"][0]["kind"] == "POOL_GONE"


def test_top_lists_capped_by_top_arg() -> None:
    rows = [
        _row(pool=f"c{i}", apy=12.0 - i, apyBase=12.0 - i, apyMean30d=12.0 - i) for i in range(8)
    ]
    summary = d.build_dashboard(snapshot=_SNAP, ranked=m.rank_pools(rows), top=3)
    assert len(summary["top_core"]) == 3

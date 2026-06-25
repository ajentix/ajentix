from __future__ import annotations

from ajentix_alpha.yields import monitor as mon


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


def _kinds(report: mon.MonitorReport) -> set[str]:
    return {a.kind for a in report.alerts}


def test_no_change_no_alerts() -> None:
    snap = [_row()]
    report = mon.diff_snapshots(snap, snap)
    assert report.alerts == ()
    assert report.critical == report.warning == report.info == 0


def test_pool_gone_is_critical() -> None:
    report = mon.diff_snapshots([_row(pool="x")], [_row(pool="y")])
    # "x" existed before and is gone now; "y" is new but not surfaced without include_new.
    assert _kinds(report) == {"POOL_GONE"}
    assert report.alerts[0].severity == "critical"
    assert report.alerts[0].pool_id == "x"


def test_apy_collapse_warning_and_critical() -> None:
    prev = [_row(apy=10.0, apyBase=10.0, apyMean30d=10.0)]
    # net APY 10 -> 5 (-50%) but still above zero floor => warning
    warn = mon.diff_snapshots(prev, [_row(apy=5.0, apyBase=5.0, apyMean30d=5.0)])
    a = next(a for a in warn.alerts if a.kind == "APY_COLLAPSE")
    assert a.severity == "warning"
    # net APY 10 -> ~0 => critical
    crit = mon.diff_snapshots(prev, [_row(apy=0.1, apyBase=0.1, apyMean30d=0.1)])
    a2 = next(a for a in crit.alerts if a.kind == "APY_COLLAPSE")
    assert a2.severity == "critical"


def test_tvl_drain_escalates_below_tradeable_floor() -> None:
    prev = [_row(tvlUsd=50_000_000.0)]
    # drained 99% to 500k -> below TVL_UNTRADEABLE_USD => critical
    report = mon.diff_snapshots(prev, [_row(tvlUsd=500_000.0)])
    a = next(a for a in report.alerts if a.kind == "TVL_DRAIN")
    assert a.severity == "critical"


def test_reward_cut_only_when_material_and_large() -> None:
    prev = [_row(apy=12.0, apyBase=4.0, apyReward=8.0, apyMean30d=12.0)]
    cur = [_row(apy=5.0, apyBase=4.0, apyReward=1.0, apyMean30d=5.0)]
    report = mon.diff_snapshots(prev, cur)
    assert "REWARD_CUT" in _kinds(report)
    # tiny reward (below material floor) changing is ignored
    quiet = mon.diff_snapshots(
        [_row(apy=10.5, apyBase=10.0, apyReward=0.5, apyMean30d=10.5)],
        [_row(apy=10.0, apyBase=10.0, apyReward=0.0, apyMean30d=10.0)],
    )
    assert "REWARD_CUT" not in _kinds(quiet)


def test_flag_raised_on_new_unstable() -> None:
    prev = [_row(mu=10.0, sigma=1.0)]  # stable
    cur = [_row(mu=10.0, sigma=6.0)]  # cv 0.6 -> UNSTABLE
    report = mon.diff_snapshots(prev, cur)
    a = next(a for a in report.alerts if a.kind == "FLAG_RAISED")
    assert "UNSTABLE" in a.detail


def test_tier_downgrade_core_to_satellite() -> None:
    prev = [_row(stablecoin=True, ilRisk="no")]  # core
    cur = [_row(stablecoin=False, symbol="ETH")]  # satellite
    report = mon.diff_snapshots(prev, cur)
    assert "TIER_DOWNGRADE" in _kinds(report)


def test_watch_restricts_targets() -> None:
    prev = [_row(pool="a", tvlUsd=50_000_000.0), _row(pool="b", tvlUsd=50_000_000.0)]
    cur = [_row(pool="a", tvlUsd=100_000.0), _row(pool="b", tvlUsd=100_000.0)]
    # Only watch "a": "b" also drained but must not be reported.
    report = mon.diff_snapshots(prev, cur, watch={"a"})
    assert {a.pool_id for a in report.alerts} == {"a"}
    assert report.watched == 1


def test_watch_missing_pool_not_found() -> None:
    report = mon.diff_snapshots([_row(pool="a")], [_row(pool="a")], watch={"ghost"})
    assert _kinds(report) == {"NOT_FOUND"}


def test_include_new_surfaces_fresh_high_apy_pool() -> None:
    prev = [_row(pool="a")]
    cur = [_row(pool="a"), _row(pool="b", apy=15.0, apyBase=15.0, apyMean30d=15.0)]
    report = mon.diff_snapshots(prev, cur, include_new=True)
    new = [a for a in report.alerts if a.kind == "NEW_OPPORTUNITY"]
    assert [a.pool_id for a in new] == ["b"]
    assert new[0].severity == "info"
    # Without include_new, nothing new is surfaced.
    assert "NEW_OPPORTUNITY" not in _kinds(mon.diff_snapshots(prev, cur))


def test_alerts_sorted_critical_first() -> None:
    prev = [
        _row(pool="gone", tvlUsd=50_000_000.0),
        _row(pool="warn", apy=10.0, apyBase=10.0, apyMean30d=10.0, tvlUsd=50_000_000.0),
    ]
    cur = [_row(pool="warn", apy=5.0, apyBase=5.0, apyMean30d=5.0, tvlUsd=50_000_000.0)]
    report = mon.diff_snapshots(prev, cur)
    severities = [a.severity for a in report.alerts]
    assert severities == sorted(severities, key=lambda s: mon._SEVERITY_RANK[s])
    assert report.alerts[0].severity == "critical"

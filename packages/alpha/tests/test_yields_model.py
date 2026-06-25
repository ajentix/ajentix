from __future__ import annotations

from ajentix_alpha.yields import model as m


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


def test_universe_excludes_thin_short_outlier_zero() -> None:
    assert m.passes_universe(m.parse_pool(_row()))
    assert not m.passes_universe(m.parse_pool(_row(tvlUsd=500_000.0)))
    assert not m.passes_universe(m.parse_pool(_row(count=5)))
    assert not m.passes_universe(m.parse_pool(_row(outlier=True)))
    assert not m.passes_universe(m.parse_pool(_row(apy=0.0)))


def test_reward_haircut_only_hits_reward_portion() -> None:
    # apy 20, half from reward -> reward haircut 50% of the reward half => 20 - 0.5*10 = 15
    s = m.score_pool(m.parse_pool(_row(apy=20.0, apyBase=10.0, apyReward=10.0, apyMean30d=20.0)))
    assert abs(s.reward_haircut_apy - 15.0) < 1e-9
    assert "REWARD_DEPENDENT" not in s.flags  # share exactly 0.5 is the boundary, not > 0.5


def test_reward_dependent_flag_boundary() -> None:
    s = m.score_pool(m.parse_pool(_row(apy=20.0, apyBase=9.0, apyReward=11.0, apyMean30d=20.0)))
    assert "REWARD_DEPENDENT" in s.flags  # 0.55 > 0.5


def test_anti_spike_caps_spot_above_30d_mean() -> None:
    # spot 30 but 30d mean 10 -> net capped near 10, SPIKE flag set
    s = m.score_pool(m.parse_pool(_row(apy=30.0, apyBase=30.0, apyReward=0.0, apyMean30d=10.0)))
    assert s.net_apy <= 10.0 + 1e-9
    assert "SPIKE" in s.flags


def test_il_haircut_volatile_vs_stable() -> None:
    vol = m.score_pool(
        m.parse_pool(_row(stablecoin=False, ilRisk="yes", exposure="multi", symbol="ETH-FOO"))
    )
    assert abs(vol.il_factor - m.IL_FACTOR_VOLATILE) < 1e-9
    assert "IL_EXPOSED" in vol.flags
    stable_multi = m.score_pool(m.parse_pool(_row(stablecoin=True, ilRisk="yes", exposure="multi")))
    assert abs(stable_multi.il_factor - m.IL_FACTOR_STABLE_MULTI) < 1e-9


def test_unstable_flag_on_high_cv() -> None:
    s = m.score_pool(m.parse_pool(_row(mu=10.0, sigma=6.0)))
    assert "UNSTABLE" in s.flags


def test_core_requires_stable_deep_clean() -> None:
    core = m.score_pool(m.parse_pool(_row()))
    assert core.tier == "core"
    # thin tvl -> satellite
    assert m.score_pool(m.parse_pool(_row(tvlUsd=5_000_000.0))).tier == "satellite"
    # volatile asset -> satellite
    assert m.score_pool(m.parse_pool(_row(stablecoin=False, symbol="ETH"))).tier == "satellite"


def test_rank_sorts_by_net_apy_desc() -> None:
    rows = [
        _row(pool="low", apy=5.0, apyBase=5.0, apyMean30d=5.0),
        _row(pool="high", apy=12.0, apyBase=12.0, apyMean30d=12.0),
        _row(pool="mid", apy=8.0, apyBase=8.0, apyMean30d=8.0),
    ]
    ranked = m.rank_pools(rows)
    assert [s.pool.pool_id for s in ranked] == ["high", "mid", "low"]


def test_net_apy_never_negative_and_zero_apy_excluded() -> None:
    ranked = m.rank_pools([_row(apy=0.0)])
    assert ranked == []

from __future__ import annotations

from ajentix_alpha.yields import model as m
from ajentix_alpha.yields import sizing as z


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


def _core(pool_id: str, apy: float, *, chain: str = "Base") -> m.ScoredPool:
    # Default to a cheap chain so the gas-payback filter does not mask capping/water-fill logic.
    s = m.score_pool(
        m.parse_pool(_row(pool=pool_id, apy=apy, apyBase=apy, apyMean30d=apy, chain=chain))
    )
    assert s.tier == "core"
    return s


def _sat(pool_id: str, apy: float) -> m.ScoredPool:
    s = m.score_pool(
        m.parse_pool(
            _row(pool=pool_id, apy=apy, apyBase=apy, apyMean30d=apy, stablecoin=False, symbol="ETH")
        )
    )
    assert s.tier == "satellite"
    return s


def _sum(plan: z.AllocationPlan) -> float:
    return sum(p.usd for p in plan.positions)


def test_empty_ranked_is_all_cash() -> None:
    plan = z.build_plan([], 1000.0)
    assert plan.positions == ()
    assert plan.cash_usd == 1000.0
    assert plan.blended_net_apy_on_budget == 0.0
    assert plan.blended_net_apy_on_allocated == 0.0


def test_budget_conservation() -> None:
    ranked = [_core("c1", 12.0), _core("c2", 9.0), _sat("s1", 40.0), _sat("s2", 30.0)]
    plan = z.build_plan(ranked, 1500.0)
    assert abs(_sum(plan) + plan.cash_usd - 1500.0) < 1e-6
    assert abs(plan.core_usd + plan.satellite_usd - _sum(plan)) < 1e-6


def test_satellite_sleeve_hard_capped() -> None:
    # Satellite APYs dwarf core, but the sleeve must still stay <= 30% of budget.
    ranked = [_core("c1", 8.0)] + [_sat(f"s{i}", 50.0 + i) for i in range(3)]
    plan = z.build_plan(ranked, 1000.0)
    assert plan.satellite_usd <= 1000.0 * z.SATELLITE_CAP_SHARE + 1e-6


def test_per_pool_caps_respected() -> None:
    ranked = [_core("c1", 99.0), _core("c2", 1.0), _sat("s1", 99.0), _sat("s2", 1.0)]
    plan = z.build_plan(ranked, 2000.0)
    for p in plan.positions:
        cap = (
            z.CORE_MAX_PER_POOL_SHARE if p.tier == "core" else z.SATELLITE_MAX_PER_POOL_SHARE
        ) * 2000.0
        assert p.usd <= cap + 1e-6


def test_no_position_below_min() -> None:
    # Tiny budget must concentrate into a few real positions, never sub-min dust.
    ranked = [_core(f"c{i}", 10.0 - i) for i in range(8)]
    plan = z.build_plan(ranked, 500.0)
    assert plan.positions, "expected at least one deployable position"
    for p in plan.positions:
        assert p.usd >= z.MIN_POSITION_USD - 1e-6


def test_unused_satellite_flows_to_core() -> None:
    # No satellite pools: nothing is forced into satellite; core absorbs the freed budget.
    # 3 core pools so the per-pool cap (34% each = 102% capacity) does not bind below budget.
    ranked = [_core("c1", 10.0), _core("c2", 9.0), _core("c3", 8.0)]
    plan = z.build_plan(ranked, 1000.0)
    assert plan.satellite_usd == 0.0
    # Core absorbs ~the full budget, well above the 70% it would get if satellite were forced.
    assert plan.core_usd > 1000.0 * (1.0 - z.SATELLITE_CAP_SHARE) + 1e-6


def test_higher_net_apy_gets_more_within_caps() -> None:
    ranked = [_core("hi", 12.0), _core("lo", 6.0)]
    plan = z.build_plan(ranked, 1000.0)
    by_id = {p.pool_id: p.usd for p in plan.positions}
    assert by_id["hi"] >= by_id["lo"]


def test_blended_on_budget_counts_cash_as_zero() -> None:
    # Force heavy cash: one tiny-cap core pool can't absorb the budget.
    ranked = [_core("c1", 10.0)]
    plan = z.build_plan(ranked, 2000.0)
    assert plan.cash_usd > 0.0
    assert plan.blended_net_apy_on_budget < plan.blended_net_apy_on_allocated


def test_max_positions_per_sleeve() -> None:
    ranked = [_core(f"c{i}", 10.0 - i * 0.1) for i in range(10)]
    plan = z.build_plan(ranked, 100_000.0)  # big budget so min-position never binds
    assert len([p for p in plan.positions if p.tier == "core"]) <= z.MAX_CORE_POSITIONS

def test_gas_filter_drops_costly_chain_at_small_budget() -> None:
    # At $1000 an Ethereum core position is capped below the size that repays ~$30 gas -> dropped
    # in favour of the cheap-chain pool, even though Ethereum has the higher APY.
    ranked = [_core("eth", 12.0, chain="Ethereum"), _core("base", 10.0, chain="Base")]
    ids = {p.pool_id for p in z.build_plan(ranked, 1000.0).positions}
    assert "eth" not in ids
    assert "base" in ids


def test_gas_filter_keeps_costly_chain_only_when_budget_repays_gas() -> None:
    eth = [_core("eth", 12.0, chain="Ethereum")]
    assert z.build_plan(eth, 1000.0).positions == ()  # capped size can't repay $30 in 120d
    big = {p.pool_id for p in z.build_plan(eth, 3000.0).positions}
    assert "eth" in big  # larger budget -> larger capped position -> gas repaid -> kept


def test_gas_filter_disabled_with_infinite_payback() -> None:
    policy = z.SizingPolicy(gas_payback_days=float("inf"))
    ranked = [_core("eth", 12.0, chain="Ethereum")]
    plan = z.build_plan(ranked, 1000.0, policy=policy)
    assert "eth" in {p.pool_id for p in plan.positions}

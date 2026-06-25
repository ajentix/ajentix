from __future__ import annotations

from ajentix_alpha.yields import aggressive as agg
from ajentix_alpha.yields import model as m
from ajentix_alpha.yields.prices import coin_key


def _row(**kw: object) -> dict[str, object]:
    base: dict[str, object] = {
        "pool": "p1",
        "chain": "Base",
        "project": "demo",
        "symbol": "USDC",
        "tvlUsd": 50_000_000.0,
        "apy": 50.0,
        "apyBase": 50.0,
        "apyReward": 0.0,
        "apyMean30d": 50.0,
        "mu": 50.0,
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


def test_loss_modes_maps_present_flags_in_severity_order() -> None:
    modes = agg.loss_modes(("THIN_TVL", "IL_EXPOSED"))
    # LOSS_MODES iterates IL_EXPOSED before THIN_TVL, regardless of input order.
    assert modes == (agg.LOSS_MODES["IL_EXPOSED"], agg.LOSS_MODES["THIN_TVL"])
    assert agg.loss_modes(()) == ()
    assert agg.loss_modes(("NOT_A_FLAG",)) == ()


def test_rank_max_yield_sorts_by_quoted_apy_desc() -> None:
    ranked = m.rank_pools(
        [
            _row(pool="lo", apy=10.0, apyBase=10.0, apyMean30d=10.0),
            _row(pool="hi", apy=80.0, apyBase=80.0, apyMean30d=80.0),
            _row(pool="mid", apy=40.0, apyBase=40.0, apyMean30d=40.0),
        ]
    )
    menu = agg.rank_max_yield(ranked, top=10)
    assert [p.pool_id for p in menu] == ["hi", "mid", "lo"]
    assert menu[0].quoted_apy == 80.0


def test_degen_plan_caps_count_and_equal_weights() -> None:
    rows = [
        _row(pool=f"p{i}", apy=90.0 - i, apyBase=90.0 - i, apyMean30d=90.0 - i) for i in range(8)
    ]
    plan = agg.build_degen_plan(m.rank_pools(rows), 1000.0)
    assert len(plan.positions) == agg.DEGEN_MAX_POSITIONS  # 5
    assert all(abs(p.usd - 200.0) < 1e-6 for p in plan.positions)  # 1000 / 5
    assert abs(plan.deployed_usd - 1000.0) < 1e-6 and plan.cash_usd == 0.0
    assert plan.positions[0].quoted_apy == 90.0  # highest quoted first


def test_degen_plan_per_pool_cap_binds_and_leaves_cash() -> None:
    rows = [
        _row(pool=f"p{i}", apy=90.0 - i, apyBase=90.0 - i, apyMean30d=90.0 - i) for i in range(3)
    ]
    plan = agg.build_degen_plan(m.rank_pools(rows), 1000.0, policy=agg.DegenPolicy(max_positions=2))
    # per = min(1000/2=500, 1000*0.25=250) -> 250 each, 500 left as cash.
    assert len(plan.positions) == 2
    assert all(abs(p.usd - 250.0) < 1e-6 for p in plan.positions)
    assert abs(plan.cash_usd - 500.0) < 1e-6


def test_degen_plan_tiny_budget_is_all_cash() -> None:
    ranked = m.rank_pools([_row(apy=90.0, apyBase=90.0, apyMean30d=90.0)])
    plan = agg.build_degen_plan(ranked, 100.0)
    # per = min(100/5=20, 25) = 20 < $50 min position -> nothing deployable.
    assert plan.positions == ()
    assert plan.cash_usd == 100.0


def test_degen_plan_gas_filter_drops_costly_chain() -> None:
    rows = [_row(pool="eth", apy=80.0, apyBase=80.0, apyMean30d=80.0, chain="Ethereum")]
    plan = agg.build_degen_plan(m.rank_pools(rows), 300.0)
    # $60 on Ethereum at 80% earns ~$15.8 over the 120d window; cannot repay ~$30 round-trip gas.
    assert plan.positions == ()


def test_degen_plan_blends_quoted_and_net_over_budget() -> None:
    # apy 80, half reward -> conservative net 60 (reward-stickiness haircut), quoted stays 80.
    rows = [_row(pool="p", apy=80.0, apyBase=40.0, apyReward=40.0, apyMean30d=80.0)]
    plan = agg.build_degen_plan(m.rank_pools(rows), 1000.0, policy=agg.DegenPolicy(max_positions=1))
    p = plan.positions[0]
    assert p.quoted_apy == 80.0 and abs(p.conservative_net_apy - 60.0) < 1e-6
    # one $250 ticket (per-pool cap) over a $1000 budget: quoted 20%, net 15%.
    assert abs(plan.blended_quoted_apy - 20.0) < 1e-6
    assert abs(plan.blended_net_apy - 15.0) < 1e-6


def test_danger_score_counts_loss_modes() -> None:
    rows = [
        _row(
            pool="p",
            apy=80.0,
            apyBase=80.0,
            apyMean30d=80.0,
            stablecoin=False,
            symbol="ETH-USDC",
            ilRisk="yes",
            exposure="multi",
            tvlUsd=2_000_000.0,  # > $1M universe floor, < $25M -> THIN_TVL
        )
    ]
    pick = agg.rank_max_yield(m.rank_pools(rows), top=1)[0]
    assert "IL_EXPOSED" in pick.flags and "THIN_TVL" in pick.flags
    assert pick.danger_score == len(pick.loss_modes) > 0


def test_degen_plan_skips_model_zeroed_pool_but_menu_keeps_it() -> None:
    addr = "0x" + "a" * 40
    key = coin_key("Ethereum", addr)
    rows = [
        # highest quoted APY, but its stable underlying is 5% off peg -> DEPEG -> model net 0.
        _row(
            pool="broken", apy=300.0, apyBase=300.0, apyMean30d=300.0,
            chain="Ethereum", underlyingTokens=[addr],
        ),
        _row(pool="ok", apy=80.0, apyBase=80.0, apyMean30d=80.0),
    ]
    ranked = m.rank_pools(rows, prices={key: {"price": 0.95, "confidence": 0.99, "symbol": "USDC"}})
    plan = agg.build_degen_plan(ranked, 1000.0, policy=agg.DegenPolicy(max_positions=1))
    assert [p.pool_id for p in plan.positions] == ["ok"]  # broken (net 0) skipped, real yield sized
    assert agg.rank_max_yield(ranked, top=5)[0].pool_id == "broken"  # still shown in the menu

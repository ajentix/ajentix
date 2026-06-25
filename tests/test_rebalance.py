from __future__ import annotations

from ajentix_alpha.yields import model as m
from ajentix_alpha.yields import rebalance as rb
from ajentix_alpha.yields.sizing import build_plan


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


def _core(pool_id: str, apy: float, chain: str = "Base") -> m.ScoredPool:
    # Default to a cheap chain so the gas-payback guard does not mask classification logic.
    return m.score_pool(
        m.parse_pool(_row(pool=pool_id, chain=chain, apy=apy, apyBase=apy, apyMean30d=apy))
    )


_RANKED = [_core("A", 12.0), _core("B", 10.0), _core("C", 8.0)]
_BUDGET = 900.0


def _target() -> dict[str, float]:
    return {p.pool_id: p.usd for p in build_plan(_RANKED, _BUDGET).positions}


def _holdings_from(target: dict[str, float]) -> list[dict[str, object]]:
    return [{"pool_id": k, "usd": v} for k, v in target.items()]


def _by_id(plan: rb.RebalancePlan) -> dict[str, rb.RebalanceAction]:
    return {a.pool_id: a for a in plan.actions}


def test_holding_equals_target_is_all_hold() -> None:
    tgt = _target()
    plan = rb.build_rebalance(_holdings_from(tgt), _RANKED, budget_usd=_BUDGET)
    assert plan.n_trades == 0
    assert plan.turnover_usd == 0.0
    assert all(a.action == "HOLD" for a in plan.actions)


def test_stale_holding_is_sold() -> None:
    holdings = _holdings_from(_target()) + [{"pool_id": "ghost", "usd": 100.0}]
    plan = rb.build_rebalance(holdings, _RANKED, budget_usd=_BUDGET)
    ghost = _by_id(plan)["ghost"]
    assert ghost.action == "SELL"
    assert "no longer ranked" in ghost.reason
    assert ghost.target_usd == 0.0


def test_forced_exit_sells_and_excludes_from_target() -> None:
    tgt = _target()
    plan = rb.build_rebalance(_holdings_from(tgt), _RANKED, budget_usd=_BUDGET, force_exit={"A"})
    acts = _by_id(plan)
    assert acts["A"].action == "SELL"
    assert "forced exit" in acts["A"].reason
    assert acts["A"].target_usd == 0.0  # removed from the investable universe before sizing
    # The freed capital is redeployed into survivors (their target never drops below before).
    assert acts["B"].target_usd >= tgt["B"] - 1e-6
    assert acts["C"].target_usd >= tgt["C"] - 1e-6


def test_below_target_increases_above_target_reduces() -> None:
    tgt = _target()
    holdings = [
        {"pool_id": "A", "usd": tgt["A"] - 200.0},
        {"pool_id": "B", "usd": tgt["B"] + 200.0},
        {"pool_id": "C", "usd": tgt["C"]},
    ]
    plan = rb.build_rebalance(holdings, _RANKED, budget_usd=_BUDGET)
    acts = _by_id(plan)
    assert acts["A"].action == "INCREASE"
    assert acts["B"].action == "REDUCE"
    assert acts["C"].action == "HOLD"


def test_small_delta_stays_hold() -> None:
    tgt = _target()
    holdings = [
        {"pool_id": "A", "usd": tgt["A"] + 10.0},  # within the $50 churn floor
        {"pool_id": "B", "usd": tgt["B"]},
        {"pool_id": "C", "usd": tgt["C"]},
    ]
    plan = rb.build_rebalance(holdings, _RANKED, budget_usd=_BUDGET)
    assert _by_id(plan)["A"].action == "HOLD"


def test_budget_defaults_to_holdings_sum() -> None:
    holdings = [{"pool_id": "A", "usd": 300.0}, {"pool_id": "B", "usd": 200.0}]
    plan = rb.build_rebalance(holdings, _RANKED)
    assert plan.budget_usd == 500.0


def test_new_target_pool_is_bought() -> None:
    # Hold only A; B and C are unheld targets -> BUY.
    plan = rb.build_rebalance([{"pool_id": "A", "usd": 300.0}], _RANKED, budget_usd=_BUDGET)
    acts = _by_id(plan)
    assert acts["B"].action == "BUY"
    assert acts["C"].action == "BUY"


def test_gas_payback_guard_blocks_costly_chain_moves() -> None:
    # Same target, but on Ethereum a small move can't repay ~$30 round-trip gas -> HOLD.
    eth = [_core("A", 12.0, chain="Ethereum"), _core("B", 10.0, chain="Ethereum")]
    tgt = {p.pool_id: p.usd for p in build_plan(eth, 900.0).positions}
    holdings = [
        {"pool_id": "A", "usd": tgt["A"] - 120.0},  # > $50 floor, but yield can't repay $30 gas
        {"pool_id": "B", "usd": tgt["B"] + 120.0},
    ]
    plan = rb.build_rebalance(holdings, eth, budget_usd=900.0)
    acts = {a.pool_id: a for a in plan.actions}
    assert acts["A"].action == "HOLD"
    assert "gas payback" in acts["A"].reason
    # A generous payback window lets the same move through.
    loose = rb.build_rebalance(holdings, eth, budget_usd=900.0, payback_days=100_000.0)
    assert {a.pool_id: a for a in loose.actions}["A"].action == "INCREASE"

_REAL_UUID = "d85a7f5f-3624-4b6b-b3a7-eefb42b2a5e9"


def test_is_pool_id_accepts_real_uuid_rejects_placeholders() -> None:
    assert rb.is_pool_id(_REAL_UUID)
    assert not rb.is_pool_id("REPLACE-with-a-real-pool-uuid")  # shipped template placeholder
    assert not rb.is_pool_id("")
    assert not rb.is_pool_id(None)
    assert not rb.is_pool_id(123)


def test_real_holdings_drops_template_and_junk() -> None:
    rows = [
        {"pool_id": _REAL_UUID, "usd": 400.0},
        {"pool_id": "REPLACE-with-a-real-pool-uuid", "usd": 400.0},  # unedited template
        {"usd": 100.0},  # no pool_id
        "not-a-dict",
    ]
    kept = rb.real_holdings(rows)
    assert [h["pool_id"] for h in kept] == [_REAL_UUID]


def test_unedited_template_yields_no_trades() -> None:
    # The whole point: an untouched template must not surface phantom SELLs.
    template = [
        {"pool_id": "REPLACE-with-a-real-pool-uuid", "usd": 400.0},
        {"pool_id": "REPLACE-with-another-pool-uuid", "usd": 250.0},
    ]
    plan = rb.build_rebalance(rb.real_holdings(template), _RANKED, budget_usd=_BUDGET)
    assert all(a.action != "SELL" for a in plan.actions)

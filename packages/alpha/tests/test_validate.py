from __future__ import annotations

from ajentix_alpha.yields import validate as v


def _row(**kw: object) -> dict[str, object]:
    base: dict[str, object] = {
        "pool": "p",
        "chain": "Ethereum",
        "project": "demo",
        "symbol": "USDC",
        "tvlUsd": 50_000_000.0,
        "apy": 20.0,
        "apyBase": 10.0,
        "apyReward": 10.0,
        "apyMean30d": 20.0,
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


# baseline: good/rosy are clean stable cores with net APY 15 (reward-haircut of apy 20).
# spike is a SPIKE pool with net APY 10 (capped at its 30d mean).
def _prev() -> list[dict[str, object]]:
    return [
        _row(pool="good", tvlUsd=50_000_000.0),
        _row(pool="rosy", tvlUsd=50_000_000.0),
        _row(pool="spike", apy=30.0, apyBase=30.0, apyReward=0.0, apyMean30d=10.0),
        _row(pool="gone", tvlUsd=50_000_000.0),
    ]


def _cur() -> list[dict[str, object]]:
    return [
        # good: realized 18 >= predicted 15 -> conservative (+3)
        _row(pool="good", apy=18.0, apyBase=18.0, apyReward=0.0, apyMean30d=18.0),
        # rosy: realized 5 < predicted 15 -> over-prediction (-10)
        _row(pool="rosy", apy=5.0, apyBase=5.0, apyReward=0.0, apyMean30d=5.0, tvlUsd=50_000_000.0),
        # spike: realized 12 < baseline 30 -> reverted; predicted 10 -> conservative (+2)
        _row(
            pool="spike", apy=12.0, apyBase=12.0, apyReward=0.0, apyMean30d=12.0,
            tvlUsd=25_000_000.0,
        ),
        # 'gone' is absent -> not matched
    ]


def test_survival_excludes_vanished_pool() -> None:
    rep = v.calibrate(_prev(), _cur())
    assert rep.baseline_ranked == 4
    assert rep.matched == 3
    assert abs(rep.survival_rate - 0.75) < 1e-9


def test_conservatism_and_error_stats() -> None:
    rep = v.calibrate(_prev(), _cur())
    # good(+3) and spike(+2) conservative; rosy(-10) not -> 2/3.
    assert abs(rep.conservatism_rate - 2.0 / 3.0) < 1e-9
    assert abs(rep.median_signed_error - 2.0) < 1e-9
    assert abs(rep.mean_signed_error - (3.0 - 10.0 + 2.0) / 3.0) < 1e-9


def test_spike_reversion_detected() -> None:
    rep = v.calibrate(_prev(), _cur())
    assert rep.spike_count == 1
    assert abs(rep.spike_reversion_rate - 1.0) < 1e-9


def test_worst_overprediction_ranked_first() -> None:
    rep = v.calibrate(_prev(), _cur())
    assert rep.worst_overpredictions[0].pool_id == "rosy"
    assert rep.worst_overpredictions[0].signed_error < 0


def test_tvl_change_split_by_tier() -> None:
    rep = v.calibrate(_prev(), _cur())
    # good & rosy are CORE and held TVL (0%); spike is SATELLITE and lost 50%.
    assert abs(rep.core_tvl_median_change_pct - 0.0) < 1e-9
    assert abs(rep.satellite_tvl_median_change_pct + 50.0) < 1e-9


def test_empty_baseline_is_safe() -> None:
    rep = v.calibrate([], _cur())
    assert rep.baseline_ranked == 0
    assert rep.matched == 0
    assert rep.conservatism_rate == 0.0
    assert rep.survival_rate == 0.0

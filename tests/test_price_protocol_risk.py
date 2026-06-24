from __future__ import annotations

from ajentix_alpha.yields import model as m
from ajentix_alpha.yields.prices import coin_key

_ADDR = "0xA0b86991c6218b36c1d19D4a2e9Eb0cE3606eB48"
_CHAIN = "Ethereum"
_KEY = coin_key(_CHAIN, _ADDR)
_NOW = 1_000_000_000.0


def _row(**kw: object) -> dict[str, object]:
    base: dict[str, object] = {
        "pool": "p1",
        "chain": _CHAIN,
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
        "underlyingTokens": [_ADDR],
    }
    base.update(kw)
    return base


def _prices(price: float, conf: float = 0.99) -> dict[str, dict[str, object]]:
    return {_KEY: {"price": price, "confidence": conf, "symbol": "USDC"}}


# --- depeg --------------------------------------------------------------------------------------
def test_well_pegged_stable_no_flag_no_haircut() -> None:
    s = m.score_pool(m.parse_pool(_row()), prices=_prices(1.0))
    assert "DEPEG" not in s.flags and "DEPEG_WATCH" not in s.flags
    assert abs(s.net_apy - 10.0) < 1e-9
    assert s.tier == "core"


def test_depeg_watch_flags_and_partially_haircuts() -> None:
    # 1% deviation: between watch (0.5%) and break (2%) -> DEPEG_WATCH, factor in (0,1).
    s = m.score_pool(m.parse_pool(_row()), prices=_prices(0.99))
    assert "DEPEG_WATCH" in s.flags
    assert 0.0 < s.net_apy < 10.0
    assert s.tier == "satellite"  # depeg watch is disqualified from CORE
    assert abs(s.peg_deviation - 0.01) < 1e-6


def test_hard_depeg_zeroes_net_apy() -> None:
    # 3% deviation >= break threshold -> DEPEG, net APY zeroed (principal risk, not yield).
    s = m.score_pool(m.parse_pool(_row()), prices=_prices(0.97))
    assert "DEPEG" in s.flags
    assert s.net_apy == 0.0
    assert s.tier == "satellite"


def test_low_confidence_price_ignored() -> None:
    s = m.score_pool(m.parse_pool(_row()), prices=_prices(0.97, conf=0.5))
    assert "DEPEG" not in s.flags
    assert abs(s.net_apy - 10.0) < 1e-9  # cannot verify -> no adjustment


def test_non_stable_pool_not_peg_assessed() -> None:
    s = m.score_pool(m.parse_pool(_row(stablecoin=False, symbol="ETH")), prices=_prices(0.90))
    assert "DEPEG" not in s.flags and "DEPEG_WATCH" not in s.flags
    assert s.peg_deviation == 0.0


def test_missing_price_no_adjustment() -> None:
    s = m.score_pool(m.parse_pool(_row()), prices={})
    assert s.peg_deviation == 0.0
    assert abs(s.net_apy - 10.0) < 1e-9


# --- protocol risk ------------------------------------------------------------------------------
def _protocols(**over: object) -> dict[str, dict[str, object]]:
    base: dict[str, object] = {"slug": "demo", "audits": "2", "listedAt": _NOW - 400 * 86400}
    base.update(over)
    return {"demo": base}


def test_audited_established_protocol_stays_core() -> None:
    s = m.score_pool(m.parse_pool(_row()), protocols=_protocols(), now_ts=_NOW)
    assert "UNAUDITED" not in s.flags and "YOUNG_PROTOCOL" not in s.flags
    assert s.tier == "core"


def test_unaudited_blocks_core() -> None:
    s = m.score_pool(m.parse_pool(_row()), protocols=_protocols(audits="0"), now_ts=_NOW)
    assert "UNAUDITED" in s.flags
    assert s.tier == "satellite"


def test_young_protocol_blocks_core() -> None:
    s = m.score_pool(
        m.parse_pool(_row()), protocols=_protocols(listedAt=_NOW - 10 * 86400), now_ts=_NOW
    )
    assert "YOUNG_PROTOCOL" in s.flags
    assert s.tier == "satellite"


def test_unknown_protocol_flags_but_does_not_block_core() -> None:
    s = m.score_pool(m.parse_pool(_row(project="ghost")), protocols=_protocols(), now_ts=_NOW)
    assert "UNKNOWN_PROTOCOL" in s.flags
    assert s.tier == "core"  # slug mismatch is common; informational, not disqualifying


def test_no_injected_data_is_backward_compatible() -> None:
    plain = m.score_pool(m.parse_pool(_row()))
    assert plain.flags == ()
    assert plain.peg_deviation == 0.0
    assert plain.tier == "core"

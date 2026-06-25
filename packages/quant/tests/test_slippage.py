import pytest

from ajentix_quant.backtest.slippage import SlippageModel


def test_slippage_is_monotonic_capped_and_stressed() -> None:
    model = SlippageModel(base_bps=1.0, impact_bps_per_pct_volume=5.0, cap_bps=25.0)

    small = model.slippage_bps(order_notional=100.0, bar_volume_notional=10_000.0)
    medium = model.slippage_bps(order_notional=1_000.0, bar_volume_notional=10_000.0)
    capped = model.slippage_bps(order_notional=1_000_000.0, bar_volume_notional=10_000.0)
    stressed = model.slippage_cost(
        order_notional=1_000.0,
        bar_volume_notional=10_000.0,
        stress_multiplier=2.0,
    )
    base_cost = model.slippage_cost(order_notional=1_000.0, bar_volume_notional=10_000.0)

    assert small < medium <= capped
    assert capped == pytest.approx(25.0)
    assert stressed > base_cost


@pytest.mark.parametrize("volume", [0.0, -1.0, None])
def test_slippage_fails_closed_without_positive_trade_volume(volume: float | None) -> None:
    model = SlippageModel(base_bps=1.0, impact_bps_per_pct_volume=5.0, cap_bps=25.0)

    with pytest.raises(ValueError, match="volume"):
        model.slippage_cost(order_notional=100.0, bar_volume_notional=volume)

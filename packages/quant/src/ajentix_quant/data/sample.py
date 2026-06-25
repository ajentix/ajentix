"""Deterministic synthetic 8h funding series for offline backtest/CI.

Positive-biased (structural long-demand) with a negative-funding stress window and a
short funding spike, so the harness exercises entry, exit, and the funding-reversal path
without any network access. Phase 1 replaces this with real Bybit history via ccxt.
"""

from __future__ import annotations

import math

from ajentix_quant.strategies.state import MarketState


def sample_funding_8h() -> list[float]:
    series: list[float] = []
    for i in range(120):
        rate = 0.00012 + 0.00008 * math.sin(i / 7.0)  # ~0.004%..0.020% / 8h, positive bias
        if 40 <= i < 47:  # sustained negative-funding regime (stress)
            rate = -0.00006
        if i == 80:  # brief funding spike
            rate = 0.00060
        series.append(round(rate, 6))
    return series


def sample_market_dataset() -> list[MarketState]:
    """Deterministic typed states for the Phase 1 carry decision surface."""
    symbol = "BTC/USDT:USDT"
    states: list[MarketState] = []

    def state(
        *,
        funding_rate: float,
        in_position: bool,
        equity_usd: float = 1_000.0,
        expected_cost_bps: float = 2.0,
        gap_survival_leverage_cap: float = 2.0,
        current_leverage: float = 2.0,
        basis_bps: float = 8.0,
        net_delta_frac: float = 0.0,
        risk_deleverage: bool = False,
    ) -> MarketState:
        return MarketState(
            symbol=symbol,
            funding_rate=funding_rate,
            interval_hours=8.0,
            spot_close=100.0,
            perp_mark_close=100.08,
            index_close=100.0,
            basis_bps=basis_bps,
            realized_vol_annual=0.45,
            expected_cost_bps=expected_cost_bps,
            equity_usd=equity_usd,
            net_delta_frac=net_delta_frac,
            in_position=in_position,
            current_leverage=current_leverage if in_position else 0.0,
            gap_survival_leverage_cap=gap_survival_leverage_cap,
            health_factor=2.0,
            risk_deleverage=risk_deleverage,
        )

    states.append(state(funding_rate=0.00022, in_position=False))
    for i in range(1, 6):
        states.append(
            state(
                funding_rate=0.00020 - i * 0.000005,
                in_position=True,
                basis_bps=8.0 + i,
            )
        )

    for rate in (0.000045, 0.000040, 0.000030, 0.000020):
        states.append(state(funding_rate=rate, in_position=True))

    states.extend(
        [
            state(funding_rate=-0.000020, in_position=True),
            state(funding_rate=-0.000040, in_position=False),
            state(funding_rate=-0.000055, in_position=True),
            state(funding_rate=-0.000010, in_position=False),
        ]
    )

    states.extend(
        [
            state(funding_rate=0.00022, in_position=True, risk_deleverage=True),
            state(funding_rate=0.00022, in_position=True, net_delta_frac=0.035),
            state(funding_rate=0.00022, in_position=True, basis_bps=65.0),
            state(
                funding_rate=0.00030,
                in_position=False,
                equity_usd=10.0,
                gap_survival_leverage_cap=1.0,
            ),
            state(funding_rate=0.00030, in_position=False, gap_survival_leverage_cap=0.80),
            state(
                funding_rate=0.00010,
                in_position=False,
                expected_cost_bps=2.0,
                gap_survival_leverage_cap=1.0,
            ),
        ]
    )

    for i in range(4):
        states.append(
            state(
                funding_rate=0.00018 + i * 0.00001,
                in_position=i > 0,
                basis_bps=-6.0 + i,
            )
        )

    return states

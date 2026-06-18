"""Deterministic synthetic 8h funding series for offline backtest/CI.

Positive-biased (structural long-demand) with a negative-funding stress window and a
short funding spike, so the harness exercises entry, exit, and the funding-reversal path
without any network access. Phase 1 replaces this with real Bybit history via ccxt.
"""

from __future__ import annotations

import math


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

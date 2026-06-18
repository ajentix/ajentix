"""Risk-adjusted performance metrics (pure stdlib, no pandas)."""

from __future__ import annotations

import math


def annualized_return(returns: list[float], periods_per_year: float) -> float:
    if not returns:
        return 0.0
    total = 1.0
    for r in returns:
        total *= 1.0 + r
    if total <= 0:
        return -1.0
    return total ** (periods_per_year / len(returns)) - 1.0


def sharpe(returns: list[float], periods_per_year: float, rf: float = 0.0) -> float:
    if len(returns) < 2:
        return 0.0
    mean = sum(returns) / len(returns)
    var = sum((r - mean) ** 2 for r in returns) / (len(returns) - 1)
    std = math.sqrt(var)
    if std == 0:
        return 0.0
    return (mean - rf) / std * math.sqrt(periods_per_year)


def sortino(returns: list[float], periods_per_year: float, rf: float = 0.0) -> float:
    if len(returns) < 2:
        return 0.0
    mean = sum(returns) / len(returns)
    downside = [min(0.0, r - rf) for r in returns]
    dvar = sum(d * d for d in downside) / len(returns)
    dstd = math.sqrt(dvar)
    if dstd == 0:
        return 0.0
    return (mean - rf) / dstd * math.sqrt(periods_per_year)


def max_drawdown(equity_curve: list[float]) -> float:
    peak = float("-inf")
    mdd = 0.0
    for v in equity_curve:
        peak = max(peak, v)
        if peak > 0:
            mdd = max(mdd, (peak - v) / peak)
    return mdd

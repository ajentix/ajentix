"""Risk-adjusted performance metrics (pure stdlib, no pandas)."""

from __future__ import annotations

import math


def _require_finite(name: str, value: float) -> float:
    value = float(value)
    if not math.isfinite(value):
        raise ValueError(f"{name} must be finite, got {value!r}")
    return value


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


def calmar(ann_return: float, max_drawdown: float) -> float:
    """Annualized return divided by max drawdown, with explicit zero-drawdown convention."""

    ann = _require_finite("ann_return", ann_return)
    mdd = _require_finite("max_drawdown", max_drawdown)
    if mdd < 0.0:
        raise ValueError("max_drawdown must be non-negative")
    if mdd == 0.0:
        if ann > 0.0:
            return math.inf
        if ann < 0.0:
            return -math.inf
        return 0.0
    return ann / mdd


def win_rate(period_returns: list[float]) -> float:
    """Fraction of strictly positive period returns; break-even is not a win."""

    if not period_returns:
        return 0.0
    wins = 0
    for r in period_returns:
        if _require_finite("period_return", r) > 0.0:
            wins += 1
    return wins / len(period_returns)


def funding_capture(captured: float, available: float) -> float:
    """Captured funding divided by available funding; sign-preserving and unclamped."""

    captured_value = _require_finite("captured", captured)
    available_value = _require_finite("available", available)
    if available_value == 0.0:
        return 0.0
    return captured_value / available_value


def max_abs_net_delta_frac(net_deltas: list[float]) -> float:
    """Maximum absolute net-delta fraction over a path."""

    if not net_deltas:
        return 0.0
    return max(abs(_require_finite("net_delta", delta)) for delta in net_deltas)

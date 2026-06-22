"""Deterministic diagnostic Black-Scholes valuation for option-chain sanity checks.

Local values and Greeks are diagnostic only. They are intentionally separate from fill
pricing: authorizing fills must come from bid/ask crossing through
``ajentix_quant.backtest.option_costs``.
"""

from __future__ import annotations

import math
from collections.abc import Iterable
from dataclasses import dataclass
from types import MappingProxyType

from ajentix_quant.options.types import OptionLeg, OptionType
from ajentix_quant.research.vrp_preregistration import PLAN_GREEK_PROVENANCE

_MS_PER_DAY = 86_400_000
_DAYS_PER_YEAR = 365.0
_SQRT_2 = math.sqrt(2.0)
_INV_SQRT_2PI = 1.0 / math.sqrt(2.0 * math.pi)

LOCAL_GREEK_PROVENANCE = MappingProxyType(dict(PLAN_GREEK_PROVENANCE))
LOCAL_GREEKS_ROLE = str(PLAN_GREEK_PROVENANCE["local_greeks_role"])
DEFAULT_RISK_FREE_RATE = float(PLAN_GREEK_PROVENANCE["risk_free_rate"])
DEFAULT_DIVIDEND_YIELD = float(PLAN_GREEK_PROVENANCE["dividend"])
DAY_COUNT = str(PLAN_GREEK_PROVENANCE["day_count"])
TIMESTAMP_CONVENTION = str(PLAN_GREEK_PROVENANCE["timestamp_convention"])



@dataclass(frozen=True, kw_only=True)
class BlackScholesGreeks:
    """Diagnostic Black-Scholes value and first-order risk numbers.

    ``vega`` is per 1.00 volatility point and ``theta`` is per calendar year. The
    ``role`` field is part of the frozen provenance contract and deliberately states
    that these numbers do not authorize fills.
    """

    option_type: OptionType
    value: float
    delta: float
    gamma: float
    vega: float
    theta: float
    d1: float
    d2: float
    time_to_expiry_years: float
    volatility: float
    risk_free_rate: float
    dividend_yield: float
    day_count: str
    timestamp_convention: str
    role: str


def year_fraction_act_365(*, snapshot_ts_ms: int, expiry_ms: int) -> float:
    """Return ACT/365 UTC year fraction between snapshot and expiry timestamps."""

    _require_int("snapshot_ts_ms", snapshot_ts_ms, positive=True)
    _require_int("expiry_ms", expiry_ms, positive=True)
    if expiry_ms <= snapshot_ts_ms:
        raise ValueError("expiry_ms must be after snapshot_ts_ms")
    return float((expiry_ms - snapshot_ts_ms) / (_MS_PER_DAY * _DAYS_PER_YEAR))


def black_scholes_value_greeks(
    *,
    option_type: OptionType | str,
    spot: float,
    strike: float,
    time_to_expiry_years: float,
    volatility: float,
    risk_free_rate: float = DEFAULT_RISK_FREE_RATE,
    dividend_yield: float = DEFAULT_DIVIDEND_YIELD,
) -> BlackScholesGreeks:
    """Return deterministic Black-Scholes value and Greeks for diagnostics only."""

    option_type = option_type if isinstance(option_type, OptionType) else OptionType(option_type)
    spot = _require_positive("spot", spot)
    strike = _require_positive("strike", strike)
    time_to_expiry_years = _require_positive("time_to_expiry_years", time_to_expiry_years)
    volatility = _require_positive("volatility", volatility)
    risk_free_rate = _require_finite("risk_free_rate", risk_free_rate)
    dividend_yield = _require_finite("dividend_yield", dividend_yield)

    sqrt_t = math.sqrt(time_to_expiry_years)
    carry = risk_free_rate - dividend_yield
    d1 = (
        math.log(spot / strike)
        + (carry + 0.5 * volatility * volatility) * time_to_expiry_years
    ) / (volatility * sqrt_t)
    d2 = d1 - volatility * sqrt_t
    discount_r = math.exp(-risk_free_rate * time_to_expiry_years)
    discount_q = math.exp(-dividend_yield * time_to_expiry_years)
    pdf_d1 = _norm_pdf(d1)

    if option_type is OptionType.CALL:
        value = spot * discount_q * _norm_cdf(d1) - strike * discount_r * _norm_cdf(d2)
        delta = discount_q * _norm_cdf(d1)
        theta = (
            -(spot * discount_q * pdf_d1 * volatility) / (2.0 * sqrt_t)
            - risk_free_rate * strike * discount_r * _norm_cdf(d2)
            + dividend_yield * spot * discount_q * _norm_cdf(d1)
        )
    else:
        value = strike * discount_r * _norm_cdf(-d2) - spot * discount_q * _norm_cdf(-d1)
        delta = discount_q * (_norm_cdf(d1) - 1.0)
        theta = (
            -(spot * discount_q * pdf_d1 * volatility) / (2.0 * sqrt_t)
            + risk_free_rate * strike * discount_r * _norm_cdf(-d2)
            - dividend_yield * spot * discount_q * _norm_cdf(-d1)
        )

    gamma = discount_q * pdf_d1 / (spot * volatility * sqrt_t)
    vega = spot * discount_q * pdf_d1 * sqrt_t
    return BlackScholesGreeks(
        option_type=option_type,
        value=float(value),
        delta=float(delta),
        gamma=float(gamma),
        vega=float(vega),
        theta=float(theta),
        d1=float(d1),
        d2=float(d2),
        time_to_expiry_years=float(time_to_expiry_years),
        volatility=float(volatility),
        risk_free_rate=float(risk_free_rate),
        dividend_yield=float(dividend_yield),
        day_count=DAY_COUNT,
        timestamp_convention=TIMESTAMP_CONVENTION,
        role=LOCAL_GREEKS_ROLE,
    )


def diagnostic_value_greeks_from_leg(
    leg: OptionLeg,
    *,
    snapshot_ts_ms: int,
    underlying_price: float,
    volatility: float | None = None,
) -> BlackScholesGreeks:
    """Return local diagnostics for one quoted leg without producing a fill price."""

    vol = (float(leg.bid_iv) + float(leg.ask_iv)) / 2.0 if volatility is None else volatility
    return black_scholes_value_greeks(
        option_type=leg.option_type,
        spot=underlying_price,
        strike=leg.strike,
        time_to_expiry_years=year_fraction_act_365(
            snapshot_ts_ms=snapshot_ts_ms,
            expiry_ms=leg.expiry_ms,
        ),
        volatility=vol,
        risk_free_rate=DEFAULT_RISK_FREE_RATE,
        dividend_yield=DEFAULT_DIVIDEND_YIELD,
    )


def nearest_by_abs_then_value[T: (int, float)](candidates: Iterable[T], target: float) -> T:
    """Return nearest candidate, tie-broken by the smaller value."""

    values = tuple(candidates)
    if not values:
        raise ValueError("candidates must be non-empty")
    target = _require_finite("target", target)
    return min(values, key=lambda value: (abs(float(value) - target), float(value)))


def _norm_cdf(value: float) -> float:
    return 0.5 * (1.0 + math.erf(value / _SQRT_2))


def _norm_pdf(value: float) -> float:
    return _INV_SQRT_2PI * math.exp(-0.5 * value * value)


def _require_int(name: str, value: int, *, positive: bool = False) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError(f"{name} must be an integer")
    if positive and value <= 0:
        raise ValueError(f"{name} must be positive")
    if not positive and value < 0:
        raise ValueError(f"{name} must be non-negative")
    return value


def _require_finite(name: str, value: float) -> float:
    value = float(value)
    if not math.isfinite(value):
        raise ValueError(f"{name} must be finite")
    return value


def _require_positive(name: str, value: float) -> float:
    value = _require_finite(name, value)
    if value <= 0.0:
        raise ValueError(f"{name} must be positive")
    return value


__all__ = [
    "BlackScholesGreeks",
    "DAY_COUNT",
    "DEFAULT_DIVIDEND_YIELD",
    "DEFAULT_RISK_FREE_RATE",
    "LOCAL_GREEK_PROVENANCE",
    "LOCAL_GREEKS_ROLE",
    "TIMESTAMP_CONVENTION",
    "black_scholes_value_greeks",
    "diagnostic_value_greeks_from_leg",
    "nearest_by_abs_then_value",
    "year_fraction_act_365",
]

"""Per-chain transaction-cost model: when a position is too small / a rebalance too churny.

At $500-2000, gas is not a rounding error: a $50 position on Ethereum that costs ~$30 to enter and
exit must run for years just to break even. There is no clean free per-chain *live* gas oracle, so
costs here are explicit, documented, conservative round-trip (enter + exit) estimates per chain —
same "frozen, auditable constant" discipline as the haircuts. Override any of them, or the default,
via `chain_costs=` if your real costs differ.

A round-trip cost turns into two decisions:
  - breakeven_days: how long a position must earn its net APY just to repay entry+exit gas;
  - cost_drag_apy: that same cost amortised over a holding horizon, expressed as an APY drag.
"""

from __future__ import annotations

# Conservative round-trip (enter + exit) USD cost per chain. Lowercase chain keys.
CHAIN_ROUND_TRIP_USD: dict[str, float] = {
    "ethereum": 30.0,
    "arbitrum": 1.5,
    "optimism": 1.5,
    "base": 1.0,
    "polygon": 0.5,
    "bsc": 0.6,
    "avalanche": 1.0,
    "fantom": 0.3,
    "gnosis": 0.3,
    "solana": 0.1,
    "tron": 2.0,
    "sui": 0.2,
    "aptos": 0.2,
    "ton": 0.3,
    "scroll": 1.0,
    "linea": 1.0,
    "zksync era": 1.0,
    "mantle": 0.5,
    "blast": 1.0,
    "mode": 1.0,
    "metis": 0.5,
    "celo": 0.3,
}
DEFAULT_ROUND_TRIP_USD = 8.0  # unknown chain: assume a moderate L1/L2 cost (conservative)
_DAYS_PER_YEAR = 365.0


def round_trip_cost(chain: str, *, chain_costs: dict[str, float] | None = None) -> float:
    """Estimated USD to enter AND exit one position on `chain` (case-insensitive)."""
    table = chain_costs if chain_costs is not None else CHAIN_ROUND_TRIP_USD
    return table.get(chain.strip().lower(), DEFAULT_ROUND_TRIP_USD)


def annual_yield_usd(position_usd: float, net_apy_pct: float) -> float:
    """Expected USD/year from a position at a given net APY."""
    return max(0.0, position_usd) * max(0.0, net_apy_pct) / 100.0


def breakeven_days(position_usd: float, net_apy_pct: float, cost_usd: float) -> float:
    """Days the position must earn its net APY to repay `cost_usd`. inf if it earns nothing."""
    annual = annual_yield_usd(position_usd, net_apy_pct)
    if annual <= 0.0:
        return float("inf")
    return cost_usd / (annual / _DAYS_PER_YEAR)


def cost_drag_apy(position_usd: float, cost_usd: float, horizon_days: float) -> float:
    """The round-trip cost amortised over `horizon_days`, expressed as an APY drag (percent)."""
    if position_usd <= 0.0 or horizon_days <= 0.0:
        return 0.0
    return (cost_usd / position_usd) * (_DAYS_PER_YEAR / horizon_days) * 100.0


def worth_moving(
    delta_usd: float,
    net_apy_pct: float,
    cost_usd: float,
    *,
    payback_days: float = 90.0,
) -> bool:
    """Is shifting `delta_usd` into a `net_apy_pct` pool worth `cost_usd` within `payback_days`?

    True iff the incremental yield on the moved capital repays the round-trip cost inside the
    payback window — the churn guard that keeps a small account from bleeding out on gas.
    """
    annual = annual_yield_usd(abs(delta_usd), net_apy_pct)
    if annual <= 0.0:
        return False
    return cost_usd <= annual * (payback_days / _DAYS_PER_YEAR)

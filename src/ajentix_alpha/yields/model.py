"""Deterministic, conservative risk-adjusted ranking of DeFi yield pools.

Design stance (the discipline carried from ajentix-quant): a quoted APY is NOT an expected return.
We never rank by raw APY. We compute a *conservative net APY* by haircutting the non-sticky reward
portion, capping spot above its 30d mean (anti-spike), and haircutting impermanent-loss exposure;
then we hard-gate on liquidity / history / outlier, attach explicit risk flags, and split a
capital-preservation CORE from a hard-capped higher-risk SATELLITE. Every haircut is an explicit,
documented constant — no hidden optimism.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any

# --- frozen, documented risk constants ------------------------------------------------------------
MIN_TVL_USD = 1_000_000.0  # below this, exit liquidity is unreliable -> excluded
CORE_MIN_TVL_USD = 25_000_000.0  # core (capital-preservation) demands deep liquidity
MIN_HISTORY_DAYS = 21  # too little history -> APY estimate untrustworthy -> excluded
REWARD_STICKINESS = 0.5  # keep only 50% of reward APR (emissions decay + reward-token risk)
SPIKE_RATIO = 1.5  # spot apy > 1.5x its 30d mean -> treat as a reverting spike
REWARD_DEPENDENT_SHARE = 0.5  # reward share above this -> flag REWARD_DEPENDENT
UNSTABLE_CV = 0.5  # sigma/mu above this -> flag UNSTABLE
IL_FACTOR_STABLE_MULTI = 0.85  # both-stable multi-asset: small IL haircut
IL_FACTOR_VOLATILE = 0.6  # volatile IL exposure: large haircut
SIGMA_FLOOR = 0.1  # avoid divide-by-zero in stability


@dataclass(frozen=True, kw_only=True)
class Pool:
    pool_id: str
    chain: str
    project: str
    symbol: str
    tvl_usd: float
    apy: float
    apy_base: float
    apy_reward: float
    apy_mean_30d: float
    mu: float
    sigma: float
    count: int
    stablecoin: bool
    il_risk: str
    exposure: str
    outlier: bool
    reward_tokens: tuple[str, ...]


@dataclass(frozen=True, kw_only=True)
class ScoredPool:
    pool: Pool
    tier: str  # "core" | "satellite"
    net_apy: float  # conservative expected APY after all haircuts (the ranking key)
    reward_haircut_apy: float  # apy after only the reward-stickiness haircut
    il_factor: float
    flags: tuple[str, ...]


def _num(row: dict[str, Any], key: str, default: float = 0.0) -> float:
    v = row.get(key)
    if v is None or isinstance(v, bool):
        return default
    if isinstance(v, (int, float)) and math.isfinite(float(v)):
        return float(v)
    return default


def parse_pool(row: dict[str, Any]) -> Pool:
    """Parse one raw DefiLlama pool row, coercing safely (missing numerics -> 0/defaults)."""
    reward = row.get("rewardTokens") or ()
    return Pool(
        pool_id=str(row.get("pool", "")),
        chain=str(row.get("chain", "")),
        project=str(row.get("project", "")),
        symbol=str(row.get("symbol", "")),
        tvl_usd=_num(row, "tvlUsd"),
        apy=_num(row, "apy"),
        apy_base=_num(row, "apyBase"),
        apy_reward=_num(row, "apyReward"),
        apy_mean_30d=_num(row, "apyMean30d"),
        mu=_num(row, "mu"),
        sigma=_num(row, "sigma"),
        count=int(_num(row, "count")),
        stablecoin=bool(row.get("stablecoin", False)),
        il_risk=str(row.get("ilRisk", "")),
        exposure=str(row.get("exposure", "")),
        outlier=bool(row.get("outlier", False)),
        reward_tokens=tuple(str(t) for t in reward) if isinstance(reward, list) else (),
    )


def _il_factor(pool: Pool) -> float:
    if pool.il_risk != "yes" and pool.exposure != "multi":
        return 1.0
    return IL_FACTOR_STABLE_MULTI if pool.stablecoin else IL_FACTOR_VOLATILE


def passes_universe(pool: Pool) -> bool:
    """Hard inclusion gate. Anything failing is not tradeable/trustworthy enough to rank."""
    return (
        pool.tvl_usd >= MIN_TVL_USD
        and pool.count >= MIN_HISTORY_DAYS
        and not pool.outlier
        and math.isfinite(pool.apy)
        and pool.apy > 0.0
    )


def score_pool(pool: Pool) -> ScoredPool:
    """Conservative net APY + tier + flags. Pure and deterministic."""
    reward_share = pool.apy_reward / pool.apy if pool.apy > 0 else 0.0
    reward_share = min(max(reward_share, 0.0), 1.0)
    # Haircut only the reward portion of the *current* apy.
    reward_haircut_apy = pool.apy * (1.0 - reward_share * (1.0 - REWARD_STICKINESS))
    # Anti-spike: do not trust spot above its 30d mean (when a mean exists).
    anti_spike = (
        min(reward_haircut_apy, pool.apy_mean_30d) if pool.apy_mean_30d > 0 else reward_haircut_apy
    )
    il_factor = _il_factor(pool)
    net_apy = max(0.0, anti_spike * il_factor)

    flags: list[str] = []
    if reward_share > REWARD_DEPENDENT_SHARE:
        flags.append("REWARD_DEPENDENT")
    if pool.apy_mean_30d > 0 and pool.apy > SPIKE_RATIO * pool.apy_mean_30d:
        flags.append("SPIKE")
    if pool.mu > 0 and (pool.sigma / pool.mu) > UNSTABLE_CV:
        flags.append("UNSTABLE")
    if il_factor < 1.0:
        flags.append("IL_EXPOSED")
    if pool.tvl_usd < CORE_MIN_TVL_USD:
        flags.append("THIN_TVL")

    is_core = (
        pool.stablecoin
        and pool.il_risk == "no"
        and pool.tvl_usd >= CORE_MIN_TVL_USD
        and "UNSTABLE" not in flags
        and "SPIKE" not in flags
        and "REWARD_DEPENDENT" not in flags
    )
    tier = "core" if is_core else "satellite"
    return ScoredPool(
        pool=pool,
        tier=tier,
        net_apy=net_apy,
        reward_haircut_apy=reward_haircut_apy,
        il_factor=il_factor,
        flags=tuple(flags),
    )


def rank_pools(rows: list[dict[str, Any]]) -> list[ScoredPool]:
    """Parse -> universe-filter -> score -> sort by conservative net APY (desc), id tiebreak."""
    scored = [score_pool(p) for p in (parse_pool(r) for r in rows) if passes_universe(p)]
    scored.sort(key=lambda s: (-s.net_apy, s.pool.pool_id))
    return scored

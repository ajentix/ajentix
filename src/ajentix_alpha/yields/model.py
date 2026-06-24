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
import time
from dataclasses import dataclass
from typing import Any

from .prices import coin_key

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
# --- optional price-risk (depeg) constants; only used when a price snapshot is supplied -----------
PEG_WATCH_DEV = 0.005  # |price-1| >= 0.5% -> DEPEG_WATCH (soft, linear haircut begins)
PEG_BREAK_DEV = 0.02  # |price-1| >= 2% -> DEPEG (hard: net APY zeroed, never CORE)
PEG_MIN_CONFIDENCE = 0.9  # ignore prices below this oracle confidence (cannot verify)
# --- optional protocol-risk constants; only used when a protocols snapshot is supplied ------------
YOUNG_PROTOCOL_DAYS = 180  # listed under ~6 months ago -> YOUNG_PROTOCOL (less battle-tested)
_SECONDS_PER_DAY = 86_400.0


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
    underlying_tokens: tuple[str, ...] = ()


@dataclass(frozen=True, kw_only=True)
class ScoredPool:
    pool: Pool
    tier: str  # "core" | "satellite"
    net_apy: float  # conservative expected APY after all haircuts (the ranking key)
    reward_haircut_apy: float  # apy after only the reward-stickiness haircut
    il_factor: float
    flags: tuple[str, ...]
    peg_deviation: float = 0.0  # worst |price-1| across underlying stables (0 = none / unverified)


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
    underlying = row.get("underlyingTokens") or ()
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
        underlying_tokens=tuple(str(t) for t in underlying) if isinstance(underlying, list) else (),
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


def _peg_assessment(
    pool: Pool, prices: dict[str, dict[str, Any]]
) -> tuple[float, float, list[str]]:
    """Worst peg deviation across a stablecoin pool's underlying tokens, as a net-APY haircut.

    Returns (peg_factor, peg_deviation, flags). Non-stablecoin pools, and prices that are missing or
    below PEG_MIN_CONFIDENCE, are skipped (no adjustment -> no false alarm). peg_factor in [0,1]:
    1.0 below the watch threshold, linear down to 0.0 at the break threshold (a broken peg is
    principal loss, not a yield opportunity).
    """
    if not pool.stablecoin or not prices:
        return 1.0, 0.0, []
    worst = 0.0
    verified = False
    for addr in pool.underlying_tokens:
        if not addr or addr.startswith("0x0000000000000000000000000000000000000000"):
            continue
        info = prices.get(coin_key(pool.chain, addr))
        if not isinstance(info, dict):
            continue
        price = info.get("price")
        conf = info.get("confidence", 1.0)
        if not isinstance(price, (int, float)):
            continue
        if isinstance(conf, (int, float)) and conf < PEG_MIN_CONFIDENCE:
            continue
        verified = True
        worst = max(worst, abs(float(price) - 1.0))
    if not verified:
        return 1.0, 0.0, []
    if worst >= PEG_BREAK_DEV:
        return 0.0, worst, ["DEPEG"]
    if worst >= PEG_WATCH_DEV:
        factor = (PEG_BREAK_DEV - worst) / (PEG_BREAK_DEV - PEG_WATCH_DEV)
        return max(0.0, min(1.0, factor)), worst, ["DEPEG_WATCH"]
    return 1.0, worst, []


def _protocol_risk(pool: Pool, protocols: dict[str, dict[str, Any]], now_ts: float) -> list[str]:
    """Protocol flags from audit count + listing age. UNKNOWN_PROTOCOL when the slug is absent."""
    info = protocols.get(pool.project)
    if not isinstance(info, dict):
        return ["UNKNOWN_PROTOCOL"]
    flags: list[str] = []
    audits = info.get("audits")
    try:
        n_audits = int(audits) if audits is not None else 0
    except (TypeError, ValueError):
        n_audits = 0
    if n_audits <= 0:
        flags.append("UNAUDITED")
    listed = info.get("listedAt")
    if (
        isinstance(listed, (int, float))
        and not isinstance(listed, bool)
        and listed > 0
        and (now_ts - float(listed)) / _SECONDS_PER_DAY < YOUNG_PROTOCOL_DAYS
    ):
        flags.append("YOUNG_PROTOCOL")
    return flags


def score_pool(
    pool: Pool,
    *,
    prices: dict[str, dict[str, Any]] | None = None,
    protocols: dict[str, dict[str, Any]] | None = None,
    now_ts: float | None = None,
) -> ScoredPool:
    """Conservative net APY + tier + flags. Pure/deterministic.

    Optional price/protocol snapshots add depeg + protocol-risk flags and haircuts when supplied;
    with neither (the default), behaviour is identical to the yields-only model.
    """
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

    # Optional price-risk (depeg) haircut.
    peg_factor, peg_dev, peg_flags = _peg_assessment(pool, prices or {})
    flags.extend(peg_flags)
    net_apy = max(0.0, net_apy * peg_factor)
    # Optional protocol-risk flags.
    if protocols is not None:
        flags.extend(_protocol_risk(pool, protocols, now_ts if now_ts is not None else time.time()))

    is_core = (
        pool.stablecoin
        and pool.il_risk == "no"
        and pool.tvl_usd >= CORE_MIN_TVL_USD
        and "UNSTABLE" not in flags
        and "SPIKE" not in flags
        and "REWARD_DEPENDENT" not in flags
        and "DEPEG" not in flags
        and "DEPEG_WATCH" not in flags
        and "UNAUDITED" not in flags
        and "YOUNG_PROTOCOL" not in flags
    )
    tier = "core" if is_core else "satellite"
    return ScoredPool(
        pool=pool,
        tier=tier,
        net_apy=net_apy,
        reward_haircut_apy=reward_haircut_apy,
        il_factor=il_factor,
        flags=tuple(flags),
        peg_deviation=peg_dev,
    )


def rank_pools(
    rows: list[dict[str, Any]],
    *,
    prices: dict[str, dict[str, Any]] | None = None,
    protocols: dict[str, dict[str, Any]] | None = None,
    now_ts: float | None = None,
) -> list[ScoredPool]:
    """Parse -> universe-filter -> score -> sort by conservative net APY (desc), id tiebreak."""
    scored = [
        score_pool(p, prices=prices, protocols=protocols, now_ts=now_ts)
        for p in (parse_pool(r) for r in rows)
        if passes_universe(p)
    ]
    scored.sort(key=lambda s: (-s.net_apy, s.pool.pool_id))
    return scored

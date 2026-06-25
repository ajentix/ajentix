"""Calibration of the conservative net-APY model against what pools actually did over time.

These are forward bets, not OOS-backtestable price edges, so this is NOT a backtest and makes no
claim of predictive edge. It is an honest *calibration check*: take an earlier snapshot and a later
one, and for pools present in both ask whether the model's claims held up —

  - conservatism: did realized APY come in at or above the conservative net APY we quoted? (the
    haircuts are meant to under-promise, so a high conservatism rate is the model working, not luck)
  - spike reversion: did pools we flagged SPIKE actually fall back toward their 30d mean?
  - liquidity persistence: did CORE TVL hold up better than SATELLITE TVL?
  - survival: what fraction of ranked pools were still in the feed later?

Pure and deterministic given two snapshots. Over a short retained window the signal is weak; the
report says so. The point is a feedback loop, not a performance claim.
"""

from __future__ import annotations

import statistics
from dataclasses import dataclass
from typing import Any

from .model import ScoredPool, parse_pool, passes_universe, score_pool


@dataclass(frozen=True, kw_only=True)
class PoolOutcome:
    pool_id: str
    project: str
    symbol: str
    tier: str
    predicted_net_apy: float  # conservative net APY quoted at baseline
    realized_apy: float  # actual quoted APY in the later snapshot
    signed_error: float  # realized - predicted (positive = we under-promised, as intended)
    flags: tuple[str, ...]


@dataclass(frozen=True, kw_only=True)
class CalibrationReport:
    baseline_ranked: int
    matched: int  # baseline-ranked pools also present later
    survival_rate: float
    conservatism_rate: float  # fraction of matched pools where realized >= predicted net APY
    median_signed_error: float  # realized - predicted, median over matched
    mean_signed_error: float
    spike_count: int
    spike_reversion_rate: float  # fraction of SPIKE pools whose later APY fell vs baseline APY
    core_tvl_median_change_pct: float
    satellite_tvl_median_change_pct: float
    worst_overpredictions: tuple[PoolOutcome, ...]  # most negative signed errors (model too rosy)


def _index_ranked(rows: list[dict[str, Any]]) -> dict[str, ScoredPool]:
    return {s.pool.pool_id: s for s in (score_pool(parse_pool(r)) for r in rows) if _ok(s)}


def _ok(s: ScoredPool) -> bool:
    return bool(s.pool.pool_id) and passes_universe(s.pool)


def _index_all(rows: list[dict[str, Any]]) -> dict[str, ScoredPool]:
    out: dict[str, ScoredPool] = {}
    for r in rows:
        s = score_pool(parse_pool(r))
        if s.pool.pool_id:
            out[s.pool.pool_id] = s
    return out


def _median(values: list[float]) -> float:
    return statistics.median(values) if values else 0.0


def calibrate(
    prev_rows: list[dict[str, Any]], cur_rows: list[dict[str, Any]], *, sample: int = 15
) -> CalibrationReport:
    """Compare a baseline snapshot's ranked pools against their realized state later."""
    baseline = _index_ranked(prev_rows)  # only pools we would have ranked at baseline
    later = _index_all(cur_rows)  # all later pools (survivors visible even if now sub-universe)

    outcomes: list[PoolOutcome] = []
    core_tvl_changes: list[float] = []
    sat_tvl_changes: list[float] = []
    spike_total = 0
    spike_reverted = 0
    for pool_id, b in baseline.items():
        c = later.get(pool_id)
        if c is None:
            continue
        realized = c.pool.apy
        outcomes.append(
            PoolOutcome(
                pool_id=pool_id,
                project=b.pool.project,
                symbol=b.pool.symbol,
                tier=b.tier,
                predicted_net_apy=b.net_apy,
                realized_apy=realized,
                signed_error=realized - b.net_apy,
                flags=b.flags,
            )
        )
        if b.pool.tvl_usd > 0:
            change = (c.pool.tvl_usd - b.pool.tvl_usd) / b.pool.tvl_usd * 100.0
            (core_tvl_changes if b.tier == "core" else sat_tvl_changes).append(change)
        if "SPIKE" in b.flags:
            spike_total += 1
            if c.pool.apy < b.pool.apy:
                spike_reverted += 1

    matched = len(outcomes)
    errors = [o.signed_error for o in outcomes]
    conservative = sum(1 for e in errors if e >= 0.0)
    worst = sorted(outcomes, key=lambda o: o.signed_error)[:sample]
    return CalibrationReport(
        baseline_ranked=len(baseline),
        matched=matched,
        survival_rate=matched / len(baseline) if baseline else 0.0,
        conservatism_rate=conservative / matched if matched else 0.0,
        median_signed_error=_median(errors),
        mean_signed_error=(sum(errors) / matched if matched else 0.0),
        spike_count=spike_total,
        spike_reversion_rate=spike_reverted / spike_total if spike_total else 0.0,
        core_tvl_median_change_pct=_median(core_tvl_changes),
        satellite_tvl_median_change_pct=_median(sat_tvl_changes),
        worst_overpredictions=tuple(worst),
    )

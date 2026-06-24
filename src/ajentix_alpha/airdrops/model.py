"""Deterministic EV model for airdrop / points farming (the planned satellite module).

There is no reliable FREE feed of live airdrop data, so this module does NOT pretend to scrape one.
Instead it enforces discipline on YOUR modeled inputs: for each campaign you supply a capital
amount, lock period, a modeled gross airdrop value, the probability it actually pays out to you,
your entry/exit/claim costs, and a confidence level. The model then computes a probability- and
confidence-haircut expected value, subtracts costs AND the safe yield you forgo by locking capital,
and ranks by capital efficiency (annualized EV per dollar).

The key cross-check: net EV is expressed *in excess of the opportunity cost* of the safe baseline
yield. So NET_EV < 0 means literally "you would be better off parking this capital in the CORE
stablecoin pool" — the airdrop bet is not worth the lock-up. The agent models; the user farms.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any

# --- frozen, documented EV constants --------------------------------------------------------------
CONFIDENCE_HAIRCUT = {"high": 0.9, "med": 0.7, "low": 0.5}  # extra haircut on your modeled value
DEFAULT_CONFIDENCE = "low"  # absent confidence is treated as low (conservative)
LOW_PROBABILITY = 0.2  # probability below this -> LOW_PROBABILITY flag (lottery ticket)
LONG_LOCK_DAYS = 180  # lock at/above this -> LONG_LOCK flag (illiquidity risk)
DEADLINE_SOON_DAYS = 7  # actionable deadline within this many days -> DEADLINE_SOON flag
_DEFAULT_ANNUALIZE_DAYS = 365  # no lock and no deadline -> annualize a one-shot bet over a year


@dataclass(frozen=True, kw_only=True)
class Campaign:
    name: str
    chain: str
    capital_usd: float  # capital you must deploy/lock to qualify
    lock_days: int  # days the capital is locked (0 = liquid, no opportunity cost)
    est_airdrop_usd: float  # YOUR modeled gross airdrop value (the central judgment)
    probability: float  # P(airdrop happens AND you qualify AND it's claimable), 0..1
    cost_usd: float  # gas + bridge + ops to enter, maintain, and claim
    confidence: str  # "high" | "med" | "low" -> haircut on est_airdrop_usd
    deadline_days: int  # days until you must act/claim (0 = unknown/none)


@dataclass(frozen=True, kw_only=True)
class ScoredCampaign:
    campaign: Campaign
    expected_gross_usd: float  # probability * est value * confidence haircut
    opportunity_cost_usd: float  # safe yield forgone while capital is locked
    net_ev_usd: float  # expected_gross - costs - opportunity_cost (excess over the safe baseline)
    ev_per_dollar: float  # net_ev / capital deployed
    annualized_ev_pct: float  # net_ev as an annualized % of capital (capital-efficiency rank key)
    confidence_haircut: float
    flags: tuple[str, ...]


def _num(row: dict[str, Any], key: str, default: float = 0.0) -> float:
    v = row.get(key)
    if v is None or isinstance(v, bool):
        return default
    if isinstance(v, (int, float)) and math.isfinite(float(v)):
        return float(v)
    return default


def parse_campaign(row: dict[str, Any]) -> Campaign:
    """Parse one user-supplied campaign row, coercing safely (missing -> conservative defaults)."""
    conf = str(row.get("confidence", DEFAULT_CONFIDENCE)).lower()
    if conf not in CONFIDENCE_HAIRCUT:
        conf = DEFAULT_CONFIDENCE
    return Campaign(
        name=str(row.get("name", "")),
        chain=str(row.get("chain", "")),
        capital_usd=max(0.0, _num(row, "capital_usd")),
        lock_days=int(max(0.0, _num(row, "lock_days"))),
        est_airdrop_usd=max(0.0, _num(row, "est_airdrop_usd")),
        probability=min(max(_num(row, "probability"), 0.0), 1.0),
        cost_usd=max(0.0, _num(row, "cost_usd")),
        confidence=conf,
        deadline_days=int(max(0.0, _num(row, "deadline_days"))),
    )


def score_campaign(campaign: Campaign, *, baseline_apy_pct: float) -> ScoredCampaign:
    """Risk-adjusted EV of a single campaign against the safe baseline yield. Pure/deterministic."""
    haircut = CONFIDENCE_HAIRCUT[campaign.confidence]
    expected_gross = campaign.probability * campaign.est_airdrop_usd * haircut
    opportunity_cost = (
        campaign.capital_usd * (baseline_apy_pct / 100.0) * (campaign.lock_days / 365.0)
    )
    net_ev = expected_gross - campaign.cost_usd - opportunity_cost
    ev_per_dollar = net_ev / campaign.capital_usd if campaign.capital_usd > 0 else 0.0
    horizon = campaign.lock_days or campaign.deadline_days or _DEFAULT_ANNUALIZE_DAYS
    annualized_ev_pct = ev_per_dollar * (365.0 / horizon) * 100.0

    flags: list[str] = []
    if net_ev < 0:
        flags.append("NEGATIVE_EV")  # loses vs simply earning the safe baseline yield
    if campaign.probability < LOW_PROBABILITY:
        flags.append("LOW_PROBABILITY")
    if campaign.lock_days >= LONG_LOCK_DAYS:
        flags.append("LONG_LOCK")
    if 0 < campaign.deadline_days <= DEADLINE_SOON_DAYS:
        flags.append("DEADLINE_SOON")
    if campaign.confidence == "low":
        flags.append("LOW_CONFIDENCE")
    return ScoredCampaign(
        campaign=campaign,
        expected_gross_usd=expected_gross,
        opportunity_cost_usd=opportunity_cost,
        net_ev_usd=net_ev,
        ev_per_dollar=ev_per_dollar,
        annualized_ev_pct=annualized_ev_pct,
        confidence_haircut=haircut,
        flags=tuple(flags),
    )


def rank_campaigns(rows: list[dict[str, Any]], *, baseline_apy_pct: float) -> list[ScoredCampaign]:
    """Parse -> score -> sort by capital efficiency (annualized EV %, then net EV $) descending."""
    scored = [score_campaign(parse_campaign(r), baseline_apy_pct=baseline_apy_pct) for r in rows]
    scored.sort(key=lambda s: (-s.annualized_ev_pct, -s.net_ev_usd, s.campaign.name))
    return scored

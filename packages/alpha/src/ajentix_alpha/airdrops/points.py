"""Track points-farming accrual over time and turn it into a capital-efficiency read.

The airdrop EV model scores a campaign *before* you enter; this tracks one you *already farm*.
You keep a dated log of point balances and the capital you had deployed; this computes accrual
velocity (points/day), capital efficiency (points per dollar-day), and — if you supply a modeled
value-per-point — an implied APY-equivalent so a farm can be compared against the CORE stablecoin
yield it is tying capital up against.

Every value-per-point is YOUR modeled estimate, not a quoted price. No log = nothing to report; a
single entry can show a balance but not a velocity. Pure and deterministic.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from typing import Any

STALLED_PPD = 1e-9  # points/day at or below this -> STALLED flag
_DAYS_PER_YEAR = 365.0


@dataclass(frozen=True, kw_only=True)
class PointsEntry:
    campaign: str
    day: int  # proleptic ordinal day (date.toordinal), for stable deltas
    points: float
    capital_usd: float
    value_per_point: float | None  # YOUR modeled $ per point (optional)


@dataclass(frozen=True, kw_only=True)
class CampaignPoints:
    campaign: str
    entries: int
    first_date: str
    last_date: str
    days_active: int
    start_points: float
    latest_points: float
    points_gained: float
    points_per_day: float
    latest_capital_usd: float
    capital_days: float  # sum of capital * days held over the logged intervals
    points_per_dollar_day: float
    value_per_point: float | None
    modeled_value_usd: float | None  # latest_points * value_per_point
    implied_apy_pct: float | None  # modeled value as an annualized % of capital-days
    flags: tuple[str, ...]


def _num(row: dict[str, Any], key: str) -> float:
    v = row.get(key)
    if isinstance(v, (int, float)) and not isinstance(v, bool):
        return float(v)
    return 0.0


def parse_entry(row: dict[str, Any]) -> PointsEntry:
    """Parse one log row; date is ISO 'YYYY-MM-DD'. value_per_point is optional."""
    vpp = row.get("value_per_point")
    vpp_val = float(vpp) if isinstance(vpp, (int, float)) and not isinstance(vpp, bool) else None
    return PointsEntry(
        campaign=str(row.get("campaign", "")),
        day=date.fromisoformat(str(row["date"])).toordinal(),
        points=max(0.0, _num(row, "points")),
        capital_usd=max(0.0, _num(row, "capital_usd")),
        value_per_point=vpp_val,
    )


def _summarize_one(campaign: str, rows: list[PointsEntry]) -> CampaignPoints:
    rows = sorted(rows, key=lambda e: e.day)
    first, last = rows[0], rows[-1]
    days_active = last.day - first.day
    gained = max(0.0, last.points - first.points)
    ppd = gained / days_active if days_active > 0 else 0.0
    # capital-days: capital reported at the start of each interval times the interval length.
    capital_days = sum(
        rows[i].capital_usd * (rows[i + 1].day - rows[i].day) for i in range(len(rows) - 1)
    )
    ppdd = gained / capital_days if capital_days > 0 else 0.0
    vpp = next((e.value_per_point for e in reversed(rows) if e.value_per_point is not None), None)
    modeled_value = last.points * vpp if vpp is not None else None
    implied_apy = (
        (modeled_value / capital_days) * _DAYS_PER_YEAR * 100.0
        if modeled_value is not None and capital_days > 0
        else None
    )

    flags: list[str] = []
    if len(rows) < 2:
        flags.append("SINGLE_ENTRY")
    elif ppd <= STALLED_PPD:
        flags.append("STALLED")
    if vpp is None:
        flags.append("NO_VALUATION")
    return CampaignPoints(
        campaign=campaign,
        entries=len(rows),
        first_date=date.fromordinal(first.day).isoformat(),
        last_date=date.fromordinal(last.day).isoformat(),
        days_active=days_active,
        start_points=first.points,
        latest_points=last.points,
        points_gained=gained,
        points_per_day=ppd,
        latest_capital_usd=last.capital_usd,
        capital_days=capital_days,
        points_per_dollar_day=ppdd,
        value_per_point=vpp,
        modeled_value_usd=modeled_value,
        implied_apy_pct=implied_apy,
        flags=tuple(flags),
    )


def summarize(rows: list[dict[str, Any]]) -> list[CampaignPoints]:
    """Group a points log by campaign; sort by implied APY then capital efficiency."""
    by_campaign: dict[str, list[PointsEntry]] = {}
    for r in rows:
        e = parse_entry(r)
        if e.campaign:
            by_campaign.setdefault(e.campaign, []).append(e)
    out = [_summarize_one(name, entries) for name, entries in by_campaign.items()]
    out.sort(
        key=lambda s: (-(s.implied_apy_pct or -1e18), -s.points_per_dollar_day, s.campaign)
    )
    return out

from __future__ import annotations

import math

from ajentix_alpha.yields import costs as c


def test_round_trip_known_and_unknown_chain() -> None:
    assert c.round_trip_cost("Ethereum") == c.CHAIN_ROUND_TRIP_USD["ethereum"]
    assert c.round_trip_cost("BASE") == c.CHAIN_ROUND_TRIP_USD["base"]  # case-insensitive
    assert c.round_trip_cost("SomeNewChain") == c.DEFAULT_ROUND_TRIP_USD


def test_round_trip_override() -> None:
    assert c.round_trip_cost("ethereum", chain_costs={"ethereum": 3.0}) == 3.0
    assert c.round_trip_cost("missing", chain_costs={"ethereum": 3.0}) == c.DEFAULT_ROUND_TRIP_USD


def test_annual_yield() -> None:
    assert abs(c.annual_yield_usd(1000.0, 10.0) - 100.0) < 1e-9
    assert c.annual_yield_usd(-5.0, 10.0) == 0.0  # clamped
    assert c.annual_yield_usd(1000.0, -10.0) == 0.0


def test_breakeven_days() -> None:
    # $1000 at 10% earns $100/yr ~= $0.274/day; $30 cost -> ~109.5 days.
    be = c.breakeven_days(1000.0, 10.0, 30.0)
    assert abs(be - (30.0 / (100.0 / 365.0))) < 1e-6
    # zero yield -> never breaks even
    assert math.isinf(c.breakeven_days(1000.0, 0.0, 30.0))


def test_cost_drag_apy() -> None:
    # $1000 position, $30 cost amortised over 365 days -> 3% APY drag.
    assert abs(c.cost_drag_apy(1000.0, 30.0, 365.0) - 3.0) < 1e-9
    assert c.cost_drag_apy(0.0, 30.0, 365.0) == 0.0
    assert c.cost_drag_apy(1000.0, 30.0, 0.0) == 0.0


def test_worth_moving_payback_window() -> None:
    # Move $1000 into 12% pool: $120/yr -> ~$29.6 over 90 days. $30 gas just misses.
    assert not c.worth_moving(1000.0, 12.0, 30.0, payback_days=90.0)
    # A longer window or cheaper gas makes it worth it.
    assert c.worth_moving(1000.0, 12.0, 30.0, payback_days=120.0)
    assert c.worth_moving(1000.0, 12.0, 1.0, payback_days=90.0)
    # No yield -> never worth it.
    assert not c.worth_moving(1000.0, 0.0, 1.0, payback_days=3650.0)

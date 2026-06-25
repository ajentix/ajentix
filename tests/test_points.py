from __future__ import annotations

from ajentix_alpha.airdrops import points as pts


def _e(campaign: str, d: str, p: float, cap: float, vpp: float | None = None) -> dict[str, object]:
    row: dict[str, object] = {"campaign": campaign, "date": d, "points": p, "capital_usd": cap}
    if vpp is not None:
        row["value_per_point"] = vpp
    return row


def test_velocity_and_capital_efficiency() -> None:
    rows = [
        _e("X", "2026-01-01", 0.0, 100.0, 0.01),
        _e("X", "2026-01-11", 1000.0, 100.0, 0.01),
    ]
    (s,) = pts.summarize(rows)
    assert s.days_active == 10
    assert abs(s.points_per_day - 100.0) < 1e-9
    assert abs(s.capital_days - 1000.0) < 1e-9  # 100 capital * 10 days
    assert abs(s.points_per_dollar_day - 1.0) < 1e-9
    assert s.modeled_value_usd is not None and s.implied_apy_pct is not None
    assert abs(s.modeled_value_usd - 10.0) < 1e-9  # 1000 points * 0.01
    assert abs(s.implied_apy_pct - 365.0) < 1e-6  # (10/1000)*365*100
    assert s.flags == ()


def test_no_valuation_flag() -> None:
    (s,) = pts.summarize([_e("Y", "2026-01-01", 0.0, 100.0), _e("Y", "2026-01-11", 500.0, 100.0)])
    assert "NO_VALUATION" in s.flags
    assert s.modeled_value_usd is None
    assert s.implied_apy_pct is None


def test_single_entry_flag() -> None:
    (s,) = pts.summarize([_e("Z", "2026-01-01", 100.0, 100.0, 0.01)])
    assert "SINGLE_ENTRY" in s.flags
    assert s.days_active == 0
    assert s.points_per_day == 0.0
    assert s.latest_points == 100.0


def test_stalled_flag() -> None:
    (s,) = pts.summarize(
        [_e("S", "2026-01-01", 500.0, 100.0, 0.01), _e("S", "2026-01-21", 500.0, 100.0, 0.01)]
    )
    assert "STALLED" in s.flags
    assert s.points_gained == 0.0


def test_entries_sorted_by_date_regardless_of_input_order() -> None:
    # Out-of-order input must still compute from the true first/last dates.
    (s,) = pts.summarize(
        [
            _e("X", "2026-01-11", 1000.0, 100.0, 0.01),
            _e("X", "2026-01-01", 0.0, 100.0, 0.01),
        ]
    )
    assert s.first_date == "2026-01-01"
    assert s.last_date == "2026-01-11"
    assert s.start_points == 0.0
    assert s.latest_points == 1000.0


def test_ranked_by_implied_apy_then_valuation_last() -> None:
    rows = [
        _e("low", "2026-01-01", 0.0, 100.0, 0.001),
        _e("low", "2026-01-11", 100.0, 100.0, 0.001),
        _e("high", "2026-01-01", 0.0, 100.0, 0.1),
        _e("high", "2026-01-11", 100.0, 100.0, 0.1),
        _e("none", "2026-01-01", 0.0, 100.0),
        _e("none", "2026-01-11", 100.0, 100.0),
    ]
    order = [s.campaign for s in pts.summarize(rows)]
    assert order.index("high") < order.index("low") < order.index("none")

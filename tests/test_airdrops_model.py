from __future__ import annotations

from ajentix_alpha.airdrops import model as a


def _row(**kw: object) -> dict[str, object]:
    base: dict[str, object] = {
        "name": "demo",
        "chain": "Ethereum",
        "capital_usd": 1000.0,
        "lock_days": 0,
        "est_airdrop_usd": 1000.0,
        "probability": 1.0,
        "cost_usd": 0.0,
        "confidence": "high",
        "deadline_days": 0,
    }
    base.update(kw)
    return base


def test_parse_clamps_and_defaults() -> None:
    c = a.parse_campaign(
        {"name": "x", "probability": 5.0, "confidence": "bogus", "capital_usd": -3}
    )
    assert c.probability == 1.0  # clamped to [0,1]
    assert c.confidence == a.DEFAULT_CONFIDENCE  # unknown -> conservative default
    assert c.capital_usd == 0.0  # negative -> 0


def test_expected_gross_applies_probability_and_confidence_haircut() -> None:
    # est 1000, p 0.5, confidence med (0.7) -> 1000*0.5*0.7 = 350
    s = a.score_campaign(
        a.parse_campaign(_row(est_airdrop_usd=1000.0, probability=0.5, confidence="med")),
        baseline_apy_pct=0.0,
    )
    assert abs(s.expected_gross_usd - 350.0) < 1e-9


def test_opportunity_cost_scales_with_lock_and_baseline() -> None:
    # 1000 capital, 10% baseline, locked 365d -> opportunity cost 100
    s = a.score_campaign(
        a.parse_campaign(_row(lock_days=365, est_airdrop_usd=0.0, probability=0.0)),
        baseline_apy_pct=10.0,
    )
    assert abs(s.opportunity_cost_usd - 100.0) < 1e-9
    # No airdrop value, so net EV is purely the forgone yield (negative).
    assert abs(s.net_ev_usd + 100.0) < 1e-9
    assert "NEGATIVE_EV" in s.flags


def test_no_lock_means_no_opportunity_cost() -> None:
    s = a.score_campaign(a.parse_campaign(_row(lock_days=0)), baseline_apy_pct=50.0)
    assert s.opportunity_cost_usd == 0.0


def test_net_ev_nets_costs_and_opportunity() -> None:
    # gross 1000*1.0*0.9=900, cost 50, opp = 1000*0.1*(180/365)
    s = a.score_campaign(
        a.parse_campaign(_row(cost_usd=50.0, lock_days=180)),
        baseline_apy_pct=10.0,
    )
    opp = 1000.0 * 0.10 * (180.0 / 365.0)
    assert abs(s.net_ev_usd - (900.0 - 50.0 - opp)) < 1e-9


def test_negative_ev_flag_when_below_baseline() -> None:
    # Small airdrop vs a long expensive lock -> worse than the safe yield.
    s = a.score_campaign(
        a.parse_campaign(
            _row(est_airdrop_usd=20.0, probability=0.3, lock_days=365, cost_usd=30.0)
        ),
        baseline_apy_pct=10.0,
    )
    assert "NEGATIVE_EV" in s.flags
    assert s.net_ev_usd < 0
    assert s.annualized_ev_pct < 0


def test_low_probability_and_long_lock_and_deadline_flags() -> None:
    s = a.score_campaign(
        a.parse_campaign(_row(probability=0.1, lock_days=200, deadline_days=3, confidence="low")),
        baseline_apy_pct=5.0,
    )
    assert {"LOW_PROBABILITY", "LONG_LOCK", "DEADLINE_SOON", "LOW_CONFIDENCE"} <= set(s.flags)


def test_annualized_ev_uses_lock_then_deadline_then_year() -> None:
    # Liquid one-shot (no lock, no deadline): annualized over a full year == ev_per_dollar*100.
    s = a.score_campaign(
        a.parse_campaign(_row(est_airdrop_usd=100.0, probability=1.0, confidence="high")),
        baseline_apy_pct=0.0,
    )
    assert abs(s.annualized_ev_pct - s.ev_per_dollar * 100.0) < 1e-9


def test_rank_sorts_by_capital_efficiency() -> None:
    rows = [
        _row(name="slow", est_airdrop_usd=300.0, lock_days=365),
        _row(name="fast", est_airdrop_usd=300.0, lock_days=30),
    ]
    ranked = a.rank_campaigns(rows, baseline_apy_pct=0.0)
    # Same gross, shorter lock -> higher annualized EV -> ranked first.
    assert [s.campaign.name for s in ranked] == ["fast", "slow"]


def test_zero_capital_is_safe() -> None:
    s = a.score_campaign(a.parse_campaign(_row(capital_usd=0.0)), baseline_apy_pct=10.0)
    assert s.ev_per_dollar == 0.0
    assert s.annualized_ev_pct == 0.0

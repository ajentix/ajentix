from __future__ import annotations

import pytest

from ajentix_quant.backtest.option_costs import max_loss_from_width_credit_usd
from ajentix_quant.options import (
    DefinedRiskStructure,
    OptionLeg,
    OptionType,
    Side,
    SourceQuality,
    StructureType,
)
from ajentix_quant.risk.options_margin import (
    DefinedRiskMarginLimits,
    assert_defined_risk_margin,
    evaluate_defined_risk_margin,
)

EXPIRY_MS = 1_735_689_600_000
QUOTE_TS_MS = 1_733_000_000_000


def _leg(**overrides) -> OptionLeg:
    params = {
        "instrument_name": "ETH-20241227-3000-P",
        "underlying": "ETH",
        "contract_multiplier": 1.0,
        "option_type": OptionType.PUT,
        "side": Side.SHORT,
        "strike": 3000.0,
        "expiry_ms": EXPIRY_MS,
        "settlement_style": "european",
        "settlement_index": "deribit_eth_index",
        "premium_currency": "ETH",
        "fee_currency": "ETH",
        "collateral_currency": "USDC_or_ETH",
        "usd_conversion_source": "deribit_eth_index",
        "quote_ts_ms": QUOTE_TS_MS,
        "quote_age_s": 2.0,
        "bid_price": 30.0,
        "bid_amount": 4.0,
        "bid_iv": 0.55,
        "ask_price": 31.0,
        "ask_amount": 5.0,
        "ask_iv": 0.56,
        "mark_price": 30.5,
        "greek_provenance_key": "vendor_cached_hashed_preferred_else_local",
        "min_tick": 0.05,
        "min_lot": 1.0,
        "source_quality": SourceQuality.VENUE,
    }
    params.update(overrides)
    return OptionLeg(**params)


def _structure(*, width: float = 200.0, credit: float = 19.0, quantity: int = 1, **overrides):
    short_strike = 3000.0
    long_strike = short_strike - width
    short = _leg(
        instrument_name=f"ETH-20241227-{short_strike:.0f}-P",
        side=Side.SHORT,
        strike=short_strike,
        bid_price=credit + 11.0,
        ask_price=credit + 12.0,
    )
    long = _leg(
        instrument_name=f"ETH-20241227-{long_strike:.0f}-P",
        side=Side.LONG,
        strike=long_strike,
        bid_price=10.0,
        ask_price=11.0,
    )
    params = {
        "structure_type": StructureType.PUT_CREDIT_SPREAD,
        "legs": (short, long),
        "quantity": quantity,
        "entry_snapshot_id": "snapshot-eth-20241227",
        "expiry_ms": EXPIRY_MS,
        "dte_days": 30,
        "settlement_style": "european",
        "settlement_index": "deribit_eth_index",
        "premium_currency": "ETH",
        "fee_currency": "ETH",
        "collateral_currency": "USDC_or_ETH",
        "usd_conversion_source": "deribit_eth_index",
        "net_credit": credit,
        "width": width,
        "fees": 0.0,
        "max_loss_usd": max_loss_from_width_credit_usd(
            width=width,
            net_credit=credit,
            contract_multiplier=1.0,
            quantity=quantity,
        ),
        "max_gain_usd": credit * quantity,
        "entry_quote_ts_ms": QUOTE_TS_MS,
        "max_quote_age_s": 2.0,
        "frozen_param_key": "vrp-eth-credit-spread-grid-v1",
    }
    params.update(overrides)
    return DefinedRiskStructure(**params)


def test_max_loss_invariant_and_caps_accept_feasible_structure():
    structure = _structure(width=200.0, credit=19.0)
    result = evaluate_defined_risk_margin(structure, equity_usd=1000.0, taker_fee_bps=0.0)

    assert structure.max_loss_usd == pytest.approx(181.0)
    assert result.accepted is True
    assert result.reserve_usd == pytest.approx(250.0)
    assert result.per_structure_cap_usd == pytest.approx(250.0)
    assert result.aggregate_cap_usd == pytest.approx(400.0)
    assert result.structure_max_loss_usd == pytest.approx(181.0)
    assert result.max_authorized_quantity == 1


def test_structures_over_per_structure_cap_are_rejected():
    structure = _structure(width=500.0, credit=10.0)
    result = evaluate_defined_risk_margin(structure, equity_usd=1000.0, taker_fee_bps=0.0)

    assert result.accepted is False
    assert result.reason == "min_lot_width_exceeds_caps"
    with pytest.raises(ValueError, match="min_lot_width_exceeds_caps"):
        assert_defined_risk_margin(structure, equity_usd=1000.0, taker_fee_bps=0.0)


def test_aggregate_defined_risk_cap_is_enforced():
    structure = _structure(width=200.0, credit=19.0)
    result = evaluate_defined_risk_margin(
        structure,
        equity_usd=1000.0,
        aggregate_open_max_loss_usd=300.0,
        taker_fee_bps=0.0,
    )

    assert result.accepted is False
    assert result.reason == "aggregate_cap"
    assert result.aggregate_remaining_usd == pytest.approx(100.0)


def test_min_lot_rounding_rejects_quantity_below_ticket():
    base = _structure(width=100.0, credit=10.0)
    legs = tuple(_leg(**_leg_overrides(leg, min_lot=2.0)) for leg in base.legs)
    structure = _structure(width=100.0, credit=10.0, legs=legs)

    result = evaluate_defined_risk_margin(structure, equity_usd=1000.0, taker_fee_bps=0.0)

    assert result.accepted is False
    assert result.reason == "below_min_lot"
    assert result.minimum_lot_quantity == 2
    assert result.max_authorized_quantity == 2


def test_min_lot_width_exceeding_caps_is_rejected():
    base = _structure(width=100.0, credit=10.0)
    legs = tuple(_leg(**_leg_overrides(leg, min_lot=3.0)) for leg in base.legs)
    structure = _structure(width=100.0, credit=10.0, legs=legs)

    result = evaluate_defined_risk_margin(structure, equity_usd=1000.0, taker_fee_bps=0.0)

    assert result.accepted is False
    assert result.reason == "min_lot_width_exceeds_caps"
    assert result.minimum_lot_quantity == 3


def test_btc_size_infeasibility_sensitivity_is_informational_rejection():
    structure = _structure(width=2_500.0, credit=100.0)
    result = evaluate_defined_risk_margin(
        structure,
        equity_usd=1000.0,
        limits=DefinedRiskMarginLimits(),
        taker_fee_bps=0.0,
    )

    assert result.accepted is False
    assert result.reason == "min_lot_width_exceeds_caps"
    assert result.structure_max_loss_usd > result.per_structure_cap_usd


def _leg_overrides(leg: OptionLeg, **updates: object) -> dict[str, object]:
    out = {
        "instrument_name": leg.instrument_name,
        "underlying": leg.underlying,
        "contract_multiplier": leg.contract_multiplier,
        "option_type": leg.option_type,
        "side": leg.side,
        "strike": leg.strike,
        "expiry_ms": leg.expiry_ms,
        "settlement_style": leg.settlement_style,
        "settlement_index": leg.settlement_index,
        "premium_currency": leg.premium_currency,
        "fee_currency": leg.fee_currency,
        "collateral_currency": leg.collateral_currency,
        "usd_conversion_source": leg.usd_conversion_source,
        "quote_ts_ms": leg.quote_ts_ms,
        "quote_age_s": leg.quote_age_s,
        "bid_price": leg.bid_price,
        "bid_amount": leg.bid_amount,
        "bid_iv": leg.bid_iv,
        "ask_price": leg.ask_price,
        "ask_amount": leg.ask_amount,
        "ask_iv": leg.ask_iv,
        "mark_price": leg.mark_price,
        "greek_provenance_key": leg.greek_provenance_key,
        "min_tick": leg.min_tick,
        "min_lot": leg.min_lot,
        "source_quality": leg.source_quality,
    }
    out.update(updates)
    return out

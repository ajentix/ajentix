from __future__ import annotations

import pytest

from ajentix_quant.backtest.option_costs import (
    evaluate_structure_costs,
    max_loss_from_width_credit_usd,
)
from ajentix_quant.options import (
    DefinedRiskStructure,
    OptionLeg,
    OptionType,
    Side,
    SourceQuality,
    StructureType,
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


def _structure(**overrides) -> DefinedRiskStructure:
    short = _leg(
        instrument_name="ETH-20241227-3000-P",
        option_type=OptionType.PUT,
        side=Side.SHORT,
        strike=3000.0,
        bid_price=30.0,
        ask_price=31.0,
    )
    long = _leg(
        instrument_name="ETH-20241227-2800-P",
        option_type=OptionType.PUT,
        side=Side.LONG,
        strike=2800.0,
        bid_price=10.0,
        ask_price=11.0,
    )
    net_credit = short.bid_price - long.ask_price
    width = abs(short.strike - long.strike)
    params = {
        "structure_type": StructureType.PUT_CREDIT_SPREAD,
        "legs": (short, long),
        "quantity": 1,
        "entry_snapshot_id": "snapshot-eth-20241227",
        "expiry_ms": EXPIRY_MS,
        "dte_days": 30,
        "settlement_style": "european",
        "settlement_index": "deribit_eth_index",
        "premium_currency": "ETH",
        "fee_currency": "ETH",
        "collateral_currency": "USDC_or_ETH",
        "usd_conversion_source": "deribit_eth_index",
        "net_credit": net_credit,
        "width": width,
        "fees": 0.0,
        "max_loss_usd": max_loss_from_width_credit_usd(
            width=width,
            net_credit=net_credit,
            contract_multiplier=1.0,
            quantity=1,
        ),
        "max_gain_usd": net_credit,
        "entry_quote_ts_ms": QUOTE_TS_MS,
        "max_quote_age_s": 2.0,
        "frozen_param_key": "vrp-eth-credit-spread-grid-v1",
    }
    params.update(overrides)
    return DefinedRiskStructure(**params)


def test_evaluate_structure_costs_crosses_each_leg_and_charges_costs():
    breakdown = evaluate_structure_costs(
        _structure(),
        taker_fee_bps=10.0,
        settlement_fee_bps=10.0,
        safety_margin_bps=1.0,
    )

    assert breakdown.authorizing is True
    assert breakdown.non_authorizing_reason is None
    assert breakdown.net_credit_usd == pytest.approx(19.0)
    assert breakdown.max_loss_usd == pytest.approx(181.0)
    assert breakdown.per_leg_crossing_cost["ETH-20241227-3000-P"] == pytest.approx(1.0)
    assert breakdown.per_leg_crossing_cost["ETH-20241227-2800-P"] == pytest.approx(1.0)
    assert breakdown.fees["entry"] == pytest.approx(0.041)
    assert breakdown.fees["exit"] == pytest.approx(0.041)
    assert breakdown.expiry_settlement_cost == pytest.approx(0.181)
    assert breakdown.safety_margin == pytest.approx(0.02)
    assert breakdown.total_cost_usd == pytest.approx(2.283)
    assert breakdown.usd_conversion["source"] == "deribit_eth_index"


def test_min_tick_rounding_and_usd_conversion_are_in_authorizing_breakdown():
    short = _leg(
        instrument_name="ETH-20241227-3000-P",
        side=Side.SHORT,
        bid_price=30.03,
        ask_price=31.02,
    )
    long = _leg(
        instrument_name="ETH-20241227-2800-P",
        side=Side.LONG,
        strike=2800.0,
        bid_price=10.02,
        ask_price=11.03,
    )
    net_credit = short.bid_price - long.ask_price
    structure = _structure(
        legs=(short, long),
        net_credit=net_credit,
        max_loss_usd=200.0 - net_credit,
        max_gain_usd=net_credit,
    )

    breakdown = evaluate_structure_costs(
        structure,
        taker_fee_bps=0.0,
        settlement_fee_bps=0.0,
        safety_margin_bps=0.0,
        usd_conversion_rate=2.0,
    )

    assert breakdown.min_tick_lot_rounding[
        "ETH-20241227-3000-P:price_delta"
    ] == pytest.approx(-0.03)
    assert breakdown.min_tick_lot_rounding[
        "ETH-20241227-2800-P:price_delta"
    ] == pytest.approx(0.02)
    assert breakdown.usd_conversion["rate"] == 2.0
    assert breakdown.net_credit_usd == pytest.approx((30.0 - 11.05) * 2.0)


def test_cost_path_mutation_changes_breakdown_hash_and_cost_consistently():
    structure = _structure()
    low_fee = evaluate_structure_costs(structure, taker_fee_bps=5.0)
    high_fee = evaluate_structure_costs(structure, taker_fee_bps=20.0)

    assert high_fee.fees["total"] > low_fee.fees["total"]
    assert high_fee.total_cost_usd > low_fee.total_cost_usd
    assert high_fee.net_credit_usd == low_fee.net_credit_usd
    assert high_fee.max_loss_usd == low_fee.max_loss_usd
    assert high_fee.assumptions_hash != low_fee.assumptions_hash


def test_non_authorizing_cost_modes_and_source_quality_are_labeled():
    structure = _structure()
    for mode, expected_reason in (
        ("maker", "maker"),
        ("marks", "marks_only"),
        ("proxy", "proxy"),
        ("sample", "sample"),
    ):
        breakdown = evaluate_structure_costs(structure, cost_mode=mode)
        assert breakdown.authorizing is False
        assert breakdown.non_authorizing_reason == expected_reason

    fixture_structure = _structure(
        legs=tuple(
            _leg(
                instrument_name=leg.instrument_name,
                option_type=leg.option_type,
                side=leg.side,
                strike=leg.strike,
                bid_price=leg.bid_price,
                ask_price=leg.ask_price,
                source_quality=SourceQuality.FIXTURE,
            )
            for leg in structure.legs
        )
    )
    fixture_breakdown = evaluate_structure_costs(fixture_structure)
    assert fixture_breakdown.authorizing is False
    assert fixture_breakdown.non_authorizing_reason == "fixture"

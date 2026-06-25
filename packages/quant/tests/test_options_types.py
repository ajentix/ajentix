"""Foundation tests for canonical immutable option value types."""

from __future__ import annotations

from dataclasses import FrozenInstanceError, fields

import pytest

from ajentix_quant.options import (
    DefinedRiskStructure,
    OptionChainSnapshot,
    OptionCostBreakdown,
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
        "min_tick": 0.0005,
        "min_lot": 0.1,
        "source_quality": SourceQuality.VENUE,
    }
    params.update(overrides)
    return OptionLeg(**params)


def _put_short() -> OptionLeg:
    return _leg(
        instrument_name="ETH-20241227-3000-P",
        option_type=OptionType.PUT,
        side=Side.SHORT,
        strike=3000.0,
    )


def _put_long() -> OptionLeg:
    return _leg(
        instrument_name="ETH-20241227-2800-P",
        option_type=OptionType.PUT,
        side=Side.LONG,
        strike=2800.0,
        bid_price=10.0,
        ask_price=11.0,
    )


def _call_short() -> OptionLeg:
    return _leg(
        instrument_name="ETH-20241227-3400-C",
        option_type=OptionType.CALL,
        side=Side.SHORT,
        strike=3400.0,
    )


def _call_long() -> OptionLeg:
    return _leg(
        instrument_name="ETH-20241227-3600-C",
        option_type=OptionType.CALL,
        side=Side.LONG,
        strike=3600.0,
        bid_price=10.0,
        ask_price=11.0,
    )


def _structure(**overrides) -> DefinedRiskStructure:
    params = {
        "structure_type": StructureType.PUT_CREDIT_SPREAD,
        "legs": (_put_short(), _put_long()),
        "quantity": 2,
        "entry_snapshot_id": "snapshot-eth-20241227",
        "expiry_ms": EXPIRY_MS,
        "dte_days": 30,
        "settlement_style": "european",
        "settlement_index": "deribit_eth_index",
        "premium_currency": "ETH",
        "fee_currency": "ETH",
        "collateral_currency": "USDC_or_ETH",
        "usd_conversion_source": "deribit_eth_index",
        "net_credit": 30.0,
        "width": 200.0,
        "fees": 1.25,
        "max_loss_usd": 340.0,
        "max_gain_usd": 60.0,
        "entry_quote_ts_ms": QUOTE_TS_MS,
        "max_quote_age_s": 2.0,
        "frozen_param_key": "vrp-eth-credit-spread-grid-v1",
    }
    params.update(overrides)
    return DefinedRiskStructure(**params)


def _snapshot() -> OptionChainSnapshot:
    put_short = _put_short()
    return OptionChainSnapshot(
        underlying="ETH",
        exchange="deribit",
        snapshot_ts_ms=QUOTE_TS_MS,
        source_ts_ms=QUOTE_TS_MS,
        source_id="deribit-public-options-fixture",
        scenario_id="deribit_options_eth_vrp_v1",
        settlement_index_price=3100.0,
        index_price=3095.0,
        usd_conversion_inputs={"ETH": 3095.0},
        legs=(put_short, _put_long()),
        source_quality_map={"chain": SourceQuality.VENUE},
        schema_version="aq-options-cache-v1",
        manifest_sha256="f" * 64,
    )


def test_defined_risk_structure_id_is_deterministic_and_order_independent():
    first = _structure()
    second = _structure(legs=tuple(reversed(first.legs)))

    assert first.structure_id == second.structure_id
    assert first.structure_id.startswith("drs-")
    assert len(first.structure_id) == len("drs-") + 12


def test_option_leg_is_frozen():
    leg = _put_short()

    with pytest.raises(FrozenInstanceError):
        leg.strike = 1.0  # type: ignore[misc]


def test_defined_risk_validation_rejects_naked_or_uncapped_structures():
    with pytest.raises(ValueError, match="exactly two"):
        _structure(legs=(_put_short(),))

    with pytest.raises(ValueError, match="one long leg and one short leg"):
        _structure(
            legs=(
                _put_short(),
                _leg(instrument_name="ETH-20241227-2800-P"),
            )
        )

    with pytest.raises(ValueError, match="lower-strike long put"):
        _structure(
            legs=(
                _put_short(),
                _leg(
                    instrument_name="ETH-20241227-3200-P",
                    side=Side.LONG,
                    strike=3200.0,
                ),
            )
        )

    with pytest.raises(ValueError, match="max_loss_usd"):
        _structure(max_loss_usd=1.0)


def test_max_loss_invariant_holds_for_put_and_call_credit_spreads():
    put = _structure()
    call = _structure(
        structure_type=StructureType.CALL_CREDIT_SPREAD,
        legs=(_call_short(), _call_long()),
        net_credit=25.0,
        width=200.0,
        max_loss_usd=350.0,
        max_gain_usd=50.0,
    )

    for structure in (put, call):
        multiplier = structure.legs[0].contract_multiplier
        expected = (
            (structure.width - structure.net_credit)
            * multiplier
            * structure.quantity
        )
        assert structure.max_loss_usd == pytest.approx(expected)


def test_option_chain_snapshot_lookup_raises_key_error_on_miss():
    snapshot = _snapshot()

    assert (
        snapshot.leg_by_instrument_name("ETH-20241227-3000-P")
        is snapshot.legs[0]
    )
    with pytest.raises(KeyError):
        snapshot.leg_by_instrument_name("ETH-UNKNOWN")


def test_enum_fields_reject_garbage_values():
    with pytest.raises(ValueError, match="option_type"):
        _leg(option_type="straddle")

    with pytest.raises(ValueError, match="side"):
        _leg(side="flat")

    with pytest.raises(ValueError, match="source_quality"):
        _leg(source_quality="made_up")

    with pytest.raises(ValueError, match="structure_type"):
        _structure(structure_type="iron_condor")


def test_required_fields_are_present():
    assert {field.name for field in fields(OptionLeg)} == {
        "instrument_name",
        "underlying",
        "contract_multiplier",
        "option_type",
        "side",
        "strike",
        "expiry_ms",
        "settlement_style",
        "settlement_index",
        "premium_currency",
        "fee_currency",
        "collateral_currency",
        "usd_conversion_source",
        "quote_ts_ms",
        "quote_age_s",
        "bid_price",
        "bid_amount",
        "bid_iv",
        "ask_price",
        "ask_amount",
        "ask_iv",
        "mark_price",
        "greek_provenance_key",
        "min_tick",
        "min_lot",
        "source_quality",
    }
    assert {field.name for field in fields(OptionChainSnapshot)} == {
        "underlying",
        "exchange",
        "snapshot_ts_ms",
        "source_ts_ms",
        "source_id",
        "scenario_id",
        "settlement_index_price",
        "index_price",
        "usd_conversion_inputs",
        "legs",
        "source_quality_map",
        "schema_version",
        "manifest_sha256",
    }
    assert {field.name for field in fields(DefinedRiskStructure)} == {
        "structure_type",
        "legs",
        "quantity",
        "entry_snapshot_id",
        "expiry_ms",
        "dte_days",
        "settlement_style",
        "settlement_index",
        "premium_currency",
        "fee_currency",
        "collateral_currency",
        "usd_conversion_source",
        "net_credit",
        "width",
        "fees",
        "max_loss_usd",
        "max_gain_usd",
        "entry_quote_ts_ms",
        "max_quote_age_s",
        "frozen_param_key",
        "structure_id",
    }
    assert {field.name for field in fields(OptionCostBreakdown)} == {
        "structure_id",
        "per_leg_crossing_cost",
        "fees",
        "min_tick_lot_rounding",
        "usd_conversion",
        "entry_cost",
        "exit_cost",
        "expiry_settlement_cost",
        "safety_margin",
        "total_cost_usd",
        "net_credit_usd",
        "max_loss_usd",
        "assumptions_hash",
        "authorizing",
        "non_authorizing_reason",
    }

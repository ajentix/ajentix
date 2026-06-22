from __future__ import annotations

from dataclasses import replace

import pytest

from ajentix_quant.options import (
    DefinedRiskStructure,
    OptionChainSnapshot,
    OptionLeg,
    OptionType,
    Side,
    SourceQuality,
    StructureType,
)
from ajentix_quant.strategies.vrp_defined_risk import (
    VrpDefinedRiskStrategy,
    VrpExitAction,
    construct_vrp_defined_risk_structures,
)

SNAPSHOT_MS = 1_700_000_000_000
EXPIRY_30D_MS = SNAPSHOT_MS + 30 * 86_400_000


def _leg(
    name: str,
    option_type: OptionType,
    strike: float,
    *,
    bid: float,
    ask: float,
    side: Side = Side.SHORT,
    quote_age_s: float = 2.0,
    bid_amount: float = 5.0,
    ask_amount: float = 5.0,
) -> OptionLeg:
    return OptionLeg(
        instrument_name=name,
        underlying="ETH",
        contract_multiplier=1.0,
        option_type=option_type,
        side=side,
        strike=strike,
        expiry_ms=EXPIRY_30D_MS,
        settlement_style="european",
        settlement_index="deribit_eth_index",
        premium_currency="ETH",
        fee_currency="ETH",
        collateral_currency="USDC_or_ETH",
        usd_conversion_source="deribit_eth_index",
        quote_ts_ms=SNAPSHOT_MS,
        quote_age_s=quote_age_s,
        bid_price=bid,
        bid_amount=bid_amount,
        bid_iv=0.55,
        ask_price=ask,
        ask_amount=ask_amount,
        ask_iv=0.56,
        mark_price=(bid + ask) / 2.0,
        greek_provenance_key="vendor_cached_hashed_preferred_else_local",
        min_tick=0.05,
        min_lot=1.0,
        source_quality=SourceQuality.FIXTURE,
    )


def _snapshot(*, legs: tuple[OptionLeg, ...] | None = None) -> OptionChainSnapshot:
    legs = legs or (
        _leg("ETH-30D-2800-P", OptionType.PUT, 2800.0, bid=15.0, ask=20.0),
        _leg("ETH-30D-2900-P", OptionType.PUT, 2900.0, bid=55.0, ask=60.0),
        _leg("ETH-30D-3000-P", OptionType.PUT, 3000.0, bid=95.0, ask=100.0),
        _leg("ETH-30D-3100-C", OptionType.CALL, 3100.0, bid=55.0, ask=60.0),
        _leg("ETH-30D-3200-C", OptionType.CALL, 3200.0, bid=15.0, ask=20.0),
        _leg("ETH-30D-3300-C", OptionType.CALL, 3300.0, bid=8.0, ask=10.0),
    )
    deltas = {
        "ETH-30D-2800-P": -0.10,
        "ETH-30D-2900-P": -0.16,
        "ETH-30D-3000-P": -0.45,
        "ETH-30D-3100-C": 0.16,
        "ETH-30D-3200-C": 0.10,
        "ETH-30D-3300-C": 0.05,
    }
    return OptionChainSnapshot(
        underlying="ETH",
        exchange="deribit",
        snapshot_ts_ms=SNAPSHOT_MS,
        source_ts_ms=SNAPSHOT_MS,
        source_id="fixture-current-deribit-options",
        scenario_id="deribit_options_eth_vrp_v1",
        settlement_index_price=3000.0,
        index_price=3000.0,
        usd_conversion_inputs={"ETH": 3000.0, "vendor_delta_by_instrument": deltas},
        legs=legs,
        source_quality_map={"chain": SourceQuality.FIXTURE},
        schema_version="aq-options-cache-v1",
        manifest_sha256="f" * 64,
    )


def test_constructs_deterministic_put_and_call_credit_spreads_from_snapshot():
    first = construct_vrp_defined_risk_structures(_snapshot(), max_quote_age_s=30.0)
    second = construct_vrp_defined_risk_structures(_snapshot(), max_quote_age_s=30.0)

    assert first
    assert tuple(structure.structure_id for structure in first) == tuple(
        structure.structure_id for structure in second
    )
    assert {structure.structure_type for structure in first} == {
        StructureType.PUT_CREDIT_SPREAD,
        StructureType.CALL_CREDIT_SPREAD,
    }
    assert all(isinstance(structure, DefinedRiskStructure) for structure in first)
    assert all(structure.quantity == 1 for structure in first)


def test_authorizing_construction_fails_closed_without_vendor_deltas() -> None:
    snapshot = replace(_snapshot(), usd_conversion_inputs={"ETH": 3000.0})

    assert construct_vrp_defined_risk_structures(snapshot, max_quote_age_s=30.0) == ()


def test_diagnostic_greek_selection_records_non_authorizing_provenance() -> None:
    snapshot = replace(_snapshot(), usd_conversion_inputs={"ETH": 3000.0})
    grid = {
        "search_space_version": "test-diagnostic-grid",
        "structure_types": ["put_credit_spread"],
        "dte_targets": [30],
        "short_leg_abs_delta": [0.38],
        "width_usd": [100],
        "min_credit_to_width": [0.10],
        "exit_rule": {
            "profit_take_frac": 0.50,
            "stop_loss_credit_mult": 2.0,
            "else": "hold_to_european_settlement",
        },
        "rolls": False,
    }

    structures = VrpDefinedRiskStrategy(
        max_quote_age_s=30.0,
        grid=grid,
        allow_diagnostic_greek_selection=True,
    ).construct_structures(snapshot)

    assert structures
    assert all(
        "greeks=local_black_scholes_diagnostic_only_non_authorizing"
        in structure.frozen_param_key
        for structure in structures
    )

def test_rejects_stale_and_missing_quotes():
    stale = tuple(replace(leg, quote_age_s=120.0) for leg in _snapshot().legs)
    missing = tuple(replace(leg, bid_amount=0.0) for leg in _snapshot().legs)

    assert construct_vrp_defined_risk_structures(_snapshot(legs=stale), max_quote_age_s=30.0) == ()
    assert (
        construct_vrp_defined_risk_structures(_snapshot(legs=missing), max_quote_age_s=30.0)
        == ()
    )


def test_never_emits_naked_or_uncapped_legs():
    structures = construct_vrp_defined_risk_structures(_snapshot(), max_quote_age_s=30.0)

    for structure in structures:
        assert len(structure.legs) == 2
        assert {leg.side for leg in structure.legs} == {Side.SHORT, Side.LONG}
        assert structure.max_loss_usd == pytest.approx(
            (structure.width - structure.net_credit) * structure.quantity
        )
        assert structure.net_credit > 0.0
        assert structure.net_credit < structure.width


def test_stable_short_leg_tie_breaker_prefers_nearest_strike():
    legs = (
        _leg("ETH-30D-2800-P", OptionType.PUT, 2800.0, bid=15.0, ask=20.0),
        _leg("ETH-30D-2880-P", OptionType.PUT, 2880.0, bid=50.0, ask=55.0),
        _leg("ETH-30D-2920-P", OptionType.PUT, 2920.0, bid=50.0, ask=55.0),
    )
    snapshot = replace(
        _snapshot(legs=legs),
        usd_conversion_inputs={
            "ETH": 3000.0,
            "vendor_delta_by_instrument": {
                "ETH-30D-2800-P": -0.10,
                "ETH-30D-2880-P": -0.15,
                "ETH-30D-2920-P": -0.15,
            },
        },
    )
    grid = {
        "search_space_version": "test-grid",
        "structure_types": ["put_credit_spread"],
        "dte_targets": [30],
        "short_leg_abs_delta": [0.16],
        "width_usd": [100],
        "min_credit_to_width": [0.10],
        "exit_rule": {
            "profit_take_frac": 0.50,
            "stop_loss_credit_mult": 2.0,
            "else": "hold_to_european_settlement",
        },
        "rolls": False,
    }
    structures = VrpDefinedRiskStrategy(max_quote_age_s=30.0, grid=grid).construct_structures(
        snapshot
    )
    put_structures = [s for s in structures if s.structure_type is StructureType.PUT_CREDIT_SPREAD]

    assert put_structures
    assert put_structures[0].legs[0].strike == 2920.0


def test_only_put_call_credit_spreads_and_exit_rules_are_supported():
    strategy = VrpDefinedRiskStrategy(max_quote_age_s=30.0)
    structures = strategy.construct_structures(_snapshot())

    assert {structure.structure_type for structure in structures} <= {
        StructureType.PUT_CREDIT_SPREAD,
        StructureType.CALL_CREDIT_SPREAD,
    }
    assert strategy.exit_action(
        entry_credit_usd=20.0,
        close_debit_usd=9.0,
        bid_ask_available=True,
    ) is VrpExitAction.TAKE_PROFIT
    assert strategy.exit_action(
        entry_credit_usd=20.0,
        close_debit_usd=40.0,
        bid_ask_available=True,
    ) is VrpExitAction.STOP_LOSS
    assert strategy.exit_action(
        entry_credit_usd=20.0,
        close_debit_usd=None,
        bid_ask_available=False,
    ) is VrpExitAction.HOLD_TO_SETTLEMENT

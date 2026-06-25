from __future__ import annotations

import pytest

from ajentix_quant.options import (
    OptionChainSnapshot,
    OptionLeg,
    OptionType,
    Side,
    SourceQuality,
    StructureType,
)
from ajentix_quant.options.usd_projection import EVAL_PREMIUM_CURRENCY, project_snapshot_to_usd
from ajentix_quant.strategies.vrp_defined_risk import construct_vrp_defined_risk_structures
from ajentix_quant.strategies.vrp_defined_risk_usd_eval import (
    VrpDefinedRiskUsdEvalStrategy,
    construct_usd_eval_structures,
)

SNAPSHOT_MS = 1_700_000_000_000
EXPIRY_30D_MS = SNAPSHOT_MS + 30 * 86_400_000
SPOT = 3000.0

# A focused single-combo grid so selection is unambiguous.
_GRID = {
    "search_space_version": "vrp-eth-credit-spread-grid-v1",
    "structure_types": ["put_credit_spread"],
    "dte_targets": [30],
    "short_leg_abs_delta": [0.16],
    "width_usd": [100],
    "min_credit_to_width": [0.15],
    "exit_rule": {
        "profit_take_frac": 0.50,
        "stop_loss_credit_mult": 2.0,
        "else": "hold_to_european_settlement",
    },
    "rolls": False,
}


def _leg(name: str, strike: float, *, bid: float, ask: float) -> OptionLeg:
    return OptionLeg(
        instrument_name=name,
        underlying="ETH",
        contract_multiplier=1.0,
        option_type=OptionType.PUT,
        side=Side.SHORT,
        strike=strike,
        expiry_ms=EXPIRY_30D_MS,
        settlement_style="european",
        settlement_index="deribit_eth_index",
        premium_currency="ETH",
        fee_currency="ETH",
        collateral_currency="USDC_or_ETH",
        usd_conversion_source="deribit_eth_index",
        quote_ts_ms=SNAPSHOT_MS,
        quote_age_s=2.0,
        bid_price=bid,
        bid_amount=100.0,
        bid_iv=0.6,
        ask_price=ask,
        ask_amount=100.0,
        ask_iv=0.61,
        mark_price=(bid + ask) / 2.0,
        greek_provenance_key="vendor_cached_hashed_preferred_else_local",
        min_tick=1e-06,
        min_lot=1.0,
        source_quality=SourceQuality.FIXTURE,
    )


def _eth_snapshot(*, short_bid: float = 0.020, long_ask: float = 0.013) -> OptionChainSnapshot:
    """A real-shaped ETH-premium snapshot (ETH bid/ask, USD strikes)."""
    legs = (
        _leg("ETH-30D-3000-P", 3000.0, bid=0.045, ask=0.047),  # ATM-ish, not selected at d=0.16
        _leg("ETH-30D-2900-P", 2900.0, bid=short_bid, ask=short_bid + 0.001),  # short, delta ~0.16
        _leg("ETH-30D-2800-P", 2800.0, bid=long_ask - 0.001, ask=long_ask),  # long, 100 wide
    )
    deltas = {
        "ETH-30D-3000-P": -0.45,
        "ETH-30D-2900-P": -0.16,
        "ETH-30D-2800-P": -0.10,
    }
    return OptionChainSnapshot(
        underlying="ETH",
        exchange="deribit",
        snapshot_ts_ms=SNAPSHOT_MS,
        source_ts_ms=SNAPSHOT_MS,
        source_id="fixture",
        scenario_id="deribit_history_eth_vrp_free_v1",
        settlement_index_price=SPOT,
        index_price=SPOT,
        usd_conversion_inputs={"ETH_USD": SPOT, "vendor_delta_by_instrument": deltas},
        legs=legs,
        source_quality_map={"chain": SourceQuality.FIXTURE},
        schema_version="aq-options-cache-v1",
        manifest_sha256="f" * 64,
    )


def _strategy() -> VrpDefinedRiskUsdEvalStrategy:
    return VrpDefinedRiskUsdEvalStrategy(max_quote_age_s=30.0, grid=_GRID)


def test_frozen_rejects_eth_credit_eval_accepts_usd_projection() -> None:
    """The core fix: frozen gate kills the structure on ETH units; eval clears it in USD units."""
    eth_snapshot = _eth_snapshot()

    # Frozen strategy on real ETH-premium data: credit/width is ~rate too small -> zero structures.
    assert construct_vrp_defined_risk_structures(eth_snapshot, max_quote_age_s=30.0) == ()

    # Eval strategy needs USD-projected legs; on ETH legs it also yields nothing (USD settlement).
    assert _strategy().construct_structures(eth_snapshot) == ()

    # On USD-projected legs the very same grid/bar now clears: credit 21 USD / width 100 = 0.21.
    usd_snapshot = project_snapshot_to_usd(eth_snapshot)
    assert usd_snapshot is not None
    structures = _strategy().construct_structures(usd_snapshot)
    assert structures
    assert {s.structure_type for s in structures} == {StructureType.PUT_CREDIT_SPREAD}


def test_usd_gate_enforced_below_and_above_bar() -> None:
    # credit (0.015-0.014)=0.001 ETH * 3000 = 3 USD / 100 width = 0.03 < 0.15 -> rejected.
    below = project_snapshot_to_usd(_eth_snapshot(short_bid=0.015, long_ask=0.014))
    assert below is not None
    assert _strategy().construct_structures(below) == ()

    # credit (0.024-0.013)=0.011 ETH * 3000 = 33 USD / 100 = 0.33 >= 0.15 -> accepted.
    above = project_snapshot_to_usd(_eth_snapshot(short_bid=0.024, long_ask=0.013))
    assert above is not None
    assert _strategy().construct_structures(above)


def test_structures_are_usd_settled_and_capped() -> None:
    usd_snapshot = project_snapshot_to_usd(_eth_snapshot())
    assert usd_snapshot is not None
    structures = _strategy().construct_structures(usd_snapshot)

    assert structures
    for structure in structures:
        assert len(structure.legs) == 2
        assert {leg.side for leg in structure.legs} == {Side.SHORT, Side.LONG}
        assert structure.premium_currency == EVAL_PREMIUM_CURRENCY
        assert structure.net_credit > 0.0
        assert structure.net_credit < structure.width
        assert structure.max_loss_usd == pytest.approx(
            (structure.width - structure.net_credit) * structure.quantity
        )


def test_construction_is_deterministic() -> None:
    usd_snapshot = project_snapshot_to_usd(_eth_snapshot())
    assert usd_snapshot is not None
    first = construct_usd_eval_structures(usd_snapshot, max_quote_age_s=30.0)
    second = construct_usd_eval_structures(usd_snapshot, max_quote_age_s=30.0)
    assert tuple(s.structure_id for s in first) == tuple(s.structure_id for s in second)

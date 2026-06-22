from __future__ import annotations

from dataclasses import replace

import pytest

from ajentix_quant.backtest.option_costs import max_loss_from_width_credit_usd
from ajentix_quant.backtest.vrp_engine import VrpBacktestStep, run_vrp_backtest
from ajentix_quant.options.types import (
    DefinedRiskStructure,
    OptionChainSnapshot,
    OptionLeg,
    OptionType,
    Side,
    SourceQuality,
    StructureType,
)

TS0 = 1_700_000_000_000
EXPIRY = TS0 + 30 * 86_400_000


def _leg(name: str, strike: float, side: Side, bid: float, ask: float) -> OptionLeg:
    return OptionLeg(
        instrument_name=name,
        underlying="ETH",
        contract_multiplier=1.0,
        option_type=OptionType.PUT,
        side=side,
        strike=strike,
        expiry_ms=EXPIRY,
        settlement_style="european",
        settlement_index="deribit_eth_index",
        premium_currency="ETH",
        fee_currency="ETH",
        collateral_currency="USDC_or_ETH",
        usd_conversion_source="deribit_eth_index",
        quote_ts_ms=TS0,
        quote_age_s=1.0,
        bid_price=bid,
        bid_amount=10.0,
        bid_iv=0.55,
        ask_price=ask,
        ask_amount=10.0,
        ask_iv=0.56,
        mark_price=(bid + ask) / 2.0,
        greek_provenance_key="vendor_cached_hashed_preferred_else_local",
        min_tick=0.05,
        min_lot=1.0,
        source_quality=SourceQuality.VENUE,
    )


def _structure() -> DefinedRiskStructure:
    short = _leg("ETH-30D-3000-P", 3000.0, Side.SHORT, 35.0, 36.0)
    long = _leg("ETH-30D-2900-P", 2900.0, Side.LONG, 9.5, 10.0)
    credit = short.bid_price - long.ask_price
    width = short.strike - long.strike
    return DefinedRiskStructure(
        structure_type=StructureType.PUT_CREDIT_SPREAD,
        legs=(short, long),
        quantity=1,
        entry_snapshot_id="entry",
        expiry_ms=EXPIRY,
        dte_days=30,
        settlement_style="european",
        settlement_index="deribit_eth_index",
        premium_currency="ETH",
        fee_currency="ETH",
        collateral_currency="USDC_or_ETH",
        usd_conversion_source="deribit_eth_index",
        net_credit=credit,
        width=width,
        fees=0.0,
        max_loss_usd=max_loss_from_width_credit_usd(
            width=width,
            net_credit=credit,
            contract_multiplier=1.0,
            quantity=1,
        ),
        max_gain_usd=credit,
        entry_quote_ts_ms=TS0,
        max_quote_age_s=1.0,
        frozen_param_key="grid|put",
    )


def _snapshot(
    *,
    short_bid: float,
    short_ask: float,
    long_bid: float,
    long_ask: float,
) -> OptionChainSnapshot:
    legs = (
        _leg("ETH-30D-3000-P", 3000.0, Side.SHORT, short_bid, short_ask),
        _leg("ETH-30D-2900-P", 2900.0, Side.LONG, long_bid, long_ask),
    )
    return OptionChainSnapshot(
        underlying="ETH",
        exchange="deribit",
        snapshot_ts_ms=TS0,
        source_ts_ms=TS0,
        source_id="fixture",
        scenario_id="unit",
        settlement_index_price=3000.0,
        index_price=3000.0,
        usd_conversion_inputs={"ETH": 3000.0},
        legs=legs,
        source_quality_map={"option_chain": SourceQuality.VENUE},
        schema_version="aq-options-cache-v1",
        manifest_sha256="f" * 64,
    )


def test_fold_replay_uses_frozen_structures_and_option_costs(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import ajentix_quant.backtest.vrp_engine as module

    calls: list[str] = []
    real_entry = module.evaluate_structure_costs
    real_exit = module.evaluate_structure_exit_costs

    def wrapped_entry(structure, **kwargs):
        calls.append("entry")
        return real_entry(structure, **kwargs)

    def wrapped_exit(structure, **kwargs):
        calls.append("exit")
        return real_exit(structure, **kwargs)

    monkeypatch.setattr(module, "evaluate_structure_costs", wrapped_entry)
    monkeypatch.setattr(module, "evaluate_structure_exit_costs", wrapped_exit)
    structure = _structure()
    result = run_vrp_backtest(
        [
            VrpBacktestStep(
                entry_timestamp_ms=TS0,
                structure=structure,
                entry_snapshot=_snapshot(
                    short_bid=35.0,
                    short_ask=36.0,
                    long_bid=9.5,
                    long_ask=10.0,
                ),
                exit_snapshot=_snapshot(
                    short_bid=12.0,
                    short_ask=13.0,
                    long_bid=3.0,
                    long_ask=4.0,
                ),
                taker_fee_bps=0.0,
            )
        ]
    )

    assert calls == ["entry", "exit"]
    assert result.n_entries == 1
    assert result.n_exits == 1
    assert result.cost_breakdowns[0].net_credit_usd == pytest.approx(25.0)
    assert all(event.structure_id == structure.structure_id for event in result.events)
    assert result.final_equity_usd > result.initial_equity_usd


def test_exit_pnl_uses_option_cost_path_min_tick_rounding() -> None:
    structure = _structure()
    entry_snapshot = _snapshot(
        short_bid=35.0,
        short_ask=36.0,
        long_bid=9.5,
        long_ask=10.0,
    )
    fine_exit_snapshot = _snapshot(
        short_bid=12.01,
        short_ask=13.02,
        long_bid=3.03,
        long_ask=4.04,
    )
    coarse_exit_snapshot = replace(
        fine_exit_snapshot,
        legs=tuple(replace(leg, min_tick=0.5) for leg in fine_exit_snapshot.legs),
    )

    fine = run_vrp_backtest(
        [
            VrpBacktestStep(
                entry_timestamp_ms=TS0,
                structure=structure,
                entry_snapshot=entry_snapshot,
                exit_snapshot=fine_exit_snapshot,
                taker_fee_bps=0.0,
            )
        ]
    )
    coarse = run_vrp_backtest(
        [
            VrpBacktestStep(
                entry_timestamp_ms=TS0,
                structure=structure,
                entry_snapshot=entry_snapshot,
                exit_snapshot=coarse_exit_snapshot,
                taker_fee_bps=0.0,
            )
        ]
    )

    fine_close_debit = fine.cost_breakdowns[1].fees["exit_close_debit_usd"]
    coarse_close_debit = coarse.cost_breakdowns[1].fees["exit_close_debit_usd"]
    assert fine_close_debit == pytest.approx(10.05)
    assert coarse_close_debit == pytest.approx(10.5)
    assert coarse.events[-1].pnl_usd < fine.events[-1].pnl_usd
    assert fine.events[-1].pnl_usd - coarse.events[-1].pnl_usd == pytest.approx(0.45)


def test_max_loss_invariant_at_entry_expiry_and_stress() -> None:
    result = run_vrp_backtest(
        [
            VrpBacktestStep(
                entry_timestamp_ms=TS0,
                structure=_structure(),
                entry_snapshot=_snapshot(
                    short_bid=35.0,
                    short_ask=36.0,
                    long_bid=9.5,
                    long_ask=10.0,
                ),
                settlement_price=1.0,
                stress_settlement_prices=(1.0, 2_950.0),
                taker_fee_bps=0.0,
            )
        ],
        initial_equity_usd=1_000.0,
    )

    assert result.n_expiries == 1
    assert result.n_stress_events == 2
    assert result.max_loss_invariant_ok is True
    assert all(event.invariant_ok for event in result.events)
    assert min(event.pnl_usd for event in result.events) >= -result.cost_breakdowns[0].max_loss_usd


def test_replay_is_deterministic() -> None:
    step = VrpBacktestStep(
        entry_timestamp_ms=TS0,
        structure=_structure(),
        entry_snapshot=_snapshot(short_bid=35.0, short_ask=36.0, long_bid=9.5, long_ask=10.0),
        exit_snapshot=_snapshot(short_bid=20.0, short_ask=21.0, long_bid=5.0, long_ask=6.0),
        taker_fee_bps=0.0,
    )

    first = run_vrp_backtest([step]).as_dict()
    second = run_vrp_backtest([replace(step)]).as_dict()

    assert first == second

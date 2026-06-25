from __future__ import annotations

import json
from pathlib import Path

import pytest

from ajentix_quant.data.vrp_free_history_cache import IndexPathPoint, parse_deribit_history_trades
from ajentix_quant.options.iv_surface_reconstruction import (
    IVSurfaceCoverageError,
    reconstruct_iv_surface_at,
    reconstructed_chains_sha256,
)

FIXTURE_PATH = (
    Path(__file__).parent
    / "fixtures"
    / "vrp_free_history"
    / "eth_option_trades_fixture.jsonl"
)
START_MS = 1725148800000
MID_MS = 1725177600000
END_MS = 1725206400000
HOUR_MS = 60 * 60 * 1000


def _fixture_rows() -> list[dict]:
    return [json.loads(line) for line in FIXTURE_PATH.read_text(encoding="utf-8").splitlines()]


def _all_trades():
    return parse_deribit_history_trades(_fixture_rows())


def _leg(chains, instrument_name: str):
    for chain in chains:
        for leg in chain.snapshot.legs:
            if leg.instrument_name == instrument_name:
                return leg
    raise AssertionError(f"missing reconstructed leg {instrument_name}")


def test_future_trade_timestamp_gt_snapshot_is_excluded_from_surface():
    rows = _fixture_rows()
    future = dict(rows[0])
    future["trade_id"] = "fixture-future-put"
    future["trade_seq"] = 9999
    future["timestamp"] = MID_MS
    future["iv"] = 99.9
    future["index_price"] = 2520.0
    rows.append(future)
    trades = parse_deribit_history_trades(rows)

    chains = reconstruct_iv_surface_at(trades, snapshot_ts_ms=START_MS)
    leg = _leg(chains, "ETH-27SEP24-2400-P")

    assert leg.quote_ts_ms == START_MS
    assert leg.bid_iv == pytest.approx(0.652)
    assert leg.bid_iv != pytest.approx(0.999)
    assert leg.bid_price == leg.ask_price


def test_staleness_greater_than_72h_is_excluded_and_fails_closed():
    trades = parse_deribit_history_trades(_fixture_rows()[:2])
    stale_snapshot = START_MS + (72 * HOUR_MS) + 1
    fresh_index_path = [
        IndexPathPoint(
            timestamp_ms=stale_snapshot,
            underlying="ETH",
            index_price=2500.0,
        )
    ]

    with pytest.raises(IVSurfaceCoverageError, match="missing_trade_coverage"):
        reconstruct_iv_surface_at(
            trades,
            snapshot_ts_ms=stale_snapshot,
            index_path=fresh_index_path,
        )


def test_identical_inputs_reproduce_identical_surface_digest():
    trades = _all_trades()

    first = reconstruct_iv_surface_at(trades, snapshot_ts_ms=END_MS)
    second = reconstruct_iv_surface_at(tuple(reversed(trades)), snapshot_ts_ms=END_MS)

    assert reconstructed_chains_sha256(first) == reconstructed_chains_sha256(second)
    assert [chain.snapshot.snapshot_ts_ms for chain in first] == [END_MS, END_MS]
    assert [chain.snapshot.snapshot_ts_ms for chain in second] == [END_MS, END_MS]

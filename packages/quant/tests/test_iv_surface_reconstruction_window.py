"""Equivalence lock for the bisect-windowed reconstruction lookups.

The reconstruction previously re-sorted and full-scanned the entire trade/index history at
every grid snapshot (O(snapshots * N log N)); it now sorts once and locates the eligible
staleness window with bisect. These tests prove the windowed helpers return results that are
byte-for-byte identical to the original brute-force scan across randomized inputs and many
snapshot timestamps, including the staleness and expiry boundaries.
"""

from __future__ import annotations

import random
from types import SimpleNamespace

import ajentix_quant.options.iv_surface_reconstruction as recon


def _brute_latest_trade(trades, snapshot_ts_ms, max_staleness_ms):
    latest: dict = {}
    for trade in sorted(trades, key=recon._trade_sort_key):
        if trade.timestamp_ms > snapshot_ts_ms:
            continue
        if snapshot_ts_ms - trade.timestamp_ms > max_staleness_ms:
            continue
        if trade.expiry_ms <= snapshot_ts_ms:
            continue
        previous = latest.get(trade.instrument_name)
        if previous is None or recon._trade_sort_key(trade) > recon._trade_sort_key(previous):
            latest[trade.instrument_name] = trade
    return latest


def _brute_latest_point(points, snapshot_ts_ms):
    latest = None
    for point in sorted(points, key=lambda item: item.timestamp_ms):
        if point.timestamp_ms <= snapshot_ts_ms:
            latest = point
        else:
            break
    return latest


def test_windowed_trade_lookup_matches_brute_force() -> None:
    rng = random.Random(20260622)
    instruments = [f"ETH-{i}-C" for i in range(6)]
    trades = []
    for tid in range(900):
        ts = rng.randrange(1_000_000, 1_000_000 + 5_000_000, 1)
        trades.append(
            SimpleNamespace(
                timestamp_ms=ts,
                trade_seq=rng.randrange(0, 10),
                instrument_name=rng.choice(instruments),
                trade_id=f"ETH-{tid}",
                expiry_ms=ts + rng.choice([-50_000, 200_000, 5_000_000]),
            )
        )
    trades_sorted = sorted(trades, key=recon._trade_sort_key)
    trade_ts = [t.timestamp_ms for t in trades_sorted]
    staleness = 300_000
    for _ in range(200):
        snap = rng.randrange(900_000, 1_000_000 + 5_200_000, 1)
        got = recon._latest_eligible_trade_by_instrument(
            trades_sorted, trade_ts, snapshot_ts_ms=snap, max_staleness_ms=staleness
        )
        want = _brute_latest_trade(trades, snap, staleness)
        assert got == want, f"window mismatch at snapshot {snap}"


def test_windowed_trade_lookup_honours_staleness_and_expiry_edges() -> None:
    snap = 2_000_000
    staleness = 100_000
    trades = [
        # exactly at the staleness floor -> eligible
        SimpleNamespace(
            timestamp_ms=snap - staleness,
            trade_seq=0,
            instrument_name="ETH-A",
            trade_id="t1",
            expiry_ms=snap + 10,
        ),
        # one ms too stale -> excluded
        SimpleNamespace(
            timestamp_ms=snap - staleness - 1,
            trade_seq=0,
            instrument_name="ETH-B",
            trade_id="t2",
            expiry_ms=snap + 10,
        ),
        # exactly at snapshot but already expired (expiry == snapshot) -> excluded
        SimpleNamespace(
            timestamp_ms=snap,
            trade_seq=0,
            instrument_name="ETH-C",
            trade_id="t3",
            expiry_ms=snap,
        ),
        # in the future -> excluded
        SimpleNamespace(
            timestamp_ms=snap + 1,
            trade_seq=0,
            instrument_name="ETH-D",
            trade_id="t4",
            expiry_ms=snap + 10,
        ),
    ]
    trades_sorted = sorted(trades, key=recon._trade_sort_key)
    trade_ts = [t.timestamp_ms for t in trades_sorted]
    got = recon._latest_eligible_trade_by_instrument(
        trades_sorted, trade_ts, snapshot_ts_ms=snap, max_staleness_ms=staleness
    )
    assert set(got) == {"ETH-A"}


def test_windowed_index_lookup_matches_brute_force() -> None:
    rng = random.Random(7)
    points = [
        SimpleNamespace(timestamp_ms=ts, index_price=2000.0 + ts % 7)
        for ts in sorted(rng.sample(range(1_000_000, 2_000_000), 400))
    ]
    point_ts = [p.timestamp_ms for p in points]
    for _ in range(200):
        snap = rng.randrange(900_000, 2_100_000)
        want = _brute_latest_point(points, snap)
        if want is None:
            # no point <= snap -> windowed lookup must fail closed
            try:
                recon._latest_index_point(
                    points, point_ts, snapshot_ts_ms=snap, max_staleness_ms=10**12
                )
                raise AssertionError("expected missing_index_coverage")
            except recon.IVSurfaceCoverageError:
                continue
        got = recon._latest_index_point(
            points, point_ts, snapshot_ts_ms=snap, max_staleness_ms=10**12
        )
        assert got is want, f"index mismatch at snapshot {snap}"

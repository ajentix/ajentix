"""G002: ReplayVenueAdapter serves cached data with no network, fail-closed on absence."""

import pytest

from ajentix_quant.adapters.base import (
    FundingRateHistoryRequest,
    MarketType,
    PriceType,
)
from ajentix_quant.data.replay import ReplayVenueAdapter
from test_data_cache import SYM, _full_dataset_kwargs


def _adapter(tmp_path):
    from ajentix_quant.data.cache import write_cache

    write_cache(tmp_path, "sc1", **_full_dataset_kwargs())
    return ReplayVenueAdapter.from_cache(tmp_path, "sc1")


def test_fetch_funding_rate_returns_latest(tmp_path):
    adapter = _adapter(tmp_path)
    fr = adapter.fetch_funding_rate(SYM)
    assert fr.symbol == SYM
    assert fr.timestamp == 3 * 8 * 3600 * 1000  # last of 4


def test_fetch_funding_rate_history_range_filter(tmp_path):
    adapter = _adapter(tmp_path)
    step = 8 * 3600 * 1000
    req = FundingRateHistoryRequest(symbol=SYM, since_ms=step, until_ms=2 * step)
    out = adapter.fetch_funding_rate_history(req)
    assert [fr.timestamp for fr in out] == [step, 2 * step]


def test_fetch_ohlcv_history_by_stream(tmp_path):
    adapter = _adapter(tmp_path)
    rows = adapter.fetch_ohlcv_history(
        SYM, "1h", 0, 10**18, market_type=MarketType.LINEAR_PERP, price_type=PriceType.MARK
    )
    assert len(rows) == 4
    assert all(r.price_type is PriceType.MARK for r in rows)
    assert all(r.volume is None for r in rows)  # mark has no volume


def test_fetch_ohlcv_last_n(tmp_path):
    adapter = _adapter(tmp_path)
    rows = adapter.fetch_ohlcv(SYM, limit=2)
    assert len(rows) == 2
    assert rows[-1].timestamp == 3 * 3600 * 1000


def test_absent_symbol_raises_no_network_fallback(tmp_path):
    adapter = _adapter(tmp_path)
    with pytest.raises(KeyError):
        adapter.fetch_funding_rate("DOGE/USDT:USDT")
    with pytest.raises(KeyError):
        adapter.fetch_ohlcv_history(
            "DOGE/USDT:USDT", "1h", 0, 10**18,
            market_type=MarketType.LINEAR_PERP, price_type=PriceType.TRADE,
        )


def test_replay_adapter_is_concrete():
    assert ReplayVenueAdapter.__abstractmethods__ == frozenset()

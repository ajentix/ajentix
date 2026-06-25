from __future__ import annotations

import builtins

import pytest

from ajentix_quant.adapters.base import FundingRateHistoryRequest, MarketType, PriceType
from ajentix_quant.adapters.bybit import BybitAdapter, spot_symbol_from_perp

SYM = "BTC/USDT:USDT"


class FakeExchange:
    def __init__(self, *, funding_pages=None, ohlcv_pages=None):
        self._funding_pages = list(funding_pages or [])
        self._funding_index = 0
        self._ohlcv_pages = {key: list(value) for key, value in (ohlcv_pages or {}).items()}
        self._ohlcv_index: dict[str, int] = {}
        self.funding_calls = []
        self.ohlcv_calls = []

    def fetch_funding_rate_history(self, symbol, since=None, limit=None, params=None):
        self.funding_calls.append(
            {"symbol": symbol, "since": since, "limit": limit, "params": dict(params or {})}
        )
        if self._funding_index >= len(self._funding_pages):
            return []
        page = self._funding_pages[self._funding_index]
        self._funding_index += 1
        return page

    def fetch_ohlcv(self, symbol, timeframe=None, since=None, limit=None, params=None):
        params = dict(params or {})
        price = params.get("price", "trade")
        self.ohlcv_calls.append(
            {
                "symbol": symbol,
                "timeframe": timeframe,
                "since": since,
                "limit": limit,
                "params": params,
            }
        )
        pages = self._ohlcv_pages.get(price, [])
        page_index = self._ohlcv_index.get(price, 0)
        self._ohlcv_index[price] = page_index + 1
        if page_index >= len(pages):
            return []
        return pages[page_index]


def test_injected_exchange_does_not_import_ccxt(monkeypatch):
    real_import = builtins.__import__

    def blocked_import(name, *args, **kwargs):
        if name == "ccxt":
            raise AssertionError("ccxt should not be imported when exchange is injected")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", blocked_import)
    fake = FakeExchange()
    adapter = BybitAdapter(exchange=fake)
    assert adapter._ex is fake


def test_fetch_funding_rate_history_paginates_sorts_dedupes_and_clips():
    fake = FakeExchange(
        funding_pages=[
            [
                {"timestamp": 3000, "fundingRate": "0.003"},
                {"timestamp": 1000, "fundingRate": "0.001"},
                {"timestamp": 500, "fundingRate": "0.0005"},
                {"timestamp": 2000, "info": {"fundingRate": "0.002"}},
            ],
            [
                {"timestamp": 3000, "fundingRate": "0.999"},
                {"timestamp": 4000, "fundingRate": 0.004},
                {"timestamp": 6000, "fundingRate": "0.006"},
            ],
        ]
    )
    adapter = BybitAdapter(exchange=fake)

    out = adapter.fetch_funding_rate_history(
        FundingRateHistoryRequest(symbol=SYM, since_ms=1000, until_ms=4500, limit=500)
    )

    assert [row.timestamp for row in out] == [1000, 2000, 3000, 4000]
    assert [row.rate for row in out] == [0.001, 0.002, 0.003, 0.004]
    assert all(row.symbol == SYM and row.interval_hours == 8.0 for row in out)
    assert [call["since"] for call in fake.funding_calls] == [1000, 3001]
    assert all(call["limit"] == 200 for call in fake.funding_calls)
    assert all(call["params"] == {"until": 4500, "paginate": True} for call in fake.funding_calls)


def test_fetch_ohlcv_history_paginates_sorts_dedupes_clips_and_maps_trade_volume():
    fake = FakeExchange(
        ohlcv_pages={
            "trade": [
                [
                    [3000, 3, 4, 2, 3.5, "30"],
                    [1000, 1, 2, 0.5, 1.5, "10"],
                    [500, 0.5, 1, 0.1, 0.8, "5"],
                    [2000, 2, 3, 1.5, 2.5, "20"],
                ],
                [
                    [3000, 99, 100, 98, 99.5, "999"],
                    [4000, 4, 5, 3, 4.5, 40],
                    [6000, 6, 7, 5, 6.5, 60],
                ],
            ]
        }
    )
    adapter = BybitAdapter(exchange=fake)

    out = adapter.fetch_ohlcv_history(
        SYM,
        "1h",
        1000,
        4500,
        market_type=MarketType.LINEAR_PERP,
        price_type=PriceType.TRADE,
    )

    assert [row.timestamp_ms for row in out] == [1000, 2000, 3000, 4000]
    assert [row.volume for row in out] == [10.0, 20.0, 30.0, 40.0]
    assert all(row.symbol == SYM for row in out)
    assert all(row.market_type is MarketType.LINEAR_PERP for row in out)
    assert all(row.price_type is PriceType.TRADE for row in out)
    assert [call["since"] for call in fake.ohlcv_calls] == [1000, 3001]
    assert all(call["limit"] == 1000 for call in fake.ohlcv_calls)
    assert all(call["params"] == {} for call in fake.ohlcv_calls)


def test_fetch_ohlcv_history_maps_mark_and_index_params_and_omits_volume():
    fake = FakeExchange(
        ohlcv_pages={
            "mark": [[[1000, 1, 2, 0.5, 1.5, 999]]],
            "index": [[[1000, 2, 3, 1.5, 2.5, 888]]],
        }
    )
    adapter = BybitAdapter(exchange=fake)

    mark = adapter.fetch_ohlcv_history(
        SYM,
        "1h",
        1000,
        1000,
        market_type=MarketType.LINEAR_PERP,
        price_type=PriceType.MARK,
    )
    index = adapter.fetch_ohlcv_history(
        SYM,
        "1h",
        1000,
        1000,
        market_type=MarketType.LINEAR_PERP,
        price_type=PriceType.INDEX,
    )

    assert fake.ohlcv_calls[0]["params"] == {"price": "mark"}
    assert fake.ohlcv_calls[1]["params"] == {"price": "index"}
    assert mark[0].volume is None
    assert index[0].volume is None
    assert mark[0].price_type is PriceType.MARK
    assert index[0].price_type is PriceType.INDEX


def test_spot_symbol_from_perp_maps_linear_perp_and_rejects_spot_symbol():
    assert spot_symbol_from_perp("BTC/USDT:USDT") == "BTC/USDT"
    with pytest.raises(ValueError, match="settle suffix"):
        spot_symbol_from_perp("BTC/USDT")


def test_funding_history_page_that_does_not_advance_raises_runtime_error():
    fake = FakeExchange(
        funding_pages=[
            [{"timestamp": 1000, "fundingRate": "0.001"}],
            [{"timestamp": 1000, "fundingRate": "0.001"}],
        ]
    )
    adapter = BybitAdapter(exchange=fake)

    with pytest.raises(RuntimeError, match="pagination failed to advance"):
        adapter.fetch_funding_rate_history(
            FundingRateHistoryRequest(symbol=SYM, since_ms=1000, until_ms=2000)
        )


def test_ohlcv_history_page_that_does_not_advance_raises_runtime_error():
    # symmetric to the funding guard: a stalled OHLCV cursor must raise, not loop forever
    fake = FakeExchange(
        ohlcv_pages={
            "trade": [
                [[1000, 1, 2, 0.5, 1.5, "10"]],
                [[1000, 1, 2, 0.5, 1.5, "10"]],
            ]
        }
    )
    adapter = BybitAdapter(exchange=fake)
    with pytest.raises(RuntimeError, match="pagination failed to advance"):
        adapter.fetch_ohlcv_history(
            SYM, "1h", 1000, 5000,
            market_type=MarketType.LINEAR_PERP, price_type=PriceType.TRADE,
        )


def test_retag_symbol_preserves_market_type_and_only_changes_scenario_key():
    # _retag_symbol must re-key the spot leg under the perp scenario symbol WITHOUT
    # changing market_type (the leg discriminator) or any price field.
    import importlib.util
    import pathlib

    script = pathlib.Path(__file__).resolve().parents[1] / "scripts" / "populate_bybit_cache.py"
    spec = importlib.util.spec_from_file_location("populate_bybit_cache", script)
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)

    from ajentix_quant.adapters.base import HistoricalCandle

    spot = HistoricalCandle(
        timestamp_ms=1000, symbol="BTC/USDT", venue="bybit",
        market_type=MarketType.SPOT, price_type=PriceType.TRADE, timeframe="1h",
        open=1.0, high=2.0, low=0.5, close=1.5, volume=10.0,
    )
    out = mod._retag_symbol([spot], "BTC/USDT:USDT")
    assert out[0].symbol == "BTC/USDT:USDT"  # scenario key changed
    assert out[0].market_type is MarketType.SPOT  # leg discriminator preserved
    assert out[0].close == 1.5 and out[0].volume == 10.0  # prices untouched

from __future__ import annotations

import importlib
import sys
from pathlib import Path

import pytest

from ajentix_quant.adapters.base import (
    FundingRate,
    FundingRateHistoryRequest,
    HistoricalCandle,
    MarketType,
    PriceType,
    StreamKey,
    StreamName,
)
from ajentix_quant.adapters.bybit import BybitAdapter, spot_symbol_from_perp
from ajentix_quant.data.cache import DEFAULT_REQUIRED_STREAMS, load_dataset

SYM = "BTC/USDT:USDT"
SPOT_SYM = "BTC/USDT"


class FakeExchange:
    def __init__(self, *, funding_pages=(), ohlcv_pages=None, max_funding_calls=10):
        self._funding_pages = list(funding_pages)
        self._funding_index = 0
        self._ohlcv_pages = {key: list(value) for key, value in (ohlcv_pages or {}).items()}
        self._ohlcv_index: dict[str, int] = {}
        self.max_funding_calls = max_funding_calls
        self.funding_calls = []
        self.ohlcv_calls = []

    def fetch_funding_rate_history(self, symbol, since=None, limit=None, params=None):
        if len(self.funding_calls) >= self.max_funding_calls:
            raise AssertionError("funding pagination exceeded the fake exchange call bound")
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
        price_key = params.get("price", "trade")
        self.ohlcv_calls.append(
            {
                "symbol": symbol,
                "timeframe": timeframe,
                "since": since,
                "limit": limit,
                "params": params,
            }
        )
        pages = self._ohlcv_pages.get(price_key, [])
        page_index = self._ohlcv_index.get(price_key, 0)
        self._ohlcv_index[price_key] = page_index + 1
        if page_index >= len(pages):
            return []
        return pages[page_index]


class RepeatingFundingExchange:
    def __init__(self, page, *, max_calls=3):
        self.page = list(page)
        self.max_calls = max_calls
        self.funding_calls = []

    def fetch_funding_rate_history(self, symbol, since=None, limit=None, params=None):
        if len(self.funding_calls) >= self.max_calls:
            raise AssertionError("pagination guard did not stop the non-advancing fake")
        self.funding_calls.append(
            {"symbol": symbol, "since": since, "limit": limit, "params": dict(params or {})}
        )
        return list(self.page)


def _populate_module():
    scripts_dir = Path(__file__).resolve().parents[1] / "scripts"
    if str(scripts_dir) not in sys.path:
        sys.path.insert(0, str(scripts_dir))
    return importlib.import_module("populate_bybit_cache")


def _funding(symbol, timestamp):
    return FundingRate(symbol=symbol, rate=0.0001, interval_hours=8.0, timestamp=timestamp)


def _candle(symbol, timestamp, timeframe, market_type, price_type):
    return HistoricalCandle(
        timestamp_ms=timestamp,
        symbol=symbol,
        venue="bybit",
        market_type=market_type,
        price_type=price_type,
        timeframe=timeframe,
        open=100.0,
        high=101.0,
        low=99.0,
        close=100.5,
        volume=12.5 if price_type is PriceType.TRADE else None,
    )


def _stream_name(market_type, price_type):
    if market_type is MarketType.SPOT and price_type is PriceType.TRADE:
        return StreamName.SPOT_TRADE_OHLCV
    if market_type is MarketType.LINEAR_PERP and price_type is PriceType.TRADE:
        return StreamName.PERP_TRADE_OHLCV
    if market_type is MarketType.LINEAR_PERP and price_type is PriceType.MARK:
        return StreamName.PERP_MARK_OHLCV
    if market_type is MarketType.LINEAR_PERP and price_type is PriceType.INDEX:
        return StreamName.INDEX_OHLCV
    raise AssertionError(f"unexpected stream request: {market_type=} {price_type=}")


def _offline_adapter_class(*, empty_stream=None):
    class OfflineBybitAdapter:
        name = "bybit"
        instances = []

        def __init__(self, *args, **kwargs):
            self.init_args = args
            self.init_kwargs = kwargs
            self.fetches = []
            type(self).instances.append(self)

        def fetch_funding_rate_history(self, request):
            self.fetches.append(("funding", request.symbol, request.since_ms, request.until_ms))
            if empty_stream is StreamName.FUNDING_HISTORY:
                return []
            return [_funding(request.symbol, request.since_ms)]

        def fetch_ohlcv_history(
            self,
            symbol,
            timeframe,
            since_ms,
            until_ms,
            *,
            market_type,
            price_type,
        ):
            stream_name = _stream_name(market_type, price_type)
            self.fetches.append(("ohlcv", symbol, market_type, price_type, since_ms, until_ms))
            if empty_stream is stream_name:
                return []
            return [_candle(symbol, since_ms, timeframe, market_type, price_type)]

    return OfflineBybitAdapter


def test_funding_pagination_overlap_out_of_order_dedupes_and_clips_inclusive():
    fake = FakeExchange(
        funding_pages=[
            [
                {"timestamp": 3000, "fundingRate": "0.003"},
                {"timestamp": 1000, "fundingRate": "0.001"},
                {"timestamp": 500, "fundingRate": "0.0005"},
                {"timestamp": 2000, "info": {"fundingRate": "0.002"}},
            ],
            [
                {"timestamp": 5000, "fundingRate": "0.005"},
                {"timestamp": 3000, "fundingRate": "9.999"},
                {"timestamp": 4000, "fundingRate": "0.004"},
                {"timestamp": 6000, "fundingRate": "0.006"},
            ],
        ]
    )
    adapter = BybitAdapter(exchange=fake)

    out = adapter.fetch_funding_rate_history(
        FundingRateHistoryRequest(symbol=SYM, since_ms=1000, until_ms=5000, limit=500)
    )

    assert [row.timestamp for row in out] == [1000, 2000, 3000, 4000, 5000]
    assert [row.rate for row in out] == [0.001, 0.002, 0.003, 0.004, 0.005]
    assert all(row.symbol == SYM and row.interval_hours == 8.0 for row in out)
    assert [call["since"] for call in fake.funding_calls] == [1000, 3001]
    assert all(call["params"] == {"until": 5000, "paginate": True} for call in fake.funding_calls)


def test_funding_pagination_rejects_non_advancing_page_before_fake_bound():
    fake = RepeatingFundingExchange(
        [{"timestamp": 1000, "fundingRate": "0.001"}],
        max_calls=3,
    )
    adapter = BybitAdapter(exchange=fake)

    with pytest.raises(RuntimeError, match="pagination failed to advance"):
        adapter.fetch_funding_rate_history(
            FundingRateHistoryRequest(symbol=SYM, since_ms=1000, until_ms=2000)
        )

    assert [call["since"] for call in fake.funding_calls] == [1000, 1001]


def test_ohlcv_price_params_and_volume_semantics_for_trade_mark_and_index():
    fake = FakeExchange(
        ohlcv_pages={
            "trade": [[[1000, 1, 2, 0.5, 1.5, "10.5"], [2000, 2, 3, 1.5, 2.5, 0]]],
            "mark": [[[1000, 1, 2, 0.5, 1.5, 999], [2000, 2, 3, 1.5, 2.5, 888]]],
            "index": [[[1000, 1, 2, 0.5, 1.5, 777], [2000, 2, 3, 1.5, 2.5, 666]]],
        }
    )
    adapter = BybitAdapter(exchange=fake)

    trade = adapter.fetch_ohlcv_history(
        SYM,
        "1h",
        1000,
        2000,
        market_type=MarketType.LINEAR_PERP,
        price_type=PriceType.TRADE,
    )
    mark = adapter.fetch_ohlcv_history(
        SYM,
        "1h",
        1000,
        2000,
        market_type=MarketType.LINEAR_PERP,
        price_type=PriceType.MARK,
    )
    index = adapter.fetch_ohlcv_history(
        SYM,
        "1h",
        1000,
        2000,
        market_type=MarketType.LINEAR_PERP,
        price_type=PriceType.INDEX,
    )

    assert [row.volume for row in trade] == [10.5, 0.0]
    assert all(isinstance(row.volume, float) for row in trade)
    assert all(row.volume is None for row in [*mark, *index])
    assert [row.price_type for row in trade] == [PriceType.TRADE, PriceType.TRADE]
    assert [row.price_type for row in mark] == [PriceType.MARK, PriceType.MARK]
    assert [row.price_type for row in index] == [PriceType.INDEX, PriceType.INDEX]
    assert fake.ohlcv_calls[0]["params"] == {}
    assert "price" not in fake.ohlcv_calls[0]["params"]
    assert fake.ohlcv_calls[1]["params"] == {"price": "mark"}
    assert fake.ohlcv_calls[2]["params"] == {"price": "index"}


def test_spot_symbol_from_perp_maps_linear_perp_and_rejects_unsuffixed_symbol():
    assert spot_symbol_from_perp(SYM) == SPOT_SYM
    with pytest.raises(ValueError, match="settle suffix"):
        spot_symbol_from_perp("BTCUSDT")


def test_populate_bybit_cache_main_offline_writes_loadable_required_streams(monkeypatch, tmp_path):
    populate = _populate_module()
    monkeypatch.delenv("CI", raising=False)
    adapter_cls = _offline_adapter_class()
    monkeypatch.setattr(populate, "BybitAdapter", adapter_cls)

    scenario_id = "g003-offline"
    populate.main(
        [
            "--out",
            str(tmp_path),
            "--scenario-id",
            scenario_id,
            "--symbols",
            SYM,
            "--since",
            "2024-01-01T00:00:00Z",
            "--until",
            "2024-01-01T01:00:00Z",
            "--timeframe",
            "1h",
        ]
    )

    dataset = load_dataset(tmp_path, scenario_id)
    assert len(adapter_cls.instances) == 1
    assert len(DEFAULT_REQUIRED_STREAMS) == 4
    assert dataset.symbols == (SYM,)
    assert dataset.funding[SYM]
    assert dataset.ohlcv[StreamKey(SYM, MarketType.SPOT, PriceType.TRADE)]
    assert dataset.ohlcv[StreamKey(SYM, MarketType.LINEAR_PERP, PriceType.TRADE)]
    assert dataset.ohlcv[StreamKey(SYM, MarketType.LINEAR_PERP, PriceType.MARK)]
    assert dataset.ohlcv[StreamKey(SYM, MarketType.LINEAR_PERP, PriceType.INDEX)]
    assert all(row.symbol == SYM for rows in dataset.ohlcv.values() for row in rows)


def test_populate_bybit_cache_refuses_to_run_under_ci_before_fetching(monkeypatch, tmp_path):
    populate = _populate_module()

    class ExplodingAdapter:
        instantiated = False

        def __init__(self, *args, **kwargs):
            type(self).instantiated = True
            raise AssertionError("CI guard allowed adapter construction")

    monkeypatch.setattr(populate, "BybitAdapter", ExplodingAdapter)
    monkeypatch.setenv("CI", "1")

    with pytest.raises(SystemExit) as exc_info:
        populate.main(["--out", str(tmp_path), "--scenario-id", "ci-blocked"])

    assert "must not run in CI" in str(exc_info.value)
    assert ExplodingAdapter.instantiated is False


def test_populate_bybit_cache_exits_one_when_required_perp_mark_stream_empty(
    monkeypatch,
    tmp_path,
    capsys,
):
    populate = _populate_module()
    monkeypatch.delenv("CI", raising=False)
    adapter_cls = _offline_adapter_class(empty_stream=StreamName.PERP_MARK_OHLCV)
    monkeypatch.setattr(populate, "BybitAdapter", adapter_cls)

    scenario_id = "g003-empty-mark"
    with pytest.raises(SystemExit) as exc_info:
        populate.main(
            [
                "--out",
                str(tmp_path),
                "--scenario-id",
                scenario_id,
                "--symbols",
                SYM,
                "--since",
                "2024-01-01T00:00:00Z",
                "--until",
                "2024-01-01T01:00:00Z",
            ]
        )

    captured = capsys.readouterr()
    assert exc_info.value.code == 1
    assert "missing required streams" in captured.err
    assert f"{SYM}:{StreamName.PERP_MARK_OHLCV.value}" in captured.err
    assert not (tmp_path / scenario_id / "manifest.json").exists()
    assert len(adapter_cls.instances) == 1

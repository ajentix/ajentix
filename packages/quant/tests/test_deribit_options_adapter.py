from __future__ import annotations

import builtins

from ajentix_quant.adapters.base import SourceQuality
from ajentix_quant.adapters.deribit_options import DeribitOptionsAdapter
from ajentix_quant.options.types import OptionChainSnapshot, OptionType, Side

EXPIRY_MS = 1719532800000
TS_MS = 1717200000000


class FakeDeribitClient:
    def __init__(self) -> None:
        self.market_calls = 0
        self.ticker_calls: list[str] = []
        self.markets = [
            {
                "id": "ETH-28JUN24-3000-P",
                "symbol": "ETH-28JUN24-3000-P",
                "base": "ETH",
                "type": "option",
                "option": True,
                "expiry": EXPIRY_MS,
                "strike": 3000.0,
                "optionType": "put",
                "contractSize": 1.0,
                "precision": {"price": 0.0005},
                "limits": {"amount": {"min": 1.0}},
                "info": {
                    "instrument_name": "ETH-28JUN24-3000-P",
                    "settlement_index": "ETH-USD",
                    "quote_currency": "ETH",
                    "settlement_currency": "ETH",
                },
            },
            {
                "id": "ETH-28JUN24-3600-C",
                "symbol": "ETH-28JUN24-3600-C",
                "base": "ETH",
                "type": "option",
                "option": True,
                "expiry": EXPIRY_MS,
                "strike": 3600.0,
                "optionType": "call",
                "contractSize": 1.0,
                "precision": {"price": 0.0005},
                "limits": {"amount": {"min": 1.0}},
                "info": {
                    "instrument_name": "ETH-28JUN24-3600-C",
                    "settlement_index": "ETH-USD",
                    "quote_currency": "ETH",
                    "settlement_currency": "ETH",
                },
            },
        ]
        self.tickers = {
            "ETH-28JUN24-3000-P": {
                "timestamp": TS_MS,
                "bid": 0.041,
                "ask": 0.045,
                "bidVolume": 4.0,
                "askVolume": 5.0,
                "bidIv": 0.62,
                "askIv": 0.65,
                "markPrice": 0.043,
                "indexPrice": 3450.0,
                "underlyingPrice": 3450.0,
            },
            "ETH-28JUN24-3600-C": {
                "timestamp": TS_MS,
                "bid": 0.052,
                "ask": 0.058,
                "bidVolume": 3.0,
                "askVolume": 6.0,
                "bidIv": 0.61,
                "askIv": 0.64,
                "markPrice": 0.055,
                "indexPrice": 3450.0,
                "underlyingPrice": 3450.0,
            },
        }

    def fetch_markets(self):
        self.market_calls += 1
        return list(self.markets)

    def fetch_ticker(self, symbol):
        self.ticker_calls.append(symbol)
        return dict(self.tickers[symbol])


def test_injected_client_does_not_import_ccxt(monkeypatch):
    real_import = builtins.__import__

    def blocked_import(name, *args, **kwargs):
        if name == "ccxt":
            raise AssertionError("ccxt should not be imported when client is injected")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", blocked_import)
    fake = FakeDeribitClient()
    adapter = DeribitOptionsAdapter(client=fake, source_quality=SourceQuality.FIXTURE)

    assert adapter.available_expiries("ETH") == (EXPIRY_MS,)
    assert fake.market_calls == 1


def test_read_only_public_surface_has_no_order_or_account_methods():
    adapter = DeribitOptionsAdapter(client=FakeDeribitClient())
    for name in (
        "cancel_order",
        "create_order",
        "edit_order",
        "fetch_balance",
        "fetch_orders",
        "private_get_account_summary",
        "withdraw",
    ):
        assert not hasattr(DeribitOptionsAdapter, name)
        assert not hasattr(adapter, name)


def test_instrument_metadata_maps_deribit_option_markets_deterministically():
    adapter = DeribitOptionsAdapter(
        client=FakeDeribitClient(), source_quality=SourceQuality.FIXTURE
    )

    metadata = adapter.instrument_metadata("ETH")

    assert metadata["exchange"] == "deribit"
    assert metadata["underlying"] == "ETH"
    assert metadata["source_quality"] == SourceQuality.FIXTURE.value
    assert metadata["expiries_ms"] == [EXPIRY_MS]
    assert [row["instrument_name"] for row in metadata["instruments"]] == [
        "ETH-28JUN24-3000-P",
        "ETH-28JUN24-3600-C",
    ]
    assert metadata["instruments"][0]["option_type"] == OptionType.PUT.value
    assert metadata["instruments"][1]["option_type"] == OptionType.CALL.value


def test_chain_snapshot_uses_public_tickers_and_rate_limit_callback():
    fake = FakeDeribitClient()
    sleep_calls: list[float] = []
    adapter = DeribitOptionsAdapter(
        client=fake,
        source_quality=SourceQuality.FIXTURE,
        rate_limit_s=0.01,
        sleep=sleep_calls.append,
    )

    snapshot = adapter.chain_snapshot("ETH", TS_MS, EXPIRY_MS)

    assert isinstance(snapshot, OptionChainSnapshot)
    assert snapshot.exchange == "deribit"
    assert snapshot.settlement_index_price == 3450.0
    assert snapshot.index_price == 3450.0
    assert snapshot.source_quality_map["option_chain"] is SourceQuality.FIXTURE
    assert [leg.instrument_name for leg in snapshot.legs] == [
        "ETH-28JUN24-3000-P",
        "ETH-28JUN24-3600-C",
    ]
    put = snapshot.legs[0]
    assert put.option_type is OptionType.PUT
    assert put.side is Side.LONG
    assert put.bid_price == 0.041
    assert put.ask_price == 0.045
    assert put.bid_amount == 4.0
    assert put.ask_amount == 5.0
    assert fake.ticker_calls == ["ETH-28JUN24-3000-P", "ETH-28JUN24-3600-C"]
    assert sleep_calls == [0.01, 0.01]

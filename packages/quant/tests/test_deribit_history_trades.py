from __future__ import annotations

import json
from pathlib import Path

import pytest

from ajentix_quant.adapters.deribit_history import (
    DeribitHistoryPaginationError,
    DeribitHistoryTradeProvider,
)
from ajentix_quant.data.vrp_free_history_cache import (
    VrpFreeHistoryCacheValidationError,
    parse_deribit_history_trade,
)

FIXTURE_PATH = (
    Path(__file__).parent
    / "fixtures"
    / "vrp_free_history"
    / "eth_option_trades_fixture.jsonl"
)


def _fixture_rows() -> list[dict]:
    return [json.loads(line) for line in FIXTURE_PATH.read_text(encoding="utf-8").splitlines()]


class FakeHistoryClient:
    def __init__(self, pages):
        self.pages = list(pages)
        self.calls: list[dict] = []

    def get_last_trades_by_currency_and_time(self, params):
        self.calls.append(dict(params))
        assert self.pages, "unexpected extra fake Deribit request"
        return self.pages.pop(0)


def test_provider_paginates_with_injected_client_and_sleep():
    rows = _fixture_rows()
    sleeps: list[float] = []
    client = FakeHistoryClient(
        [
            {"result": {"trades": rows[:4], "has_more": True}},
            {"result": {"trades": rows[4:], "has_more": False}},
        ]
    )
    provider = DeribitHistoryTradeProvider(
        client=client,
        rate_limit_s=0.125,
        sleep=sleeps.append,
    )

    fetched = provider.fetch_option_trades(
        currency="ETH",
        start_timestamp_ms=1725148800000,
        end_timestamp_ms=1725206400000,
        count=4,
        chunk_ms=24 * 60 * 60 * 1000,
    )

    assert [row["trade_id"] for row in fetched] == [row["trade_id"] for row in rows]
    assert len(client.calls) == 2
    assert client.calls[0] == {
        "currency": "ETH",
        "kind": "option",
        "start_timestamp": 1725148800000,
        "end_timestamp": 1725206400000,
        "count": 4,
        "sorting": "asc",
    }
    assert client.calls[1]["start_timestamp"] == 1725177600000
    assert sleeps == [0.125, 0.125]


def test_provider_has_no_order_account_or_private_surface():
    forbidden = ("order", "account", "private", "withdraw", "deposit", "position")
    public_names = [name for name in dir(DeribitHistoryTradeProvider) if not name.startswith("_")]

    assert public_names
    assert not [name for name in public_names if any(token in name.lower() for token in forbidden)]


def test_strict_trade_parser_extracts_deribit_instrument_and_required_fields():
    trade = parse_deribit_history_trade(_fixture_rows()[0])

    assert trade.trade_id == "fixture-eth-opt-0001"
    assert trade.underlying == "ETH"
    assert trade.expiry_token == "27SEP24"
    assert trade.expiry_ms == 1727424000000
    assert trade.strike == 2400.0
    assert trade.option_type == "put"
    assert trade.timestamp_ms == 1725148800000
    assert trade.price == 0.052
    assert trade.mark_price == 0.053
    assert trade.iv == 65.2
    assert trade.index_price == 2500.0
    assert trade.amount == 12.5
    assert trade.contracts == 12.5
    assert trade.direction == "buy"
    assert 26.0 < trade.dte_days < 27.0


def test_parser_is_deterministic_for_mapping_order():
    row = _fixture_rows()[0]
    reversed_row = dict(reversed(list(row.items())))

    assert parse_deribit_history_trade(row) == parse_deribit_history_trade(reversed_row)


@pytest.mark.parametrize("field", ["iv", "index_price", "amount"])
def test_parser_fails_closed_on_missing_required_trade_fields(field):
    row = _fixture_rows()[0]
    row.pop(field)

    with pytest.raises(VrpFreeHistoryCacheValidationError, match=field):
        parse_deribit_history_trade(row)


@pytest.mark.parametrize(
    "field,value,match",
    [
        ("instrument_name", "ETH-BAD-2400-P", "instrument_name"),
        ("iv", float("nan"), "finite"),
        ("index_price", 0.0, "positive"),
        ("amount", 0.0, "positive"),
        ("direction", "hold", "buy/sell"),
    ],
)
def test_parser_fails_closed_on_malformed_trade_values(field, value, match):
    row = _fixture_rows()[0]
    row[field] = value

    with pytest.raises(VrpFreeHistoryCacheValidationError, match=match):
        parse_deribit_history_trade(row)

class WindowFakeHistoryClient:
    """Fake Deribit client that serves trades by the requested time window.

    Unlike ``FakeHistoryClient`` (which pops scripted pages), this models the real
    endpoint: it returns ascending trades with ``timestamp`` in the requested
    ``[start_timestamp, end_timestamp]`` window, capped at ``count``, and sets
    ``has_more`` truthfully. This exercises the boundary-overlap re-fetch path.
    """

    def __init__(self, trades):
        self._trades = sorted(
            (dict(t) for t in trades),
            key=lambda r: (int(r["timestamp"]), str(r["trade_id"])),
        )
        self.calls: list[dict] = []

    def get_last_trades_by_currency_and_time(self, params):
        self.calls.append(dict(params))
        start = int(params["start_timestamp"])
        end = int(params["end_timestamp"])
        count = int(params["count"])
        window = [t for t in self._trades if start <= int(t["timestamp"]) <= end]
        page = window[:count]
        return {
            "result": {
                "trades": [dict(t) for t in page],
                "has_more": len(window) > count,
            }
        }


def _make_trade(trade_id, timestamp, trade_seq):
    return {
        "trade_id": trade_id,
        "timestamp": timestamp,
        "trade_seq": trade_seq,
        "instrument_name": "ETH-FIXTURE-OPT",
    }


def test_pagination_overlaps_and_dedupes_boundary_timestamp():
    # Three trades share the boundary timestamp 2_000; the page cut at count=4 forces
    # a second request that re-fetches the boundary. The overlap+dedupe path must
    # return every trade exactly once with none skipped or duplicated.
    trades = [
        _make_trade("t-a", 1_000, 1),
        _make_trade("t-b", 2_000, 2),
        _make_trade("t-c", 2_000, 3),
        _make_trade("t-d", 2_000, 4),
        _make_trade("t-e", 3_000, 5),
    ]
    client = WindowFakeHistoryClient(trades)
    provider = DeribitHistoryTradeProvider(client=client, sleep=lambda _s: None)

    rows = provider.fetch_option_trades(
        currency="ETH",
        start_timestamp_ms=1_000,
        end_timestamp_ms=3_000,
        count=4,
        chunk_ms=10_000,
    )

    ids = [row["trade_id"] for row in rows]
    assert ids == ["t-a", "t-b", "t-c", "t-d", "t-e"]
    assert len(ids) == len(set(ids))
    assert len(client.calls) >= 2
    # The boundary cursor overlaps (max_ts), it does not jump to max_ts + 1.
    assert client.calls[1]["start_timestamp"] == 2_000


def test_pagination_fails_closed_on_full_single_timestamp_page():
    # count=2 but three trades all share timestamp 2_000: a full page collapsed onto a
    # single millisecond that time-window pagination cannot disambiguate. The provider
    # must fail closed rather than silently drop the unseen same-timestamp trade.
    trades = [
        _make_trade("t-a", 2_000, 1),
        _make_trade("t-b", 2_000, 2),
        _make_trade("t-c", 2_000, 3),
    ]
    client = WindowFakeHistoryClient(trades)
    provider = DeribitHistoryTradeProvider(client=client, sleep=lambda _s: None)

    with pytest.raises(DeribitHistoryPaginationError):
        provider.fetch_option_trades(
            currency="ETH",
            start_timestamp_ms=1_000,
            end_timestamp_ms=3_000,
            count=2,
            chunk_ms=10_000,
        )

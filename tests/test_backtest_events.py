from dataclasses import FrozenInstanceError

import pytest

from ajentix_quant.backtest.events import EventKind, LedgerEvent


def test_event_kind_values_are_stable_strings() -> None:
    assert EventKind.ENTRY == "entry"
    assert EventKind.LIQUIDATION == "liquidation"
    assert EventKind.DELEVERAGE == "deleverage"


def test_ledger_event_is_typed_frozen_and_validates_symbol() -> None:
    event = LedgerEvent(
        timestamp_ms=123,
        kind=EventKind.FUNDING,
        symbol="BTC/USDT:USDT",
        amount="1.25000000",
        equity_before="1000.00000000",
        equity_after="1001.25000000",
    )

    assert event.kind is EventKind.FUNDING
    assert event.amount == "1.25000000"
    with pytest.raises(FrozenInstanceError):
        event.amount = "0"  # type: ignore[misc]
    with pytest.raises(ValueError, match="symbol"):
        LedgerEvent(timestamp_ms=1, kind=EventKind.ENTRY, symbol="")

"""Typed deterministic ledger events emitted by the two-leg backtest."""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from enum import StrEnum


class EventKind(StrEnum):
    ENTRY = "entry"
    EXIT = "exit"
    FORCED_EXIT = "forced_exit"
    REBALANCE = "rebalance"
    FUNDING = "funding"
    LEVERAGE_COST = "leverage_cost"
    DELEVERAGE = "deleverage"
    LIQUIDATION = "liquidation"
    KILL_SWITCH = "kill_switch"


EventNumber = Decimal | float | int | str


@dataclass(frozen=True)
class LedgerEvent:
    """One immutable account-ledger event with canonical numeric payload slots."""

    timestamp_ms: int
    kind: EventKind
    symbol: str
    reason: str = ""
    amount: EventNumber | None = None
    notional: EventNumber | None = None
    leverage: EventNumber | None = None
    net_delta: EventNumber | None = None
    equity_before: EventNumber | None = None
    equity_after: EventNumber | None = None
    spot_price: EventNumber | None = None
    perp_mark: EventNumber | None = None

    def __post_init__(self) -> None:
        if self.timestamp_ms < 0:
            raise ValueError("timestamp_ms must be non-negative")
        if not self.symbol:
            raise ValueError("symbol must be non-empty")

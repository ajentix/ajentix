"""Read-only option-chain provider protocol for VRP data flows."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any, Protocol, runtime_checkable

from ajentix_quant.options.types import OptionChainSnapshot


@runtime_checkable
class OptionChainProvider(Protocol):
    """Narrow market-data surface for normalized option chains.

    The live Deribit public-data adapter implements this protocol only to populate the
    deterministic cache. The no-network replay provider implements the same protocol for
    offline analysis. Strategy and backtest code depend on this protocol and on
    ``OptionChainSnapshot`` values; they must not depend on venue adapters, order/account
    plumbing, raw file loaders, or network clients.
    """

    def available_expiries(self, underlying: str) -> tuple[int, ...]:
        """Return available option expiry timestamps in UTC epoch milliseconds."""
        ...

    def chain_snapshot(
        self, underlying: str, ts_ms: int, expiry_ms: int
    ) -> OptionChainSnapshot:
        """Return one normalized chain snapshot for ``underlying`` and ``expiry_ms``."""
        ...

    def instrument_metadata(self, underlying: str) -> Mapping[str, Any]:
        """Return deterministic instrument metadata for cache/replay validation only."""
        ...


__all__ = ["OptionChainProvider"]

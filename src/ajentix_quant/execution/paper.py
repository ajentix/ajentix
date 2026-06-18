"""Paper / dry-run executor.

Records intended delta-neutral carry orders without sending anything live. Live execution
(Phase 2) will place orders through a VenueAdapter using TRADE-ONLY API keys
(NO withdrawal permission).
"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class PaperExecutor:
    log: list[str] = field(default_factory=list)

    def open_carry(self, symbol: str, notional_usd: float, leverage: float) -> None:
        self.log.append(
            f"[PAPER] OPEN carry {symbol} notional=${notional_usd:.2f} lev={leverage:.2f}x "
            f"(long spot + short perp, net delta 0)"
        )

    def close_carry(self, symbol: str) -> None:
        self.log.append(f"[PAPER] CLOSE carry {symbol}")

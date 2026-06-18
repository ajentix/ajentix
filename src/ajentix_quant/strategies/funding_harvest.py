"""Delta-neutral funding harvest.

Hold long spot + short perpetual on the same asset (net delta = 0) and collect positive
funding. Enter when the 8h funding rate clears a threshold that covers expected costs.
Deterministic — no LLM in the path.
"""

from __future__ import annotations

from .base import Signal, Strategy


class FundingHarvest(Strategy):
    name = "funding_harvest"

    def __init__(self, min_funding_rate_8h: float = 0.0001) -> None:
        self.min_funding_rate_8h = min_funding_rate_8h

    def signal(self, *, symbol: str, funding_rate_8h: float) -> Signal:
        if funding_rate_8h >= self.min_funding_rate_8h:
            return Signal(
                symbol=symbol,
                enter=True,
                target_delta=0.0,
                reason=(
                    f"8h funding {funding_rate_8h:.4%} >= threshold "
                    f"{self.min_funding_rate_8h:.4%}; hold delta-neutral carry"
                ),
            )
        return Signal(
            symbol=symbol,
            enter=False,
            target_delta=0.0,
            reason=f"8h funding {funding_rate_8h:.4%} below threshold; stay flat",
        )

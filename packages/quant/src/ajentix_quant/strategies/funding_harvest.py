"""Delta-neutral funding harvest.

Hold long spot + short perpetual on the same asset (net delta = 0) and collect positive
funding. Enter when the 8h funding rate clears a threshold that covers expected costs.
Deterministic — no LLM in the path.
"""

from __future__ import annotations

import math

from .base import Signal, Strategy
from .sizing import SmallCapitalSizingPolicy
from .state import CarrySignal, MarketState, SignalAction


class FundingHarvest(Strategy):
    name = "funding_harvest"
    _BASE_LEVERAGE_CAP = 3.0
    _BPS_EPSILON = 1e-12

    def __init__(
        self,
        min_funding_rate_8h: float = 0.0001,
        *,
        funding_compression_8h: float = 0.00005,
        funding_reversal_imminent: float = 0.0,
        max_net_delta_frac: float = 0.02,
        basis_dislocation_bps: float = 50.0,
    ) -> None:
        _require_finite_non_negative("min_funding_rate_8h", min_funding_rate_8h)
        _require_finite_non_negative("funding_compression_8h", funding_compression_8h)
        _require_finite_non_negative("funding_reversal_imminent", funding_reversal_imminent)
        _require_finite_non_negative("max_net_delta_frac", max_net_delta_frac)
        _require_finite_non_negative("basis_dislocation_bps", basis_dislocation_bps)
        self.min_funding_rate_8h = min_funding_rate_8h
        self.funding_compression_8h = funding_compression_8h
        self.funding_reversal_imminent = funding_reversal_imminent
        self.max_net_delta_frac = max_net_delta_frac
        self.basis_dislocation_bps = basis_dislocation_bps

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

    def decide(
        self,
        state: MarketState,
        *,
        sizing: SmallCapitalSizingPolicy | None = None,
        hold_intervals: int = 3,
        safety_margin_bps: float = 1.0,
    ) -> CarrySignal:
        state.validate()
        if isinstance(hold_intervals, bool) or not isinstance(hold_intervals, int):
            raise ValueError("hold_intervals must be an integer")
        if hold_intervals <= 0:
            raise ValueError("hold_intervals must be positive")
        _require_finite_non_negative("safety_margin_bps", safety_margin_bps)

        policy = sizing or SmallCapitalSizingPolicy()

        if state.in_position:
            return self._decide_position(state, policy)
        return self._decide_flat(
            state,
            policy,
            hold_intervals=hold_intervals,
            safety_margin_bps=safety_margin_bps,
        )

    def _decide_position(
        self,
        state: MarketState,
        sizing: SmallCapitalSizingPolicy,
    ) -> CarrySignal:
        if state.risk_deleverage:
            return _carry_signal(
                state,
                SignalAction.EXIT,
                reason="risk deleverage/kill/liq-buffer; exit carry",
            )
        if state.funding_rate <= self.funding_reversal_imminent:
            return _carry_signal(
                state,
                SignalAction.EXIT,
                reason="funding reversal/negative; exit carry",
            )
        if 0.0 <= state.funding_rate < self.funding_compression_8h:
            return _carry_signal(
                state,
                SignalAction.EXIT,
                reason="funding compression; exit carry",
            )
        if abs(state.net_delta_frac) > self.max_net_delta_frac:
            return _carry_signal(
                state,
                SignalAction.EXIT,
                reason="net-delta drift; exit carry",
            )
        if abs(state.basis_bps) > self.basis_dislocation_bps:
            return _carry_signal(
                state,
                SignalAction.EXIT,
                reason="basis dislocation; exit carry",
            )

        target_leverage = state.current_leverage
        target_notional = 0.0
        if target_leverage > 0.0:
            target_notional = sizing.size(
                equity_usd=state.equity_usd,
                leverage=target_leverage,
            )
        return _carry_signal(
            state,
            SignalAction.HOLD,
            target_notional_usd=target_notional,
            target_leverage=target_leverage,
            reason=(
                f"carry remains valid: funding {state.funding_rate:.4%}, "
                f"net delta {state.net_delta_frac:.4f}, basis {state.basis_bps:.2f} bps"
            ),
        )

    def _decide_flat(
        self,
        state: MarketState,
        sizing: SmallCapitalSizingPolicy,
        *,
        hold_intervals: int,
        safety_margin_bps: float,
    ) -> CarrySignal:
        if state.funding_rate < self.min_funding_rate_8h:
            return _carry_signal(
                state,
                SignalAction.FLAT,
                reason="funding below threshold; stay flat",
            )
        if state.gap_survival_leverage_cap < 1.0:
            return _carry_signal(
                state,
                SignalAction.FLAT,
                reason="no safe leverage (gap cap < 1x); stay flat",
            )

        target_leverage = max(1.0, min(state.gap_survival_leverage_cap, self._BASE_LEVERAGE_CAP))
        expected_capture_bps = state.funding_rate * hold_intervals * target_leverage * 1e4
        required_bps = state.expected_cost_bps + safety_margin_bps
        if expected_capture_bps <= required_bps + self._BPS_EPSILON:
            return _carry_signal(
                state,
                SignalAction.FLAT,
                reason=(
                    "expected carry below cost+margin; stay flat "
                    f"(capture {expected_capture_bps:.2f} bps <= cost "
                    f"{state.expected_cost_bps:.2f} bps + margin {safety_margin_bps:.2f} bps)"
                ),
            )

        target_notional = sizing.size(
            equity_usd=state.equity_usd,
            leverage=target_leverage,
        )
        if not sizing.feasible(
            equity_usd=state.equity_usd,
            leverage=target_leverage,
        ):
            return _carry_signal(
                state,
                SignalAction.FLAT,
                reason="min-notional infeasible at this capital; stay flat",
            )

        return _carry_signal(
            state,
            SignalAction.ENTER,
            target_notional_usd=target_notional,
            target_leverage=target_leverage,
            reason=(
                f"expected carry {expected_capture_bps:.2f} bps > cost "
                f"{state.expected_cost_bps:.2f} bps + margin {safety_margin_bps:.2f} bps "
                f"over {hold_intervals} intervals; enter delta-neutral carry"
            ),
        )


def _carry_signal(
    state: MarketState,
    action: SignalAction,
    *,
    target_notional_usd: float = 0.0,
    target_leverage: float = 0.0,
    reason: str,
) -> CarrySignal:
    return CarrySignal(
        symbol=state.symbol,
        action=action,
        target_notional_usd=target_notional_usd,
        target_leverage=target_leverage,
        target_net_delta=0.0,
        reason=reason,
    )


def _require_finite_non_negative(name: str, value: float) -> None:
    if not math.isfinite(value):
        raise ValueError(f"{name} must be finite")
    if value < 0.0:
        raise ValueError(f"{name} must be non-negative")
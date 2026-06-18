"""Deterministic funding-harvest backtest over an 8h funding-rate series.

Because the carry is delta-neutral (long spot + short perp), price legs cancel to first
order, so per-period PnL ~= funding_captured * leverage - costs. This Phase 0 harness
models entry/exit fees, leverage, and a funding-reversal forced exit. Full methodology
(walk-forward, regime split, slippage-by-size, Monte Carlo) is Phase 1.
"""

from __future__ import annotations

from dataclasses import dataclass

from ..risk.engine import RiskEngine
from ..strategies.funding_harvest import FundingHarvest
from . import metrics

# 8h funding => 3 settlements/day.
_PERIODS_PER_YEAR = 365 * 3


@dataclass
class BacktestResult:
    sharpe: float
    sortino: float
    ann_return: float
    max_drawdown: float
    n_periods: int
    n_entries: int
    final_equity: float


@dataclass
class FundingBacktest:
    strategy: FundingHarvest
    risk: RiskEngine
    fee_round_trip: float = 0.0011  # ~0.11% across entry + exit, both legs
    periods_per_year: float = _PERIODS_PER_YEAR

    def run(self, funding_8h: list[float], realized_vol_annual: float = 0.5) -> BacktestResult:
        equity = 1.0
        curve = [equity]
        rets: list[float] = []
        in_pos = False
        entries = 0
        hours_negative = 0.0
        entry_fee = self.fee_round_trip / 2.0
        exit_fee = self.fee_round_trip / 2.0

        for f in funding_8h:
            sig = self.strategy.signal(symbol="SAMPLE", funding_rate_8h=f)
            lev = self.risk.dynamic_leverage(
                realized_vol_annual=realized_vol_annual, funding_rate_8h=f
            )
            period_ret = 0.0

            want_position = sig.enter and self.risk.liquidation_distance_ok(leverage=lev)

            if want_position:
                if not in_pos:
                    period_ret -= entry_fee
                    in_pos = True
                    entries += 1
                period_ret += f * lev  # funding captured on levered, delta-neutral notional
                hours_negative = 0.0
            elif in_pos:
                period_ret -= exit_fee
                in_pos = False

            # funding-reversal forced exit
            if f < 0:
                hours_negative += 8.0
                if in_pos and self.risk.should_exit_funding_reversal(hours_negative=hours_negative):
                    period_ret -= exit_fee
                    in_pos = False

            rets.append(period_ret)
            equity *= 1.0 + period_ret
            curve.append(equity)

        return BacktestResult(
            sharpe=metrics.sharpe(rets, self.periods_per_year),
            sortino=metrics.sortino(rets, self.periods_per_year),
            ann_return=metrics.annualized_return(rets, self.periods_per_year),
            max_drawdown=metrics.max_drawdown(curve),
            n_periods=len(rets),
            n_entries=entries,
            final_equity=equity,
        )

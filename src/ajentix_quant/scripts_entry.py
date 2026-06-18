"""Console-script entry points."""

from __future__ import annotations

from .backtest.engine import FundingBacktest
from .data.sample import sample_funding_8h
from .risk.engine import RiskEngine
from .strategies.funding_harvest import FundingHarvest


def run_backtest_main() -> None:
    strat = FundingHarvest(min_funding_rate_8h=0.0001)
    bt = FundingBacktest(strategy=strat, risk=RiskEngine())
    res = bt.run(sample_funding_8h())
    print("=== ajentix-quant — funding-harvest backtest (sample data) ===")
    print(f"periods={res.n_periods}  entries={res.n_entries}")
    print(
        f"ann_return={res.ann_return:.2%}  sharpe={res.sharpe:.2f}  "
        f"sortino={res.sortino:.2f}  max_drawdown={res.max_drawdown:.2%}"
    )
    print(f"final_equity={res.final_equity:.4f}")
    print("NOTE: Phase 0 skeleton on synthetic data — not a live performance claim.")


if __name__ == "__main__":
    run_backtest_main()

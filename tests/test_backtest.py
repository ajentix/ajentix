from ajentix_quant.backtest.engine import BacktestResult, FundingBacktest
from ajentix_quant.data.sample import sample_funding_8h
from ajentix_quant.risk.engine import RiskEngine
from ajentix_quant.strategies.funding_harvest import FundingHarvest


def _run() -> BacktestResult:
    bt = FundingBacktest(strategy=FundingHarvest(0.0001), risk=RiskEngine())
    return bt.run(sample_funding_8h())


def test_backtest_runs_end_to_end():
    res = _run()
    assert res.n_periods == 120
    assert res.n_entries >= 1  # it took at least one carry position
    assert res.final_equity > 0.0


def test_backtest_is_deterministic():
    a, b = _run(), _run()
    assert a.final_equity == b.final_equity
    assert a.sharpe == b.sharpe


def test_backtest_metrics_present():
    res = _run()
    # metrics computed without error and finite
    for v in (res.sharpe, res.sortino, res.ann_return, res.max_drawdown):
        assert v == v  # not NaN

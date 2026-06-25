import math

from ajentix_quant.backtest import metrics


def test_max_drawdown_basic():
    # peak 1.2 then trough 0.9 -> dd = 0.25
    curve = [1.0, 1.2, 0.9, 1.1]
    assert abs(metrics.max_drawdown(curve) - 0.25) < 1e-9


def test_max_drawdown_monotonic_up_is_zero():
    assert metrics.max_drawdown([1.0, 1.1, 1.2, 1.3]) == 0.0


def test_sharpe_zero_variance_is_zero():
    assert metrics.sharpe([0.01, 0.01, 0.01], periods_per_year=365) == 0.0


def test_sharpe_positive_for_positive_mean():
    rets = [0.01, 0.005, 0.012, 0.008, 0.011]
    assert metrics.sharpe(rets, periods_per_year=365 * 3) > 0.0


def test_annualized_return_compounds():
    # constant 1% per period, 3 periods/year -> ~ (1.01)^3 - 1
    r = metrics.annualized_return([0.01], periods_per_year=3)
    assert abs(r - ((1.01**3) - 1.0)) < 1e-9


def test_sortino_ignores_upside_volatility():
    # no downside -> dstd 0 -> 0.0 by convention
    assert metrics.sortino([0.01, 0.02, 0.03], periods_per_year=365) == 0.0


def test_calmar_zero_drawdown_conventions_and_negative_return():
    assert metrics.calmar(0.12, 0.0) == math.inf
    assert metrics.calmar(0.0, 0.0) == 0.0
    assert metrics.calmar(-0.12, 0.0) == -math.inf
    assert metrics.calmar(-0.12, 0.03) == -4.0


def test_win_rate_edges_and_break_even_is_not_win():
    assert metrics.win_rate([]) == 0.0
    assert metrics.win_rate([0.01, 0.02]) == 1.0
    assert metrics.win_rate([-0.01, -0.02]) == 0.0
    assert metrics.win_rate([0.01, 0.0, -0.01, 0.02]) == 0.5


def test_funding_capture_zero_available_and_negative_captured():
    assert metrics.funding_capture(1.0, 0.0) == 0.0
    assert metrics.funding_capture(-2.0, 4.0) == -0.5
    assert metrics.funding_capture(2.0, -4.0) == -0.5


def test_max_abs_net_delta_frac_edges():
    assert metrics.max_abs_net_delta_frac([]) == 0.0
    assert metrics.max_abs_net_delta_frac([0.0, 0.0]) == 0.0
    assert metrics.max_abs_net_delta_frac([0.01, -0.025, 0.015]) == 0.025

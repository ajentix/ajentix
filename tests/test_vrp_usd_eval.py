from __future__ import annotations

import pytest

from ajentix_quant.research.vrp_usd_eval import (
    EFFECTIVE_SPREAD_P50_USD,
    EFFECTIVE_SPREAD_P75_USD,
    SIGNAL_NET_NEGATIVE,
    SIGNAL_NO_ENTRIES,
    SIGNAL_POSITIVE_MEETS_BAR,
    SIGNAL_POSITIVE_SUBSCALE,
    measurement_signal,
    periods_per_year,
    summarize_measurement,
)

EQUITY = 1000.0


def _fold(fid: str, test_start: str, test_end: str) -> dict[str, str]:
    return {
        "id": fid,
        "train_start": "2024-09-01T00:00:00Z",
        "train_end": "2025-03-01T00:00:00Z",
        "test_start": test_start,
        "test_end": test_end,
    }


def _row(fid: str, *, entries: int, gross: float, max_loss: float, cw: float = 0.25) -> dict:
    return {
        "fold_id": fid,
        "entries": entries,
        "gross_pnl_usd": gross,
        "total_max_loss_usd": max_loss,
        "mean_credit_to_width": cw,
    }


def test_periods_per_year_from_test_windows() -> None:
    folds = [
        _fold("F1", "2025-03-01T00:00:00Z", "2025-03-31T00:00:00Z"),  # 30 days
        _fold("F2", "2025-04-01T00:00:00Z", "2025-05-01T00:00:00Z"),  # 30 days
    ]
    assert periods_per_year(folds) == pytest.approx(365.0 / 30.0)
    assert periods_per_year([]) == 0.0


def test_measurement_signal_branches() -> None:
    assert measurement_signal(
        total_net_p50_usd=50.0, fold_sharpe_net_p50=3.0, total_entries=0
    ) == SIGNAL_NO_ENTRIES
    assert measurement_signal(
        total_net_p50_usd=-5.0, fold_sharpe_net_p50=3.0, total_entries=10
    ) == SIGNAL_NET_NEGATIVE
    assert measurement_signal(
        total_net_p50_usd=5.0, fold_sharpe_net_p50=0.4, total_entries=10
    ) == SIGNAL_POSITIVE_SUBSCALE
    assert measurement_signal(
        total_net_p50_usd=5.0, fold_sharpe_net_p50=2.0, total_entries=10
    ) == SIGNAL_POSITIVE_MEETS_BAR


def test_summarize_applies_spread_haircut_and_return_on_risk() -> None:
    folds = [
        _fold("F1", "2025-03-01T00:00:00Z", "2025-05-01T00:00:00Z"),
        _fold("F2", "2025-05-01T00:00:00Z", "2025-07-01T00:00:00Z"),
    ]
    rows = [
        _row("F1", entries=10, gross=200.0, max_loss=2000.0),
        _row("F2", entries=10, gross=150.0, max_loss=2000.0),
    ]
    out = summarize_measurement(rows, folds=folds, equity_usd=EQUITY)

    f1 = out["per_fold"][0]
    assert f1["net_p50_pnl_usd"] == pytest.approx(200.0 - 10 * EFFECTIVE_SPREAD_P50_USD)  # 160
    assert f1["net_p75_pnl_usd"] == pytest.approx(200.0 - 10 * EFFECTIVE_SPREAD_P75_USD)  # 130
    assert f1["return_on_risk_gross"] == pytest.approx(200.0 / 2000.0)
    agg = out["aggregate"]
    assert agg["total_gross_pnl_usd"] == pytest.approx(350.0)
    assert agg["total_net_p50_pnl_usd"] == pytest.approx(350.0 - 20 * EFFECTIVE_SPREAD_P50_USD)
    assert agg["total_entries"] == 20


def test_summarize_flags_net_negative_when_spread_dominates() -> None:
    # Many entries, thin gross: the per-structure spread haircut turns it net-negative -> honest.
    folds = [
        _fold("F1", "2025-03-01T00:00:00Z", "2025-05-01T00:00:00Z"),
        _fold("F2", "2025-05-01T00:00:00Z", "2025-07-01T00:00:00Z"),
    ]
    rows = [
        _row("F1", entries=1000, gross=100.0, max_loss=50000.0),
        _row("F2", entries=1000, gross=120.0, max_loss=50000.0),
    ]
    out = summarize_measurement(rows, folds=folds, equity_usd=EQUITY)
    assert out["aggregate"]["total_net_p50_pnl_usd"] < 0.0
    assert out["measurement_signal"] == SIGNAL_NET_NEGATIVE


def test_summarize_no_entries_signal() -> None:
    folds = [_fold("F1", "2025-03-01T00:00:00Z", "2025-05-01T00:00:00Z")]
    rows = [_row("F1", entries=0, gross=0.0, max_loss=0.0)]
    out = summarize_measurement(rows, folds=folds, equity_usd=EQUITY)
    assert out["measurement_signal"] == SIGNAL_NO_ENTRIES

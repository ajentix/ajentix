from __future__ import annotations

import json
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace

import pytest
from scripts import run_breakeven_analysis

from ajentix_quant.adapters.base import (
    FundingRate,
    HistoricalCandle,
    MarketDataset,
    MarketType,
    PriceType,
    SourceQuality,
    StreamKey,
    StreamName,
)
from ajentix_quant.backtest.breakeven import A1_CLEARS, A1_NO_GO, analyze_symbol
from ajentix_quant.backtest.costs import round_trip_cost_usd
from ajentix_quant.backtest.engine import TwoLegFundingBacktest
from ajentix_quant.research.preregistration import PLAN_A1_BAR, PLAN_INSAMPLE_UNTIL_MS
from ajentix_quant.risk.engine import RiskEngine
from ajentix_quant.risk.margin import (
    VenueMarginModel,
    bybit_btc_eth_instruments,
    bybit_btc_eth_risk_limits,
)
from ajentix_quant.strategies.funding_harvest import FundingHarvest

SYM = "BTC/USDT:USDT"
VENUE = "bybit"
STEP_MS = 8 * 3600 * 1000
PLAN_HORIZON = 21
PLAN_EQUITIES = (500.0, 1000.0, 2000.0)
PLAN_PRIMARY_EQUITY = 1000.0
INSAMPLE_ROWS = 900


def _settings(**overrides: float) -> SimpleNamespace:
    values = {
        "perp_taker_fee_bps": 5.5,
        "perp_maker_fee_bps": 2.0,
        "spot_taker_fee_bps": 10.0,
        "slippage_base_bps": 1.0,
        "slippage_impact_bps_per_pct_volume": 5.0,
        "slippage_cap_bps": 50.0,
        "reserve_pct": 0.25,
        "max_position_pct": 0.25,
        "base_leverage": 2.0,
        "max_leverage": 5.0,
        "min_liq_distance_pct": 0.15,
        "health_factor_floor": 1.5,
        "gap_stress_pct": 0.20,
        "vol_spike_annual": 1.0,
        "funding_compression_8h": 0.00005,
        "funding_reversal_imminent_8h": 0.0,
        "max_net_delta_frac": 0.02,
        "adl_rank_threshold": 3,
        "max_drawdown_pct": 0.05,
        "funding_reversal_exit_hours": 24,
    }
    values.update(overrides)
    return SimpleNamespace(**values)


def _margin() -> VenueMarginModel:
    return VenueMarginModel(bybit_btc_eth_instruments()[SYM], bybit_btc_eth_risk_limits()[SYM])


def _dataset(
    rates: list[float],
    *,
    start_ms: int | None = None,
    volume_units: float = 1_000_000.0,
) -> MarketDataset:
    start = (
        start_ms if start_ms is not None
        else PLAN_INSAMPLE_UNTIL_MS - (len(rates) - 1) * STEP_MS
    )
    timestamps = [start + i * STEP_MS for i in range(len(rates))]
    funding = tuple(
        FundingRate(symbol=SYM, rate=rate, interval_hours=8.0, timestamp=ts)
        for rate, ts in zip(rates, timestamps, strict=True)
    )
    spot_key = StreamKey(SYM, MarketType.SPOT, PriceType.TRADE)
    perp_trade_key = StreamKey(SYM, MarketType.LINEAR_PERP, PriceType.TRADE)
    perp_mark_key = StreamKey(SYM, MarketType.LINEAR_PERP, PriceType.MARK)
    index_key = StreamKey(SYM, MarketType.LINEAR_PERP, PriceType.INDEX)
    return MarketDataset(
        venue=VENUE,
        timeframe="8h",
        scenario_id="sv2-g002-redteam-synthetic",
        symbols=(SYM,),
        funding={SYM: funding},
        ohlcv={
            spot_key: tuple(
                _candle(ts, MarketType.SPOT, PriceType.TRADE, volume_units) for ts in timestamps
            ),
            perp_trade_key: tuple(
                _candle(ts, MarketType.LINEAR_PERP, PriceType.TRADE, volume_units)
                for ts in timestamps
            ),
            perp_mark_key: tuple(
                _candle(ts, MarketType.LINEAR_PERP, PriceType.MARK, None) for ts in timestamps
            ),
            index_key: tuple(
                _candle(ts, MarketType.LINEAR_PERP, PriceType.INDEX, None) for ts in timestamps
            ),
        },
        source_quality={
            StreamName.FUNDING_HISTORY: SourceQuality.FIXTURE,
            StreamName.SPOT_TRADE_OHLCV: SourceQuality.FIXTURE,
            StreamName.PERP_TRADE_OHLCV: SourceQuality.FIXTURE,
            StreamName.PERP_MARK_OHLCV: SourceQuality.FIXTURE,
            StreamName.INDEX_OHLCV: SourceQuality.FIXTURE,
        },
    )


def _candle(
    timestamp_ms: int,
    market_type: MarketType,
    price_type: PriceType,
    volume: float | None,
) -> HistoricalCandle:
    return HistoricalCandle(
        timestamp_ms=timestamp_ms,
        symbol=SYM,
        venue=VENUE,
        market_type=market_type,
        price_type=price_type,
        timeframe="8h",
        open=100.0,
        high=100.0,
        low=100.0,
        close=100.0,
        volume=volume,
    )


def _analyze(
    rates: list[float],
    *,
    settings: SimpleNamespace | None = None,
    include_maker_sensitivity: bool = False,
    start_ms: int | None = None,
    insample_until_ms: int = PLAN_INSAMPLE_UNTIL_MS,
):
    return analyze_symbol(
        _dataset(rates, start_ms=start_ms),
        symbol=SYM,
        margin_model=_margin(),
        settings=settings or _settings(),
        decision_horizons=(PLAN_HORIZON,),
        equity_grid=PLAN_EQUITIES,
        primary_equity_usd=PLAN_PRIMARY_EQUITY,
        insample_until_ms=insample_until_ms,
        a1_bar=PLAN_A1_BAR,
        include_maker_sensitivity=include_maker_sensitivity,
    )


def _broad_multicluster_rates() -> list[float]:
    rates = [-0.002] * INSAMPLE_ROWS
    positive_len = 45
    gap_len = 80
    first_start = 40
    for block in range(6):
        start = first_start + block * (positive_len + gap_len)
        for idx in range(start, start + positive_len):
            rates[idx] = 0.001
    return rates


def test_cost_helper_matches_engine_round_trip_and_never_understates() -> None:
    settings = _settings(
        spot_taker_fee_bps=9.0,
        perp_taker_fee_bps=4.0,
        slippage_base_bps=2.0,
        slippage_impact_bps_per_pct_volume=7.0,
        slippage_cap_bps=40.0,
    )
    runner = TwoLegFundingBacktest(
        strategy=FundingHarvest(),
        risk=RiskEngine(),
        margin_model=_margin(),
        settings=settings,
    )
    cases = (
        {
            "spot_notional": Decimal("1234.56"),
            "perp_notional": Decimal("1200.00"),
            "spot_volume_notional": 2_000_000.0,
            "perp_volume_notional": 1_500_000.0,
            "stress_multiplier": 1.25,
        },
        {
            "spot_notional": Decimal("75.00"),
            "perp_notional": Decimal("80.50"),
            "spot_volume_notional": 25_000.0,
            "perp_volume_notional": 18_000.0,
            "stress_multiplier": 2.0,
        },
        {
            "spot_notional": Decimal("5000.00"),
            "perp_notional": Decimal("5000.00"),
            "spot_volume_notional": 1_000_000_000.0,
            "perp_volume_notional": 900_000_000.0,
            "stress_multiplier": 0.75,
        },
    )

    for case in cases:
        fee, slippage = runner._two_leg_costs(**case)
        engine_round_trip = 2.0 * float(fee + slippage)
        shared_round_trip = round_trip_cost_usd(settings=settings, **case)

        assert shared_round_trip == pytest.approx(engine_round_trip, rel=1e-12, abs=1e-12)
        assert shared_round_trip >= engine_round_trip - 1e-12


def test_appended_heldout_positive_rows_do_not_change_a1_or_insample_counts() -> None:
    base_rates = [0.0] * INSAMPLE_ROWS
    heldout_rates = [0.01] * 100
    start_ms = PLAN_INSAMPLE_UNTIL_MS - (len(base_rates) - 1) * STEP_MS

    base = _analyze(base_rates, start_ms=start_ms)
    appended = _analyze(base_rates + heldout_rates, start_ms=start_ms)
    base_metric = base.metric_for(horizon=PLAN_HORIZON, equity_usd=PLAN_PRIMARY_EQUITY)
    appended_metric = appended.metric_for(horizon=PLAN_HORIZON, equity_usd=PLAN_PRIMARY_EQUITY)

    assert base.funding_rows_insample == INSAMPLE_ROWS
    assert appended.funding_rows_insample == base.funding_rows_insample
    assert appended.a1_decision == base.a1_decision
    assert appended.reason_codes == base.reason_codes
    assert appended_metric.valid_windows == base_metric.valid_windows
    assert appended_metric.qualifying_windows == base_metric.qualifying_windows
    assert appended_metric.qualifying_pct == base_metric.qualifying_pct
    assert appended_metric.cluster_metrics == base_metric.cluster_metrics

    leaky_control = _analyze(
        base_rates + heldout_rates,
        start_ms=start_ms,
        insample_until_ms=PLAN_INSAMPLE_UNTIL_MS + len(heldout_rates) * STEP_MS,
    )
    leaky_metric = leaky_control.metric_for(horizon=PLAN_HORIZON, equity_usd=PLAN_PRIMARY_EQUITY)
    assert leaky_control.funding_rows_insample > base.funding_rows_insample
    assert leaky_metric.qualifying_windows > base_metric.qualifying_windows


def test_spike_concentrated_qualifying_windows_cannot_clear_a1() -> None:
    rates = [-0.002] * INSAMPLE_ROWS
    for idx in range(300, 450):
        rates[idx] = 0.0015

    result = _analyze(rates)
    metric = result.metric_for(horizon=PLAN_HORIZON, equity_usd=PLAN_PRIMARY_EQUITY)

    assert metric.valid_windows >= PLAN_A1_BAR["min_valid_windows_per_horizon"]
    assert metric.qualifying_pct > PLAN_A1_BAR["min_qualifying_pct"]
    assert metric.qualifying_windows >= PLAN_A1_BAR["min_qualifying_windows"]
    assert metric.clears_availability is True
    assert metric.cluster_metrics.cluster_count < PLAN_A1_BAR["min_clusters"]
    assert metric.clears_concentration is False
    assert metric.clears_horizon_bar is False
    assert result.a1_decision == A1_NO_GO


def test_broad_frequent_capital_robust_carry_clears_a1_bar() -> None:
    result = _analyze(_broad_multicluster_rates())
    primary = result.metric_for(horizon=PLAN_HORIZON, equity_usd=PLAN_PRIMARY_EQUITY)

    assert result.a1_decision == A1_CLEARS
    assert result.authorizing_horizons == (PLAN_HORIZON,)
    assert primary.clears_horizon_bar is True
    assert primary.cluster_metrics.cluster_count >= PLAN_A1_BAR["min_clusters"]
    max_share = primary.cluster_metrics.max_single_cluster_share
    assert max_share <= PLAN_A1_BAR["max_single_cluster_share"]
    assert primary.cluster_metrics.top3_cluster_share <= PLAN_A1_BAR["max_top3_cluster_share"]
    for equity in PLAN_A1_BAR["capital_robustness_equities"]:
        robust = result.metric_for(horizon=PLAN_HORIZON, equity_usd=equity)
        assert robust.clears_horizon_bar is True


def test_breakeven_analysis_is_deterministic_for_identical_dataset() -> None:
    dataset = _dataset(_broad_multicluster_rates())
    kwargs = dict(
        symbol=SYM,
        margin_model=_margin(),
        settings=_settings(),
        decision_horizons=(PLAN_HORIZON,),
        equity_grid=PLAN_EQUITIES,
        primary_equity_usd=PLAN_PRIMARY_EQUITY,
        a1_bar=PLAN_A1_BAR,
        include_maker_sensitivity=True,
    )

    first = analyze_symbol(dataset, **kwargs)
    second = analyze_symbol(dataset, **kwargs)

    assert first.a1_decision == second.a1_decision
    assert first.as_dict(include_windows=True) == second.as_dict(include_windows=True)


def test_run_breakeven_analysis_refuses_tampered_preregistration_without_report(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    original_path = next(Path("docs/preregistration").glob("stratv2-*.json"))
    tampered = json.loads(original_path.read_text(encoding="utf-8"))
    tampered["content_hash"] = "0" * 64
    tampered_path = tmp_path / "tampered-preregistration.json"
    tampered_path.write_text(
        json.dumps(tampered, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    reports_dir = tmp_path / "reports"

    exit_code = run_breakeven_analysis.main(
        [
            "--repo-root",
            str(Path.cwd()),
            "--preregistration",
            str(tampered_path),
            "--reports-dir",
            str(reports_dir),
        ]
    )

    captured = capsys.readouterr()
    assert exit_code == 1
    assert "run_status=invalid" in captured.out
    assert "decision=REFUSED_INVALID_PREREGISTRATION" in captured.out
    assert "branch_decision=" not in captured.out
    assert "wrote=" not in captured.out
    assert "content_hash drift" in captured.err
    assert not reports_dir.exists()


def test_maker_sensitivity_cannot_authorize_a1_when_taker_primary_is_no_go() -> None:
    result = _analyze(
        _broad_multicluster_rates(),
        settings=_settings(
            spot_taker_fee_bps=0.0,
            perp_taker_fee_bps=300.0,
            perp_maker_fee_bps=0.0,
            slippage_base_bps=0.0,
            slippage_impact_bps_per_pct_volume=0.0,
        ),
        include_maker_sensitivity=True,
    )
    taker_metric = result.metric_for(horizon=PLAN_HORIZON, equity_usd=PLAN_PRIMARY_EQUITY)

    assert result.a1_decision == A1_NO_GO
    assert taker_metric.clears_horizon_bar is False
    assert taker_metric.qualifying_windows == 0
    assert result.maker_sensitivity is not None
    maker_metric = result.maker_sensitivity.metric_for(
        horizon=PLAN_HORIZON,
        equity_usd=PLAN_PRIMARY_EQUITY,
    )
    assert maker_metric.clears_horizon_bar is True
    assert result.maker_sensitivity.would_clear_horizons == (PLAN_HORIZON,)
    assert result.maker_sensitivity.can_authorize is False

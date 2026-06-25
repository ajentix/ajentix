from __future__ import annotations

from decimal import Decimal
from types import SimpleNamespace

import pytest

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
from ajentix_quant.backtest.breakeven import (
    A1_INCONCLUSIVE,
    A1_NO_GO,
    BreakevenWindow,
    analyze_symbol,
    cluster_qualifying_windows,
)
from ajentix_quant.backtest.costs import round_trip_cost_usd
from ajentix_quant.backtest.engine import TwoLegFundingBacktest
from ajentix_quant.research.preregistration import PLAN_INSAMPLE_UNTIL_MS
from ajentix_quant.risk.engine import RiskEngine
from ajentix_quant.risk.margin import (
    VenueMarginModel,
    bybit_btc_eth_instruments,
    bybit_btc_eth_risk_limits,
)
from ajentix_quant.strategies.funding_harvest import FundingHarvest

SYM = "BTC/USDT:USDT"
VENUE = "bybit"
STEP = 8 * 3600 * 1000


def _settings(**overrides: float) -> SimpleNamespace:
    data = {
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
    data.update(overrides)
    return SimpleNamespace(**data)


def _bar(**overrides):
    data = {
        "min_insample_funding_rows": 1,
        "min_valid_windows_per_horizon": 1,
        "min_qualifying_pct": 0.10,
        "min_qualifying_windows": 1,
        "capital_robustness_equities": [500.0],
        "min_clusters": 1,
        "max_single_cluster_share": 1.0,
        "max_top3_cluster_share": 1.0,
        "safety_margin_bps": 1.0,
        "maker_can_authorize": False,
    }
    data.update(overrides)
    return data


def _margin() -> VenueMarginModel:
    return VenueMarginModel(bybit_btc_eth_instruments()[SYM], bybit_btc_eth_risk_limits()[SYM])


def _dataset(rates: list[float], *, start: int = 0, volume: float = 1_000_000.0) -> MarketDataset:
    timestamps = [start + i * STEP for i in range(len(rates))]
    funding = tuple(
        FundingRate(symbol=SYM, rate=rate, interval_hours=8.0, timestamp=ts)
        for rate, ts in zip(rates, timestamps, strict=True)
    )
    spot_key = StreamKey(SYM, MarketType.SPOT, PriceType.TRADE)
    perp_trade_key = StreamKey(SYM, MarketType.LINEAR_PERP, PriceType.TRADE)
    perp_mark_key = StreamKey(SYM, MarketType.LINEAR_PERP, PriceType.MARK)
    index_key = StreamKey(SYM, MarketType.LINEAR_PERP, PriceType.INDEX)
    ohlcv = {
        spot_key: tuple(
            _candle(ts, MarketType.SPOT, PriceType.TRADE, volume=volume) for ts in timestamps
        ),
        perp_trade_key: tuple(
            _candle(ts, MarketType.LINEAR_PERP, PriceType.TRADE, volume=volume)
            for ts in timestamps
        ),
        perp_mark_key: tuple(
            _candle(ts, MarketType.LINEAR_PERP, PriceType.MARK, volume=None)
            for ts in timestamps
        ),
        index_key: tuple(
            _candle(ts, MarketType.LINEAR_PERP, PriceType.INDEX, volume=None)
            for ts in timestamps
        ),
    }
    return MarketDataset(
        venue=VENUE,
        timeframe="1h",
        scenario_id="synthetic",
        symbols=(SYM,),
        funding={SYM: funding},
        ohlcv=ohlcv,
        source_quality={
            StreamName.FUNDING_HISTORY: SourceQuality.FIXTURE,
            StreamName.SPOT_TRADE_OHLCV: SourceQuality.FIXTURE,
            StreamName.PERP_TRADE_OHLCV: SourceQuality.FIXTURE,
            StreamName.PERP_MARK_OHLCV: SourceQuality.FIXTURE,
            StreamName.INDEX_OHLCV: SourceQuality.FIXTURE,
        },
    )


def _candle(
    timestamp: int,
    market_type: MarketType,
    price_type: PriceType,
    *,
    volume: float | None,
) -> HistoricalCandle:
    return HistoricalCandle(
        timestamp_ms=timestamp,
        symbol=SYM,
        venue=VENUE,
        market_type=market_type,
        price_type=price_type,
        timeframe="1h",
        open=100.0,
        high=100.0,
        low=100.0,
        close=100.0,
        volume=volume,
    )


def test_round_trip_cost_matches_engine_two_leg_costs_on_identical_inputs() -> None:
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
    fee, slip = runner._two_leg_costs(
        spot_notional=Decimal("1234.56"),
        perp_notional=Decimal("1200.00"),
        spot_volume_notional=2_000_000.0,
        perp_volume_notional=1_500_000.0,
        stress_multiplier=1.25,
    )

    shared = round_trip_cost_usd(
        spot_notional=Decimal("1234.56"),
        perp_notional=Decimal("1200.00"),
        spot_volume_notional=2_000_000.0,
        perp_volume_notional=1_500_000.0,
        settings=settings,
        stress_multiplier=1.25,
    )

    assert shared == pytest.approx(2.0 * float(fee + slip))


def test_qualifying_window_classification_math_for_clear_and_non_clear_cases() -> None:
    high = analyze_symbol(
        _dataset([0.02, 0.02, 0.02]),
        symbol=SYM,
        margin_model=_margin(),
        settings=_settings(slippage_base_bps=0.0, slippage_impact_bps_per_pct_volume=0.0),
        decision_horizons=(2,),
        equity_grid=(1000.0,),
        primary_equity_usd=1000.0,
        a1_bar=_bar(capital_robustness_equities=[]),
        include_maker_sensitivity=False,
    )
    low = analyze_symbol(
        _dataset([0.0, 0.0, 0.0]),
        symbol=SYM,
        margin_model=_margin(),
        settings=_settings(slippage_base_bps=0.0, slippage_impact_bps_per_pct_volume=0.0),
        decision_horizons=(2,),
        equity_grid=(1000.0,),
        primary_equity_usd=1000.0,
        a1_bar=_bar(capital_robustness_equities=[]),
        include_maker_sensitivity=False,
    )

    high_metric = high.metric_for(horizon=2, equity_usd=1000.0)
    low_metric = low.metric_for(horizon=2, equity_usd=1000.0)
    assert high_metric.valid_windows == 2
    assert high_metric.qualifying_windows == 2
    assert all(w.edge_usd > 0.0 for w in high_metric.windows)
    assert low_metric.valid_windows == 2
    assert low_metric.qualifying_windows == 0
    assert all(w.edge_usd <= 0.0 for w in low_metric.windows)


def test_in_sample_only_enforcement_excludes_rows_after_plan_boundary() -> None:
    start = PLAN_INSAMPLE_UNTIL_MS - STEP
    result = analyze_symbol(
        _dataset([0.0, 0.0, 0.50, 0.50], start=start),
        symbol=SYM,
        margin_model=_margin(),
        settings=_settings(slippage_base_bps=0.0, slippage_impact_bps_per_pct_volume=0.0),
        decision_horizons=(2,),
        equity_grid=(1000.0,),
        primary_equity_usd=1000.0,
        a1_bar=_bar(capital_robustness_equities=[]),
        include_maker_sensitivity=False,
    )

    metric = result.metric_for(horizon=2, equity_usd=1000.0)
    assert result.funding_rows_insample == 2
    assert metric.total_windows == 1
    assert metric.qualifying_windows == 0


def test_cluster_concentration_de_overlaps_by_horizon_gap() -> None:
    windows = (
        _window(start=0, edge=2.0),
        _window(start=1, edge=1.0),
        _window(start=4, edge=3.0),
        _window(start=8, edge=4.0),
    )

    clusters = cluster_qualifying_windows(windows, horizon=3)

    assert clusters.cluster_count == 3
    assert clusters.cluster_edge_usd == pytest.approx((3.0, 3.0, 4.0))
    assert clusters.max_single_cluster_share == pytest.approx(0.4)
    assert clusters.top3_cluster_share == pytest.approx(1.0)


def _window(*, start: int, edge: float) -> BreakevenWindow:
    return BreakevenWindow(
        start_index=start,
        end_index=start + 1,
        start_timestamp_ms=start,
        end_timestamp_ms=start + 1,
        leverage=1.0,
        notional_usd=100.0,
        funding_sum=0.0,
        funding_income_usd=0.0,
        round_trip_cost_usd=0.0,
        safety_margin_usd=0.0,
        edge_usd=edge,
        qualifying=True,
    )


def test_min_sample_shortfall_is_inconclusive() -> None:
    result = analyze_symbol(
        _dataset([0.50, 0.50, 0.50]),
        symbol=SYM,
        margin_model=_margin(),
        settings=_settings(slippage_base_bps=0.0, slippage_impact_bps_per_pct_volume=0.0),
        decision_horizons=(2,),
        equity_grid=(1000.0,),
        primary_equity_usd=1000.0,
        a1_bar=_bar(min_insample_funding_rows=900, capital_robustness_equities=[]),
        include_maker_sensitivity=False,
    )

    assert result.a1_decision == A1_INCONCLUSIVE
    assert "MIN_INSAMPLE_FUNDING_ROWS_NOT_MET" in result.reason_codes


def test_maker_sensitivity_is_labeled_non_authorizing_and_cannot_flip_a1() -> None:
    result = analyze_symbol(
        _dataset([0.03, 0.03, 0.03]),
        symbol=SYM,
        margin_model=_margin(),
        settings=_settings(
            spot_taker_fee_bps=0.0,
            perp_taker_fee_bps=1000.0,
            perp_maker_fee_bps=0.0,
            slippage_base_bps=0.0,
            slippage_impact_bps_per_pct_volume=0.0,
        ),
        decision_horizons=(2,),
        equity_grid=(1000.0, 500.0),
        primary_equity_usd=1000.0,
        a1_bar=_bar(capital_robustness_equities=[500.0]),
        include_maker_sensitivity=True,
    )

    taker_metric = result.metric_for(horizon=2, equity_usd=1000.0)
    maker_metric = result.maker_sensitivity.metric_for(horizon=2, equity_usd=1000.0)
    assert taker_metric.qualifying_windows == 0
    assert maker_metric.qualifying_windows > 0
    assert result.maker_sensitivity.can_authorize is False
    assert result.maker_sensitivity.would_clear_horizons == (2,)
    assert result.a1_decision == A1_NO_GO


def test_capital_robustness_is_any_of_robustness_equities():
    # The locked A1 bar requires clearing at $1000 AND >=1 of {$500,$2000} (any-of, per plan).
    from types import SimpleNamespace

    from ajentix_quant.backtest import breakeven as bk
    from ajentix_quant.research.preregistration import PLAN_A1_BAR

    def _m(equity, clears):
        return SimpleNamespace(
            horizon=21, equity_usd=float(equity), valid_windows=900, clears_horizon_bar=clears
        )

    # primary clears + ONE robustness equity ($500) clears, $2000 fails -> A1 CLEARS (any-of)
    clears = bk._decide_a1(
        funding_rows_insample=1000,
        metrics=(_m(1000, True), _m(500, True), _m(2000, False)),
        horizons=(21,),
        primary_equity_usd=1000.0,
        a1_bar=PLAN_A1_BAR,
    )
    assert clears[0] == bk.A1_CLEARS

    # primary clears but NEITHER robustness equity clears -> NO_GO (robustness not met)
    no_go = bk._decide_a1(
        funding_rows_insample=1000,
        metrics=(_m(1000, True), _m(500, False), _m(2000, False)),
        horizons=(21,),
        primary_equity_usd=1000.0,
        a1_bar=PLAN_A1_BAR,
    )
    assert no_go[0] == bk.A1_NO_GO
    assert any("CAPITAL_ROBUSTNESS_NOT_MET" in r for r in no_go[3])

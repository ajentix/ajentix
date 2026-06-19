from __future__ import annotations

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
from ajentix_quant.backtest.engine import BacktestResult, FundingBacktest, TwoLegFundingBacktest
from ajentix_quant.backtest.events import EventKind
from ajentix_quant.backtest.slippage import SlippageModel
from ajentix_quant.data.sample import sample_funding_8h
from ajentix_quant.risk.engine import RiskEngine, RiskParams
from ajentix_quant.risk.margin import (
    VenueMarginModel,
    bybit_btc_eth_instruments,
    bybit_btc_eth_risk_limits,
)
from ajentix_quant.strategies.funding_harvest import FundingHarvest
from ajentix_quant.strategies.sizing import SmallCapitalSizingPolicy

SYM = "BTC/USDT:USDT"
VENUE = "bybit"
STEP = 8 * 3600 * 1000


def _settings(**overrides: float) -> SimpleNamespace:
    data = {
        "perp_taker_fee_bps": 0.0,
        "spot_taker_fee_bps": 0.0,
        "leverage_cost_apr": 0.0,
        "reserve_pct": 0.25,
        "max_position_pct": 0.25,
        "max_net_delta_frac": 0.02,
        "slippage_base_bps": 0.0,
        "slippage_impact_bps_per_pct_volume": 0.0,
        "slippage_cap_bps": 0.0,
        "default_capital_usd": 1000.0,
    }
    data.update(overrides)
    return SimpleNamespace(**data)


def _margin_model() -> VenueMarginModel:
    return VenueMarginModel(
        bybit_btc_eth_instruments()[SYM],
        bybit_btc_eth_risk_limits()[SYM],
    )


def _dataset(
    *,
    funding_rates: list[float],
    spot_closes: list[float] | None = None,
    perp_closes: list[float] | None = None,
    mark_highs: list[float] | None = None,
    volume: float = 1_000_000.0,
) -> MarketDataset:
    n = len(funding_rates)
    spot_closes = spot_closes or [100.0] * n
    perp_closes = perp_closes or [100.0] * n
    mark_highs = mark_highs or perp_closes
    funding = []
    spot = []
    perp_trade = []
    perp_mark = []
    for i, rate in enumerate(funding_rates):
        ts = i * STEP
        funding.append(FundingRate(symbol=SYM, rate=rate, interval_hours=8.0, timestamp=ts))
        spot.append(
            _candle(
                ts,
                MarketType.SPOT,
                PriceType.TRADE,
                close=spot_closes[i],
                high=max(spot_closes[i], spot_closes[i]),
                volume=volume,
            )
        )
        perp_trade.append(
            _candle(
                ts,
                MarketType.LINEAR_PERP,
                PriceType.TRADE,
                close=perp_closes[i],
                high=max(perp_closes[i], mark_highs[i]),
                volume=volume,
            )
        )
        perp_mark.append(
            _candle(
                ts,
                MarketType.LINEAR_PERP,
                PriceType.MARK,
                close=perp_closes[i],
                high=mark_highs[i],
                volume=None,
            )
        )
    return MarketDataset(
        venue=VENUE,
        timeframe="8h",
        scenario_id="unit",
        symbols=(SYM,),
        funding={SYM: tuple(funding)},
        ohlcv={
            StreamKey(SYM, MarketType.SPOT, PriceType.TRADE): tuple(spot),
            StreamKey(SYM, MarketType.LINEAR_PERP, PriceType.TRADE): tuple(perp_trade),
            StreamKey(SYM, MarketType.LINEAR_PERP, PriceType.MARK): tuple(perp_mark),
        },
        source_quality={
            StreamName.FUNDING_HISTORY: SourceQuality.FIXTURE,
            StreamName.SPOT_TRADE_OHLCV: SourceQuality.FIXTURE,
            StreamName.PERP_TRADE_OHLCV: SourceQuality.FIXTURE,
            StreamName.PERP_MARK_OHLCV: SourceQuality.FIXTURE,
        },
    )


def _candle(
    timestamp_ms: int,
    market_type: MarketType,
    price_type: PriceType,
    *,
    close: float,
    high: float,
    volume: float | None,
) -> HistoricalCandle:
    return HistoricalCandle(
        timestamp_ms=timestamp_ms,
        symbol=SYM,
        venue=VENUE,
        market_type=market_type,
        price_type=price_type,
        timeframe="8h",
        open=close,
        high=high,
        low=min(close, high),
        close=close,
        volume=volume,
    )


def _runner(
    *,
    settings: SimpleNamespace | None = None,
    risk: RiskEngine | None = None,
    sizing: SmallCapitalSizingPolicy | None = None,
    slippage: SlippageModel | None = None,
    strategy: FundingHarvest | None = None,
) -> TwoLegFundingBacktest:
    settings = settings or _settings()
    return TwoLegFundingBacktest(
        strategy=strategy
        or FundingHarvest(min_funding_rate_8h=0.0001, basis_dislocation_bps=10_000.0),
        risk=risk or RiskEngine(),
        margin_model=_margin_model(),
        slippage=slippage or SlippageModel(
            settings.slippage_base_bps,
            settings.slippage_impact_bps_per_pct_volume,
            settings.slippage_cap_bps,
        ),
        sizing=sizing,
        settings=settings,
    )


def test_positive_funding_increases_and_negative_funding_decreases_short_carry_equity() -> None:
    positive = _runner().run_market_dataset(_dataset(funding_rates=[0.005, 0.001]))
    negative = _runner().run_market_dataset(_dataset(funding_rates=[0.005, -0.001]))

    assert positive.final_equity > 1000.0
    assert negative.final_equity < 1000.0
    assert positive.funding_received > 0.0
    assert negative.funding_paid > 0.0


def test_matched_legs_cancel_pure_price_move_without_funding_or_costs() -> None:
    result = _runner().run_market_dataset(
        _dataset(funding_rates=[0.005, 0.0], spot_closes=[100.0, 110.0], perp_closes=[100.0, 110.0])
    )

    assert result.final_equity == pytest.approx(1000.0, abs=1e-8)
    assert result.liquidated is False


def test_basis_widening_produces_bounded_nonzero_equity_move() -> None:
    result = _runner().run_market_dataset(
        _dataset(funding_rates=[0.005, 0.0], spot_closes=[100.0, 100.0], perp_closes=[100.0, 110.0])
    )

    assert 900.0 < result.final_equity < 1000.0
    assert result.liquidated is False


def test_size_based_slippage_and_fees_reduce_equity_on_entry_and_exit() -> None:
    cost_settings = _settings(
        spot_taker_fee_bps=10.0,
        perp_taker_fee_bps=5.5,
        slippage_base_bps=1.0,
        slippage_impact_bps_per_pct_volume=5.0,
        slippage_cap_bps=50.0,
    )
    no_cost = _runner().run_market_dataset(_dataset(funding_rates=[0.005, 0.0]))
    with_cost = _runner(settings=cost_settings).run_market_dataset(
        _dataset(funding_rates=[0.005, 0.0])
    )

    assert with_cost.final_equity < no_cost.final_equity
    assert with_cost.total_fees > 0.0
    assert with_cost.total_slippage > 0.0


def test_vol_spike_deleverages_before_liquidation() -> None:
    risk = RiskEngine(RiskParams(vol_spike_annual=1.0, funding_compression_8h=0.0))
    result = _runner(risk=risk).run_market_dataset(
        _dataset(funding_rates=[0.005, 0.005]),
        realized_vol_annual=[0.2, 2.0],
    )

    assert result.n_deleverages == 1
    assert EventKind.DELEVERAGE in {event.kind for event in result.events}
    assert result.liquidated is False
    assert EventKind.LIQUIDATION not in {event.kind for event in result.events}


def test_large_adverse_gap_liquidates_over_cap_but_not_gap_survival_capped_leverage() -> None:
    aggressive_sizing = SmallCapitalSizingPolicy(max_position_pct=1.0, reserve_pct=0.0)
    strategy = FundingHarvest(min_funding_rate_8h=0.0001, basis_dislocation_bps=10_000.0)
    over_cap = _runner(
        risk=RiskEngine(RiskParams(gap_stress_pct=0.0, reserve_pct=0.0)),
        sizing=aggressive_sizing,
        strategy=strategy,
    ).run_market_dataset(
        _dataset(funding_rates=[0.005, 0.005], mark_highs=[100.0, 140.0]),
        realized_vol_annual=[0.2, 0.2],
    )
    capped = _runner(
        risk=RiskEngine(RiskParams(gap_stress_pct=0.40, reserve_pct=0.0)),
        sizing=aggressive_sizing,
        strategy=strategy,
    ).run_market_dataset(
        _dataset(funding_rates=[0.005, 0.005], mark_highs=[100.0, 140.0]),
        realized_vol_annual=[0.2, 0.2],
    )

    assert over_cap.liquidated is True
    assert over_cap.n_liquidations == 1
    assert over_cap.final_equity < 1000.0
    assert capped.liquidated is False
    assert capped.n_liquidations == 0


def test_two_runs_are_identical_for_final_equity_event_counts_and_curve_length() -> None:
    dataset = _dataset(
        funding_rates=[0.005, 0.001, 0.0],
        spot_closes=[100.0, 101.0, 102.0],
        perp_closes=[100.0, 101.5, 102.0],
    )

    a = _runner().run_market_dataset(dataset)
    b = _runner().run_market_dataset(dataset)

    assert a.final_equity == b.final_equity
    assert len(a.events) == len(b.events)
    assert len(a.equity_curve) == len(b.equity_curve)


def test_equity_curve_is_net_account_basis_and_rebalance_keeps_end_delta_in_band() -> None:
    settings = _settings(max_net_delta_frac=0.02)
    result = _runner(settings=settings).run_market_dataset(
        _dataset(
            funding_rates=[0.005, 0.005],
            spot_closes=[100.0, 100.0],
            perp_closes=[100.0, 103.0],
        )
    )

    assert result.n_rebalances >= 1
    assert result.max_abs_net_delta <= settings.max_net_delta_frac
    assert len(result.equity_curve) == result.n_periods + 1


def test_phase0_funding_backtest_still_returns_finite_deterministic_metrics() -> None:
    def run() -> BacktestResult:
        return FundingBacktest(strategy=FundingHarvest(0.0001), risk=RiskEngine()).run(
            sample_funding_8h()
        )

    a = run()
    b = run()

    assert a.n_periods == 120
    assert a.final_equity == b.final_equity
    assert a.sharpe == b.sharpe
    for value in (a.sharpe, a.sortino, a.ann_return, a.max_drawdown):
        assert value == value

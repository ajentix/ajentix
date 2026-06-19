"""Deterministic funding-harvest backtests.

Phase 0 keeps the scalar ``FundingBacktest.run(funding_8h)`` API unchanged.

G006 adds a deterministic two-leg economic model over an in-memory ``MarketDataset``:
long spot + short linear perp with matched USD notionals, Decimal ledger accounting,
trade-volume-only slippage, taker fees on both legs, funding paid both ways, borrow drag,
venue-margin health checks, high-first adverse path liquidation checks, risk deleverage,
and rebalance-to-neutral.

Design assumptions made conservative where the story leaves room:
- Reserve is segregated and not spent. Deployable margin for health checks is current
  account equity minus reserve, floored at zero, so fees/losses reduce the margin wallet.
- Spot notional is paid from cash on entry; if target notional exceeds deployable cash,
  cash can go negative and the configured leverage-cost APR is charged on carry notional.
- Perp PnL uses short linear semantics: ``qty * (entry_mark - current_mark)``. Basis enters
  PnL because spot is marked on spot closes while the short is marked on perp mark closes;
  equal spot/perp moves cancel, widening basis does not.
- Positive funding rates are income to the short; negative rates are paid by the short.
- Liquidation is checked before any favorable intraperiod path using the period perp-mark
  high and an optional scenario gap. If breached, the account is forcibly closed at the
  adverse perp mark, pays exit costs, records ``liquidated=True``, and does not re-enter.
- Rebalancing reduces the larger USD delta leg down to the smaller leg when the net-delta
  fraction breaches the configured band. This avoids increasing exposure after drift and
  pays taker fee + size-based slippage on the mismatch notional.
- The period loop is no-lookahead: each funding timestamp uses only OHLCV rows with
  timestamps ``<= t`` and mark highs in ``(t-1, t]``.
"""

from __future__ import annotations

import math
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from decimal import Decimal

from ..adapters.base import HistoricalCandle, MarketDataset, MarketType, PriceType, StreamKey
from ..config import settings as default_settings
from ..risk.engine import RiskEngine
from ..risk.margin import VenueMarginModel
from ..strategies.funding_harvest import FundingHarvest
from ..strategies.sizing import SmallCapitalSizingPolicy
from ..strategies.state import MarketState, SignalAction
from . import metrics
from .account import RATIO_QUANT, TwoLegAccount, canonical_str, quantize_decimal, to_decimal
from .events import EventKind, LedgerEvent
from .slippage import SlippageModel

# 8h funding => 3 settlements/day.
_PERIODS_PER_YEAR = 365 * 3
_BPS_DENOMINATOR = Decimal("10000")
_ZERO = Decimal("0")

RealizedVolInput = (
    float
    | Sequence[float]
    | Mapping[int, float]
    | Callable[[int, int], float]
)


@dataclass
class BacktestResult:
    sharpe: float
    sortino: float
    ann_return: float
    max_drawdown: float
    n_periods: int
    n_entries: int
    final_equity: float
    total_fees: float = 0.0
    total_slippage: float = 0.0
    funding_received: float = 0.0
    funding_paid: float = 0.0
    leverage_cost: float = 0.0
    n_exits: int = 0
    n_forced_exits: int = 0
    n_rebalances: int = 0
    n_liquidations: int = 0
    n_deleverages: int = 0
    max_abs_net_delta: float = 0.0
    min_health_factor: float = math.inf
    max_leverage_used: float = 0.0
    liquidated: bool = False
    equity_curve: tuple[float, ...] = ()
    events: tuple[LedgerEvent, ...] = ()


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


@dataclass(frozen=True)
class PricePathPolicy:
    """Conservative short-perp path: adverse high/gap is observed before favorable lows."""

    scenario_gap_pct: float | None = None

    def adverse_short_mark(
        self, *, prior_mark: float, close_mark: float, high_mark: float
    ) -> float:
        if not math.isfinite(close_mark) or close_mark <= 0.0:
            raise ValueError("close_mark must be finite and positive")
        if not math.isfinite(high_mark) or high_mark <= 0.0:
            raise ValueError("high_mark must be finite and positive")
        if not math.isfinite(prior_mark) or prior_mark <= 0.0:
            raise ValueError("prior_mark must be finite and positive")
        # Most-adverse KNOWN level this period (never the favorable low).
        adverse = max(prior_mark, high_mark, close_mark)
        if self.scenario_gap_pct is not None:
            if not math.isfinite(self.scenario_gap_pct) or self.scenario_gap_pct < 0.0:
                raise ValueError("scenario_gap_pct must be finite and non-negative")
            # Shock UP from the pre-favorable base (prior/high), not the favorable close.
            gap_base = max(prior_mark, high_mark)
            adverse = max(adverse, gap_base * (1.0 + self.scenario_gap_pct))
        return adverse


@dataclass(frozen=True)
class _Period:
    index: int
    timestamp_ms: int
    funding_rate: float
    interval_hours: float
    spot_close: float
    perp_trade_close: float
    perp_mark_close: float
    perp_mark_high: float
    spot_volume_notional: float
    perp_volume_notional: float
    index_close: float | None


@dataclass
class TwoLegFundingBacktest:
    strategy: FundingHarvest
    risk: RiskEngine
    margin_model: VenueMarginModel
    slippage: SlippageModel | None = None
    sizing: SmallCapitalSizingPolicy | None = None
    initial_equity_usd: float | Decimal | str | None = None
    periods_per_year: float = _PERIODS_PER_YEAR
    settings: object = field(default_factory=lambda: default_settings)

    def __post_init__(self) -> None:
        if self.slippage is None:
            self.slippage = SlippageModel(
                base_bps=float(_setting(self.settings, "slippage_base_bps", 1.0)),
                impact_bps_per_pct_volume=float(
                    _setting(self.settings, "slippage_impact_bps_per_pct_volume", 5.0)
                ),
                cap_bps=float(_setting(self.settings, "slippage_cap_bps", 50.0)),
            )
        if self.sizing is None:
            self.sizing = SmallCapitalSizingPolicy(
                max_position_pct=float(_setting(self.settings, "max_position_pct", 0.25)),
                reserve_pct=float(_setting(self.settings, "reserve_pct", 0.25)),
                min_notional_usd=float(self.margin_model.instrument.min_notional),
            )

    def run_market_dataset(
        self,
        dataset: MarketDataset,
        *,
        symbol: str | None = None,
        initial_equity_usd: float | Decimal | str | None = None,
        realized_vol_annual: RealizedVolInput = 0.5,
        hold_intervals: int = 3,
        safety_margin_bps: float = 1.0,
        scenario_gap_pct: float | None = None,
        slippage_stress_multiplier: float = 1.0,
    ) -> BacktestResult:
        """Run the strict-order two-leg funding backtest over a cached market dataset."""

        assert self.slippage is not None
        assert self.sizing is not None
        symbol = symbol or _single_symbol(dataset)
        if symbol != self.margin_model.instrument.symbol:
            raise ValueError(
                f"margin model symbol {self.margin_model.instrument.symbol!r} "
                f"!= dataset symbol {symbol!r}"
            )
        periods = _dataset_periods(dataset, symbol)
        initial = (
            initial_equity_usd
            if initial_equity_usd is not None
            else self.initial_equity_usd
            if self.initial_equity_usd is not None
            else _setting(self.settings, "default_capital_usd", 1000.0)
        )
        account = TwoLegAccount(
            symbol=symbol,
            initial_equity=initial,
            reserve_pct=self.sizing.reserve_pct,
        )
        path_policy = PricePathPolicy(scenario_gap_pct=scenario_gap_pct)
        events: list[LedgerEvent] = []
        curve_dec: list[Decimal] = [quantize_decimal(account.equity())]
        peak_equity = account.equity()
        hours_negative = 0.0
        liquidated = False
        trading_disabled = False
        n_entries = 0
        n_exits = 0
        n_forced_exits = 0
        n_rebalances = 0
        n_liquidations = 0
        n_deleverages = 0
        max_abs_net_delta = Decimal("0")
        min_health_factor = math.inf
        max_leverage_used = 0.0

        for period in periods:
            prior_perp_mark = (
                float(account.perp_mark)
                if account.in_position and account.perp_mark > 0
                else period.perp_mark_close
            )
            account.mark(spot_price=period.spot_close, perp_mark=period.perp_mark_close)
            vol = _realized_vol_at(realized_vol_annual, period.index, period.timestamp_ms)

            if account.in_position:
                if period.funding_rate < 0.0:
                    hours_negative += period.interval_hours
                else:
                    hours_negative = 0.0

                equity_before = account.equity()
                funding_amount = account.accrue_funding(funding_rate=period.funding_rate)
                if funding_amount != _ZERO:
                    events.append(
                        _event(
                            period,
                            EventKind.FUNDING,
                            symbol,
                            reason="short receives positive funding; pays negative funding",
                            amount=funding_amount,
                            notional=account.short_notional(),
                            equity_before=equity_before,
                            equity_after=account.equity(),
                            account=account,
                        )
                    )

                leverage_cost = _leverage_cost(
                    account.current_notional,
                    apr=float(_setting(self.settings, "leverage_cost_apr", 0.0)),
                    interval_hours=period.interval_hours,
                )
                if leverage_cost > _ZERO:
                    equity_before = account.equity()
                    account.apply_cost(leverage_cost, bucket="leverage")
                    events.append(
                        _event(
                            period,
                            EventKind.LEVERAGE_COST,
                            symbol,
                            reason="borrow/leverage carry cost",
                            amount=-leverage_cost,
                            notional=account.current_notional,
                            equity_before=equity_before,
                            equity_after=account.equity(),
                            account=account,
                        )
                    )

            skip_decision = False
            if account.in_position:
                peak_equity = max(peak_equity, account.equity())
                drawdown_pct = _drawdown_pct(account.equity(), peak_equity)
                wallet = account.margin_wallet()
                current_health = self.margin_model.health_factor(
                    entry=float(account.perp_entry),
                    mark=period.perp_mark_close,
                    qty=float(account.perp_qty),
                    wallet_equity=float(wallet),
                )
                adverse_mark = path_policy.adverse_short_mark(
                    prior_mark=prior_perp_mark,
                    close_mark=period.perp_mark_close,
                    high_mark=period.perp_mark_high,
                )
                adverse_health = self.margin_model.health_factor(
                    entry=float(account.perp_entry),
                    mark=adverse_mark,
                    qty=float(account.perp_qty),
                    wallet_equity=float(wallet),
                )
                liq_mark = self.margin_model.liquidation_mark(
                    entry=float(account.perp_entry),
                    qty=float(account.perp_qty),
                    wallet_equity=float(wallet),
                )
                min_health_factor = min(min_health_factor, current_health, adverse_health)

                if adverse_mark >= liq_mark or adverse_health < 1.0:
                    equity_before = account.equity()
                    pre_notional = account.current_notional
                    pre_leverage = _account_leverage(account, self.sizing)
                    fee_cost, slip_cost = self._two_leg_costs(
                        spot_notional=account.spot_value(),
                        perp_notional=account.perp_qty * to_decimal(adverse_mark),
                        spot_volume_notional=period.spot_volume_notional,
                        perp_volume_notional=period.perp_volume_notional,
                        stress_multiplier=slippage_stress_multiplier,
                    )
                    account.close_carry(
                        spot_price=period.spot_close,
                        perp_mark=adverse_mark,
                        fee_cost=fee_cost,
                        slippage_cost=slip_cost,
                    )
                    liquidated = True
                    trading_disabled = True
                    skip_decision = True
                    n_liquidations += 1
                    n_forced_exits += 1
                    n_exits += 1
                    events.append(
                        _event(
                            period,
                            EventKind.LIQUIDATION,
                            symbol,
                            reason=(
                                f"adverse mark {adverse_mark:.12g} breached liquidation "
                                f"mark {liq_mark:.12g} or HF {adverse_health:.6g} < 1"
                            ),
                            amount=account.equity() - equity_before,
                            notional=pre_notional,
                            leverage=pre_leverage,
                            equity_before=equity_before,
                            equity_after=account.equity(),
                            account=account,
                            perp_mark=adverse_mark,
                        )
                    )
                else:
                    raw_reasons = self.risk.deleverage_reasons(
                        realized_vol_annual=vol,
                        funding_rate_8h=period.funding_rate,
                        health_factor=current_health,
                        hours_negative=hours_negative,
                        drawdown_pct=drawdown_pct,
                        adl_rank=self.risk.adl_provider.adl_rank(symbol),
                        net_delta_frac=float(account.net_delta_frac()),
                    )
                    # Net-delta drift is handled by the deterministic rebalance step that
                    # immediately follows the risk check. Other risk reasons still force out.
                    reasons = tuple(reason for reason in raw_reasons if reason != "net_delta")
                    if reasons:
                        equity_before = account.equity()
                        events.append(
                            _event(
                                period,
                                EventKind.DELEVERAGE,
                                symbol,
                                reason=",".join(reasons),
                                notional=account.current_notional,
                                leverage=_account_leverage(account, self.sizing),
                                equity_before=equity_before,
                                equity_after=equity_before,
                                account=account,
                            )
                        )
                        fee_cost, slip_cost = self._two_leg_costs(
                            spot_notional=account.spot_value(),
                            perp_notional=account.short_notional(),
                            spot_volume_notional=period.spot_volume_notional,
                            perp_volume_notional=period.perp_volume_notional,
                            stress_multiplier=slippage_stress_multiplier,
                        )
                        account.close_carry(
                            spot_price=period.spot_close,
                            perp_mark=period.perp_mark_close,
                            fee_cost=fee_cost,
                            slippage_cost=slip_cost,
                        )
                        n_deleverages += 1
                        n_forced_exits += 1
                        n_exits += 1
                        skip_decision = True
                        events.append(
                            _event(
                                period,
                                EventKind.EXIT,
                                symbol,
                                reason="risk deleverage forced exit",
                                amount=account.equity() - equity_before,
                                equity_before=equity_before,
                                equity_after=account.equity(),
                                account=account,
                            )
                        )

            if account.in_position and not skip_decision:
                net_delta_frac = abs(account.net_delta_frac())
                max_band = to_decimal(_setting(self.settings, "max_net_delta_frac", 0.02))
                if net_delta_frac > max_band:
                    leg, trade_notional = account.rebalance_trade()
                    if leg != "none" and trade_notional > _ZERO:
                        equity_before = account.equity()
                        fee_cost, slip_cost = self._single_leg_cost(
                            leg=leg,
                            notional=trade_notional,
                            spot_volume_notional=period.spot_volume_notional,
                            perp_volume_notional=period.perp_volume_notional,
                            stress_multiplier=slippage_stress_multiplier,
                        )
                        before_delta, after_delta = account.rebalance(
                            leg=leg,
                            trade_notional=trade_notional,
                            spot_price=period.spot_close,
                            perp_mark=period.perp_mark_close,
                            fee_cost=fee_cost,
                            slippage_cost=slip_cost,
                        )
                        n_rebalances += 1
                        events.append(
                            _event(
                                period,
                                EventKind.REBALANCE,
                                symbol,
                                reason=f"reduced {leg} leg to restore delta neutrality",
                                amount=account.equity() - equity_before,
                                notional=trade_notional,
                                net_delta=after_delta,
                                equity_before=equity_before,
                                equity_after=account.equity(),
                                account=account,
                            )
                        )
                        _ = before_delta

            if not skip_decision and not trading_disabled:
                state = self._market_state(period, account, vol)
                signal = self.strategy.decide(
                    state,
                    sizing=self.sizing,
                    hold_intervals=hold_intervals,
                    safety_margin_bps=safety_margin_bps,
                )
                if signal.action is SignalAction.ENTER and not account.in_position:
                    notional = to_decimal(signal.target_notional_usd)
                    if notional > _ZERO:
                        equity_before = account.equity()
                        fee_cost, slip_cost = self._two_leg_costs(
                            spot_notional=notional,
                            perp_notional=notional,
                            spot_volume_notional=period.spot_volume_notional,
                            perp_volume_notional=period.perp_volume_notional,
                            stress_multiplier=slippage_stress_multiplier,
                        )
                        account.open_carry(
                            spot_price=period.spot_close,
                            perp_mark=period.perp_mark_close,
                            notional=notional,
                            leverage=signal.target_leverage,
                            fee_cost=fee_cost,
                            slippage_cost=slip_cost,
                        )
                        n_entries += 1
                        max_leverage_used = max(max_leverage_used, float(signal.target_leverage))
                        events.append(
                            _event(
                                period,
                                EventKind.ENTRY,
                                symbol,
                                reason=signal.reason,
                                amount=account.equity() - equity_before,
                                notional=notional,
                                leverage=signal.target_leverage,
                                equity_before=equity_before,
                                equity_after=account.equity(),
                                account=account,
                            )
                        )
                elif signal.action is SignalAction.EXIT and account.in_position:
                    equity_before = account.equity()
                    fee_cost, slip_cost = self._two_leg_costs(
                        spot_notional=account.spot_value(),
                        perp_notional=account.short_notional(),
                        spot_volume_notional=period.spot_volume_notional,
                        perp_volume_notional=period.perp_volume_notional,
                        stress_multiplier=slippage_stress_multiplier,
                    )
                    account.close_carry(
                        spot_price=period.spot_close,
                        perp_mark=period.perp_mark_close,
                        fee_cost=fee_cost,
                        slippage_cost=slip_cost,
                    )
                    n_exits += 1
                    events.append(
                        _event(
                            period,
                            EventKind.EXIT,
                            symbol,
                            reason=signal.reason,
                            amount=account.equity() - equity_before,
                            equity_before=equity_before,
                            equity_after=account.equity(),
                            account=account,
                        )
                    )

            end_delta = abs(account.net_delta_frac())
            max_abs_net_delta = max(max_abs_net_delta, end_delta)
            if account.in_position:
                max_leverage_used = max(max_leverage_used, _account_leverage(account, self.sizing))
            curve_dec.append(quantize_decimal(account.equity()))
            peak_equity = max(peak_equity, account.equity())

        curve = tuple(float(v) for v in curve_dec)
        rets = _returns(curve)
        final_equity = float(quantize_decimal(account.equity()))
        if math.isinf(min_health_factor):
            min_health_factor_out = math.inf
        else:
            min_health_factor_out = float(min_health_factor)
        return BacktestResult(
            sharpe=metrics.sharpe(rets, self.periods_per_year),
            sortino=metrics.sortino(rets, self.periods_per_year),
            ann_return=metrics.annualized_return(rets, self.periods_per_year),
            max_drawdown=metrics.max_drawdown(list(curve)),
            n_periods=len(periods),
            n_entries=n_entries,
            final_equity=final_equity,
            total_fees=float(quantize_decimal(account.total_fees)),
            total_slippage=float(quantize_decimal(account.total_slippage)),
            funding_received=float(quantize_decimal(account.funding_received)),
            funding_paid=float(quantize_decimal(account.funding_paid)),
            leverage_cost=float(quantize_decimal(account.leverage_cost)),
            n_exits=n_exits,
            n_forced_exits=n_forced_exits,
            n_rebalances=n_rebalances,
            n_liquidations=n_liquidations,
            n_deleverages=n_deleverages,
            max_abs_net_delta=float(quantize_decimal(max_abs_net_delta, RATIO_QUANT)),
            min_health_factor=min_health_factor_out,
            max_leverage_used=float(max_leverage_used),
            liquidated=liquidated,
            equity_curve=curve,
            events=tuple(events),
        )

    def _market_state(self, period: _Period, account: TwoLegAccount, vol: float) -> MarketState:
        gap_cap = self.risk.gap_survival_leverage_cap(
            self.margin_model,
            equity=max(float(account.equity()), 1e-12),
        )
        dynamic_cap = self.risk.dynamic_leverage_capped(
            realized_vol_annual=vol,
            funding_rate_8h=period.funding_rate,
            margin_model=self.margin_model,
            equity=max(float(account.equity()), 1e-12),
        )
        effective_gap_cap = min(gap_cap, dynamic_cap) if dynamic_cap > 0.0 else 0.0
        health = math.inf
        if account.in_position:
            health = self.margin_model.health_factor(
                entry=float(account.perp_entry),
                mark=period.perp_mark_close,
                qty=float(account.perp_qty),
                wallet_equity=float(account.margin_wallet()),
            )
        basis_bps = (period.perp_mark_close - period.spot_close) / period.spot_close * 10_000.0
        return MarketState(
            symbol=account.symbol,
            funding_rate=period.funding_rate,
            interval_hours=period.interval_hours,
            spot_close=period.spot_close,
            perp_mark_close=period.perp_mark_close,
            index_close=period.index_close,
            basis_bps=basis_bps,
            realized_vol_annual=vol,
            expected_cost_bps=self._expected_cost_bps(period, account, effective_gap_cap),
            equity_usd=max(float(account.equity()), 0.0),
            net_delta_frac=float(account.net_delta_frac()),
            in_position=account.in_position,
            current_leverage=_account_leverage(account, self.sizing),
            gap_survival_leverage_cap=effective_gap_cap,
            health_factor=health,
            risk_deleverage=False,
        )

    def _expected_cost_bps(
        self,
        period: _Period,
        account: TwoLegAccount,
        leverage_cap: float,
    ) -> float:
        assert self.slippage is not None
        assert self.sizing is not None
        equity = max(float(account.equity()), 0.0)
        if equity == 0.0 or leverage_cap < 1.0:
            return math.inf
        lev = max(1.0, min(leverage_cap, 3.0))
        notional = self.sizing.size(equity_usd=equity, leverage=lev)
        if notional <= 0.0:
            return math.inf
        one_way_fee_bps = _setting(self.settings, "spot_taker_fee_bps", 10.0) + _setting(
            self.settings, "perp_taker_fee_bps", 5.5
        )
        one_way_slip_bps = self.slippage.slippage_bps(
            order_notional=notional,
            bar_volume_notional=period.spot_volume_notional,
        ) + self.slippage.slippage_bps(
            order_notional=notional,
            bar_volume_notional=period.perp_volume_notional,
        )
        return float(2.0 * (one_way_fee_bps + one_way_slip_bps))

    def _two_leg_costs(
        self,
        *,
        spot_notional: Decimal | float | int | str,
        perp_notional: Decimal | float | int | str,
        spot_volume_notional: float,
        perp_volume_notional: float,
        stress_multiplier: float,
    ) -> tuple[Decimal, Decimal]:
        spot = to_decimal(spot_notional)
        perp = to_decimal(perp_notional)
        spot_rate = to_decimal(_setting(self.settings, "spot_taker_fee_bps", 10.0))
        spot_fee = spot * spot_rate / _BPS_DENOMINATOR
        perp_rate = to_decimal(_setting(self.settings, "perp_taker_fee_bps", 5.5))
        perp_fee = perp * perp_rate / _BPS_DENOMINATOR
        assert self.slippage is not None
        spot_slip = to_decimal(
            self.slippage.slippage_cost(
                order_notional=float(spot),
                bar_volume_notional=spot_volume_notional,
                stress_multiplier=stress_multiplier,
            )
        )
        perp_slip = to_decimal(
            self.slippage.slippage_cost(
                order_notional=float(perp),
                bar_volume_notional=perp_volume_notional,
                stress_multiplier=stress_multiplier,
            )
        )
        return spot_fee + perp_fee, spot_slip + perp_slip

    def _single_leg_cost(
        self,
        *,
        leg: str,
        notional: Decimal,
        spot_volume_notional: float,
        perp_volume_notional: float,
        stress_multiplier: float,
    ) -> tuple[Decimal, Decimal]:
        assert self.slippage is not None
        if leg == "spot":
            fee_bps = _setting(self.settings, "spot_taker_fee_bps", 10.0)
            volume = spot_volume_notional
        elif leg == "perp":
            fee_bps = _setting(self.settings, "perp_taker_fee_bps", 5.5)
            volume = perp_volume_notional
        else:
            raise ValueError(f"unknown leg {leg!r}")
        fee = notional * to_decimal(fee_bps) / _BPS_DENOMINATOR
        slip = to_decimal(
            self.slippage.slippage_cost(
                order_notional=float(notional),
                bar_volume_notional=volume,
                stress_multiplier=stress_multiplier,
            )
        )
        return fee, slip


def _single_symbol(dataset: MarketDataset) -> str:
    if len(dataset.symbols) != 1:
        raise ValueError("symbol is required for multi-symbol datasets")
    return dataset.symbols[0]


def _dataset_periods(dataset: MarketDataset, symbol: str) -> tuple[_Period, ...]:
    funding = tuple(sorted(dataset.funding.get(symbol, ()), key=lambda fr: fr.timestamp))
    if not funding:
        raise ValueError(f"no funding series for {symbol}")
    spot = _stream(dataset, symbol, MarketType.SPOT, PriceType.TRADE)
    perp_trade = _stream(dataset, symbol, MarketType.LINEAR_PERP, PriceType.TRADE)
    perp_mark = _stream(dataset, symbol, MarketType.LINEAR_PERP, PriceType.MARK)
    index_stream = dataset.ohlcv.get(StreamKey(symbol, MarketType.LINEAR_PERP, PriceType.INDEX), ())
    index = tuple(sorted(index_stream, key=lambda c: c.timestamp_ms))

    out: list[_Period] = []
    previous_ts: int | None = None
    for i, fr in enumerate(funding):
        spot_row = _row_at_or_before(spot, fr.timestamp, "spot trade", symbol)
        perp_trade_row = _row_at_or_before(perp_trade, fr.timestamp, "perp trade", symbol)
        perp_mark_row = _row_at_or_before(perp_mark, fr.timestamp, "perp mark", symbol)
        index_row = _optional_row_at_or_before(index, fr.timestamp)
        high = _high_between(perp_mark, previous_ts, fr.timestamp, fallback=perp_mark_row.high)
        out.append(
            _Period(
                index=i,
                timestamp_ms=fr.timestamp,
                funding_rate=fr.rate,
                interval_hours=fr.interval_hours,
                spot_close=spot_row.close,
                perp_trade_close=perp_trade_row.close,
                perp_mark_close=perp_mark_row.close,
                perp_mark_high=high,
                spot_volume_notional=_volume_notional(spot_row, spot_row.close, "spot trade"),
                perp_volume_notional=_volume_notional(
                    perp_trade_row, perp_trade_row.close, "perp trade"
                ),
                index_close=None if index_row is None else index_row.close,
            )
        )
        previous_ts = fr.timestamp
    return tuple(out)


def _stream(
    dataset: MarketDataset,
    symbol: str,
    market_type: MarketType,
    price_type: PriceType,
) -> tuple[HistoricalCandle, ...]:
    key = StreamKey(symbol, market_type, price_type)
    rows = dataset.ohlcv.get(key)
    if not rows:
        raise ValueError(f"missing OHLCV stream {key}")
    return tuple(sorted(rows, key=lambda c: c.timestamp_ms))


def _row_at_or_before(
    rows: Sequence[HistoricalCandle],
    timestamp_ms: int,
    label: str,
    symbol: str,
) -> HistoricalCandle:
    found = _optional_row_at_or_before(rows, timestamp_ms)
    if found is None:
        raise ValueError(f"no {label} row for {symbol} at or before {timestamp_ms}")
    return found


def _optional_row_at_or_before(
    rows: Sequence[HistoricalCandle],
    timestamp_ms: int,
) -> HistoricalCandle | None:
    found: HistoricalCandle | None = None
    for row in rows:
        if row.timestamp_ms <= timestamp_ms:
            found = row
        else:
            break
    return found


def _high_between(
    rows: Sequence[HistoricalCandle],
    previous_ts: int | None,
    timestamp_ms: int,
    *,
    fallback: float,
) -> float:
    highs = [
        row.high
        for row in rows
        if (previous_ts is None or previous_ts < row.timestamp_ms)
        and row.timestamp_ms <= timestamp_ms
    ]
    return max(highs) if highs else fallback


def _volume_notional(row: HistoricalCandle, price: float, label: str) -> float:
    if row.price_type is not PriceType.TRADE:
        raise ValueError(f"{label} volume must come from a trade-price stream")
    if row.volume is None:
        raise ValueError(f"{label} trade volume is required for slippage")
    if not math.isfinite(row.volume) or row.volume <= 0.0:
        raise ValueError(f"{label} trade volume must be finite and positive")
    if not math.isfinite(price) or price <= 0.0:
        raise ValueError(f"{label} price must be finite and positive")
    return float(row.volume * price)


def _realized_vol_at(source: RealizedVolInput, index: int, timestamp_ms: int) -> float:
    if callable(source):
        value = source(index, timestamp_ms)
    elif isinstance(source, Mapping):
        value = source.get(timestamp_ms, source.get(index, 0.5))
    elif isinstance(source, Sequence) and not isinstance(source, (str, bytes)):
        value = source[index] if index < len(source) else source[-1]
    else:
        if not isinstance(source, (int, float)):
            raise TypeError(f"unsupported realized_vol source type: {type(source)!r}")
        value = float(source)
    value = float(value)
    if not math.isfinite(value) or value < 0.0:
        raise ValueError("realized_vol_annual must be finite and non-negative")
    return value


def _setting(settings_obj: object, name: str, default: float) -> float:
    return float(getattr(settings_obj, name, default))


def _leverage_cost(notional: Decimal, *, apr: float, interval_hours: float) -> Decimal:
    if apr <= 0.0 or notional <= _ZERO:
        return _ZERO
    return notional * to_decimal(apr) * to_decimal(interval_hours) / to_decimal(365 * 24)


def _drawdown_pct(equity: Decimal, peak: Decimal) -> float:
    if peak <= _ZERO:
        return 0.0
    drawdown = max(_ZERO, peak - equity) / peak
    return float(drawdown)


def _account_leverage(account: TwoLegAccount, sizing: SmallCapitalSizingPolicy | None) -> float:
    if not account.in_position or sizing is None:
        return 0.0
    equity = account.equity()
    max_position_pct = to_decimal(sizing.max_position_pct)
    denominator = equity * max_position_pct
    if denominator <= _ZERO:
        return math.inf
    return float(account.current_notional / denominator)


def _event(
    period: _Period,
    kind: EventKind,
    symbol: str,
    *,
    reason: str,
    account: TwoLegAccount,
    amount: Decimal | float | int | str | None = None,
    notional: Decimal | float | int | str | None = None,
    leverage: Decimal | float | int | str | None = None,
    net_delta: Decimal | float | int | str | None = None,
    equity_before: Decimal | float | int | str | None = None,
    equity_after: Decimal | float | int | str | None = None,
    spot_price: Decimal | float | int | str | None = None,
    perp_mark: Decimal | float | int | str | None = None,
) -> LedgerEvent:
    return LedgerEvent(
        timestamp_ms=period.timestamp_ms,
        kind=kind,
        symbol=symbol,
        reason=reason,
        amount=None if amount is None else canonical_str(amount),
        notional=None if notional is None else canonical_str(notional),
        leverage=None if leverage is None else canonical_str(leverage, RATIO_QUANT),
        net_delta=(
            canonical_str(account.net_delta_usd() if net_delta is None else net_delta)
        ),
        equity_before=None if equity_before is None else canonical_str(equity_before),
        equity_after=None if equity_after is None else canonical_str(equity_after),
        spot_price=canonical_str(period.spot_close if spot_price is None else spot_price),
        perp_mark=canonical_str(period.perp_mark_close if perp_mark is None else perp_mark),
    )


def _returns(curve: Sequence[float]) -> list[float]:
    out: list[float] = []
    for prev, cur in zip(curve, curve[1:], strict=False):
        if prev == 0.0:
            out.append(0.0)
        else:
            out.append(cur / prev - 1.0)
    return out

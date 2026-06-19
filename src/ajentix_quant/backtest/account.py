"""Decimal ledger for a deterministic long-spot / short-perp carry account.

Economic assumptions encoded here:
- Reserve is segregated cash and is not spent on spot. Non-reserve cash can go
  negative when the selected notional exceeds deployable cash, representing a
  conservative borrow/margin account that pays the engine's leverage-cost drag.
- Spot purchases/sales move cash; short-perp unrealized PnL is marked separately
  and is realized into cash when the short leg is closed or reduced.
- Rebalancing reduces the larger USD delta leg down to the smaller one. This avoids
  increasing exposure after drift and pays costs on the traded mismatch notional.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import ROUND_HALF_EVEN, Decimal, getcontext
from typing import Literal

getcontext().prec = 36

MONEY_QUANT = Decimal("0.00000001")
PRICE_QUANT = Decimal("0.00000001")
RATIO_QUANT = Decimal("0.000000000001")
_ZERO = Decimal("0")
_ONE = Decimal("1")


@dataclass
class TwoLegAccount:
    symbol: str
    initial_equity: Decimal | float | int | str
    reserve_pct: Decimal | float | int | str
    cash: Decimal = field(init=False)
    reserve: Decimal = field(init=False)
    spot_qty: Decimal = field(default=_ZERO, init=False)
    perp_qty: Decimal = field(default=_ZERO, init=False)
    perp_entry: Decimal = field(default=_ZERO, init=False)
    spot_price: Decimal = field(default=_ZERO, init=False)
    perp_mark: Decimal = field(default=_ZERO, init=False)
    current_notional: Decimal = field(default=_ZERO, init=False)
    current_leverage: Decimal = field(default=_ZERO, init=False)
    total_fees: Decimal = field(default=_ZERO, init=False)
    total_slippage: Decimal = field(default=_ZERO, init=False)
    funding_received: Decimal = field(default=_ZERO, init=False)
    funding_paid: Decimal = field(default=_ZERO, init=False)
    leverage_cost: Decimal = field(default=_ZERO, init=False)

    def __post_init__(self) -> None:
        if not self.symbol:
            raise ValueError("symbol must be non-empty")
        self.initial_equity = require_non_negative("initial_equity", self.initial_equity)
        self.reserve_pct = require_non_negative("reserve_pct", self.reserve_pct)
        if self.reserve_pct >= _ONE:
            raise ValueError("reserve_pct must be < 1")
        self.reserve = self.initial_equity * self.reserve_pct
        self.cash = self.initial_equity - self.reserve

    @property
    def in_position(self) -> bool:
        return self.spot_qty > _ZERO or self.perp_qty > _ZERO

    def mark(
        self,
        *,
        spot_price: Decimal | float | int | str,
        perp_mark: Decimal | float | int | str,
    ) -> tuple[Decimal, Decimal]:
        """Update known marks and return incremental spot/perp price PnL."""

        new_spot = require_positive("spot_price", spot_price)
        new_perp = require_positive("perp_mark", perp_mark)
        spot_pnl = _ZERO
        perp_pnl = _ZERO
        if self.in_position and self.spot_price > _ZERO and self.perp_mark > _ZERO:
            spot_pnl = self.spot_qty * (new_spot - self.spot_price)
            perp_pnl = self.perp_qty * (self.perp_mark - new_perp)
        self.spot_price = new_spot
        self.perp_mark = new_perp
        return spot_pnl, perp_pnl

    def open_carry(
        self,
        *,
        spot_price: Decimal | float | int | str,
        perp_mark: Decimal | float | int | str,
        notional: Decimal | float | int | str,
        leverage: Decimal | float | int | str,
        fee_cost: Decimal | float | int | str = _ZERO,
        slippage_cost: Decimal | float | int | str = _ZERO,
    ) -> None:
        if self.in_position:
            raise ValueError("carry is already open")
        spot = require_positive("spot_price", spot_price)
        perp = require_positive("perp_mark", perp_mark)
        n = require_positive("notional", notional)
        lev = require_non_negative("leverage", leverage)
        fees = require_non_negative("fee_cost", fee_cost)
        slip = require_non_negative("slippage_cost", slippage_cost)

        self.spot_price = spot
        self.perp_mark = perp
        self.spot_qty = n / spot
        self.perp_qty = n / perp
        self.perp_entry = perp
        self.current_notional = n
        self.current_leverage = lev
        self.cash -= n
        self.apply_cost(fees, bucket="fee")
        self.apply_cost(slip, bucket="slippage")

    def close_carry(
        self,
        *,
        spot_price: Decimal | float | int | str,
        perp_mark: Decimal | float | int | str,
        fee_cost: Decimal | float | int | str = _ZERO,
        slippage_cost: Decimal | float | int | str = _ZERO,
    ) -> Decimal:
        """Close both legs and return realized perp PnL before close costs."""

        if not self.in_position:
            return _ZERO
        spot = require_positive("spot_price", spot_price)
        perp = require_positive("perp_mark", perp_mark)
        fees = require_non_negative("fee_cost", fee_cost)
        slip = require_non_negative("slippage_cost", slippage_cost)

        self.spot_price = spot
        self.perp_mark = perp
        spot_proceeds = self.spot_qty * spot
        perp_pnl = self.perp_qty * (self.perp_entry - perp)
        self.cash += spot_proceeds + perp_pnl
        self.apply_cost(fees, bucket="fee")
        self.apply_cost(slip, bucket="slippage")
        self.spot_qty = _ZERO
        self.perp_qty = _ZERO
        self.perp_entry = _ZERO
        self.current_notional = _ZERO
        self.current_leverage = _ZERO
        return perp_pnl

    def accrue_funding(self, *, funding_rate: Decimal | float | int | str) -> Decimal:
        """Apply funding to the short-perp leg; positive rates pay the short."""

        rate = to_decimal(funding_rate)
        if not self.in_position:
            return _ZERO
        amount = rate * self.perp_qty * self.perp_mark
        self.cash += amount
        if amount >= _ZERO:
            self.funding_received += amount
        else:
            self.funding_paid += -amount
        return amount

    def apply_cost(
        self,
        amount: Decimal | float | int | str,
        *,
        bucket: Literal["fee", "slippage", "leverage", "other"] = "other",
    ) -> Decimal:
        cost = require_non_negative("amount", amount)
        self.cash -= cost
        if bucket == "fee":
            self.total_fees += cost
        elif bucket == "slippage":
            self.total_slippage += cost
        elif bucket == "leverage":
            self.leverage_cost += cost
        return cost

    def rebalance_trade(self) -> tuple[Literal["spot", "perp", "none"], Decimal]:
        """Return the larger leg to reduce and the mismatch notional."""

        if not self.in_position:
            return "none", _ZERO
        spot_notional = self.spot_value()
        perp_notional = self.short_notional()
        mismatch = spot_notional - perp_notional
        if mismatch > _ZERO:
            return "spot", mismatch
        if mismatch < _ZERO:
            return "perp", -mismatch
        return "none", _ZERO

    def rebalance(
        self,
        *,
        leg: Literal["spot", "perp"],
        trade_notional: Decimal | float | int | str,
        spot_price: Decimal | float | int | str,
        perp_mark: Decimal | float | int | str,
        fee_cost: Decimal | float | int | str = _ZERO,
        slippage_cost: Decimal | float | int | str = _ZERO,
    ) -> tuple[Decimal, Decimal]:
        """Reduce the larger leg by ``trade_notional`` and return before/after deltas."""

        if not self.in_position:
            return _ZERO, _ZERO
        self.mark(spot_price=spot_price, perp_mark=perp_mark)
        trade = require_non_negative("trade_notional", trade_notional)
        fees = require_non_negative("fee_cost", fee_cost)
        slip = require_non_negative("slippage_cost", slippage_cost)
        before = self.net_delta_usd()
        if trade == _ZERO:
            return before, before

        if leg == "spot":
            qty = min(self.spot_qty, trade / self.spot_price)
            proceeds = qty * self.spot_price
            self.spot_qty -= qty
            self.cash += proceeds
        elif leg == "perp":
            qty = min(self.perp_qty, trade / self.perp_mark)
            realized = qty * (self.perp_entry - self.perp_mark)
            self.perp_qty -= qty
            self.cash += realized
            if self.perp_qty == _ZERO:
                self.perp_entry = _ZERO
        else:
            raise ValueError(f"unknown rebalance leg {leg!r}")

        self.apply_cost(fees, bucket="fee")
        self.apply_cost(slip, bucket="slippage")
        self.current_notional = min(self.spot_value(), self.short_notional())
        after = self.net_delta_usd()
        return before, after

    def spot_value(self) -> Decimal:
        return self.spot_qty * self.spot_price

    def short_notional(self) -> Decimal:
        return self.perp_qty * self.perp_mark

    def perp_unrealized_pnl(self) -> Decimal:
        if self.perp_qty == _ZERO:
            return _ZERO
        return self.perp_qty * (self.perp_entry - self.perp_mark)

    def equity(self) -> Decimal:
        return self.reserve + self.cash + self.spot_value() + self.perp_unrealized_pnl()

    def deployable_equity(self) -> Decimal:
        return max(_ZERO, self.equity() - self.reserve)

    def margin_wallet(self) -> Decimal:
        """Equity backing the perp leg for margin/liquidation checks.

        Excludes the segregated reserve AND the perp leg's mark-to-market PnL, because
        ``VenueMarginModel`` re-adds the entry->mark PnL itself at the test mark. Equals
        ``cash + spot_value`` and is intentionally NOT floored at zero, so an underwater
        account (cash + spot_value < 0) yields a sub-1 health factor and liquidates.
        """
        return self.cash + self.spot_value()

    def net_delta_usd(self) -> Decimal:
        return self.spot_value() - self.short_notional()

    def net_delta_frac(self) -> Decimal:
        denominator = max(self.current_notional, self.spot_value(), self.short_notional())
        if denominator == _ZERO:
            return _ZERO
        return self.net_delta_usd() / denominator


def to_decimal(value: Decimal | float | int | str) -> Decimal:
    if isinstance(value, Decimal):
        return value
    return Decimal(str(value))


def require_non_negative(name: str, value: Decimal | float | int | str) -> Decimal:
    out = to_decimal(value)
    if not out.is_finite():
        raise ValueError(f"{name} must be finite")
    if out < _ZERO:
        raise ValueError(f"{name} must be non-negative")
    return out


def require_positive(name: str, value: Decimal | float | int | str) -> Decimal:
    out = require_non_negative(name, value)
    if out <= _ZERO:
        raise ValueError(f"{name} must be positive")
    return out


def quantize_decimal(
    value: Decimal | float | int | str,
    quantum: Decimal = MONEY_QUANT,
) -> Decimal:
    return to_decimal(value).quantize(quantum, rounding=ROUND_HALF_EVEN)


def canonical_str(value: Decimal | float | int | str, quantum: Decimal = MONEY_QUANT) -> str:
    return format(quantize_decimal(value, quantum), "f")

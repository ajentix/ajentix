"""Read-only Deribit public option-chain adapter.

The adapter implements ``OptionChainProvider`` for cache population only. It exposes no
order/account/write surface and imports ``ccxt`` lazily only when no client is injected.
"""

from __future__ import annotations

import math
import time
from collections.abc import Callable, Mapping, Sequence
from datetime import UTC, datetime
from typing import Any, Protocol, SupportsIndex, SupportsInt, runtime_checkable

from ajentix_quant.adapters.base import SourceQuality
from ajentix_quant.options.provider import OptionChainProvider
from ajentix_quant.options.types import OptionChainSnapshot, OptionLeg, OptionType, Side

_PROVIDER_SCHEMA_VERSION = "deribit-options-public-v1"
_MAX_MARKETS = 100_000


@runtime_checkable
class _SupportsTrunc(Protocol):
    def __trunc__(self) -> int: ...


def _finite_float(value: object, label: str) -> float:
    try:
        out = float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{label} must be a finite float, got {value!r}") from exc
    if not math.isfinite(out):
        raise ValueError(f"{label} must be finite, got {value!r}")
    return out


def _nonnegative_float(value: object, label: str) -> float:
    out = _finite_float(value, label)
    if out < 0.0:
        raise ValueError(f"{label} must be >= 0, got {value!r}")
    return out


def _positive_float(value: object, label: str) -> float:
    out = _finite_float(value, label)
    if out <= 0.0:
        raise ValueError(f"{label} must be > 0, got {value!r}")
    return out


def _positive_int(value: object, label: str) -> int:
    try:
        if not isinstance(
            value,
            str | bytes | bytearray | SupportsInt | SupportsIndex | _SupportsTrunc,
        ):
            raise TypeError
        out = int(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{label} must be an integer, got {value!r}") from exc
    if out <= 0:
        raise ValueError(f"{label} must be > 0, got {value!r}")
    return out


def _maybe_float(*values: object) -> float | None:
    for value in values:
        if value is not None and value != "":
            return _finite_float(value, "optional float")
    return None


def _first_present(*values: object, default: object | None = None) -> object | None:
    for value in values:
        if value is not None and value != "":
            return value
    return default



def _nested(mapping: Mapping[str, Any], *keys: str) -> object | None:
    current: object = mapping
    for key in keys:
        if not isinstance(current, Mapping):
            return None
        current = current.get(key)
    return current


def _parse_expiry_ms(market: Mapping[str, Any]) -> int:
    info = market.get("info") if isinstance(market.get("info"), Mapping) else {}
    assert isinstance(info, Mapping)
    value = _first_present(
        market.get("expiry"),
        market.get("expiration"),
        info.get("expiration_timestamp"),
        info.get("expiration"),
    )
    if value is not None and value != "":
        expiry = _positive_int(value, "option expiry")
        return expiry * 1000 if expiry < 10_000_000_000 else expiry

    expiry_dt = market.get("expiryDatetime") or info.get("expiration_datetime")
    if isinstance(expiry_dt, str) and expiry_dt:
        dt = datetime.fromisoformat(expiry_dt.replace("Z", "+00:00"))
        return _positive_int(dt.timestamp() * 1000, "option expiryDatetime")

    instrument_name = str(
        info.get("instrument_name") or market.get("id") or market.get("symbol") or ""
    )
    parts = instrument_name.split("-")
    if len(parts) >= 2:
        for fmt in ("%d%b%y", "%d%b%Y"):
            try:
                dt = datetime.strptime(parts[1].upper(), fmt).replace(tzinfo=UTC)
            except ValueError:
                continue
            return int(dt.timestamp() * 1000)
    raise ValueError(f"option market missing expiry: {market!r}")


def _parse_option_type(market: Mapping[str, Any]) -> OptionType:
    info = market.get("info") if isinstance(market.get("info"), Mapping) else {}
    assert isinstance(info, Mapping)
    raw = (
        market.get("optionType")
        or market.get("option_type")
        or info.get("option_type")
        or info.get("optionType")
        or ""
    )
    text = str(raw).lower()
    if text in {"call", "c"}:
        return OptionType.CALL
    if text in {"put", "p"}:
        return OptionType.PUT

    instrument_name = str(
        info.get("instrument_name") or market.get("id") or market.get("symbol") or ""
    ).upper()
    if instrument_name.endswith("-C"):
        return OptionType.CALL
    if instrument_name.endswith("-P"):
        return OptionType.PUT
    raise ValueError(f"option market missing option type: {market!r}")


def _parse_underlying(market: Mapping[str, Any]) -> str:
    info = market.get("info") if isinstance(market.get("info"), Mapping) else {}
    assert isinstance(info, Mapping)
    value = (
        market.get("base")
        or market.get("currency")
        or info.get("base_currency")
        or info.get("currency")
    )
    if value:
        return str(value).upper()
    instrument_name = str(
        info.get("instrument_name") or market.get("id") or market.get("symbol") or ""
    )
    return instrument_name.split("-", 1)[0].upper()


def _parse_market(market: Mapping[str, Any]) -> dict[str, Any]:
    info = market.get("info") if isinstance(market.get("info"), Mapping) else {}
    assert isinstance(info, Mapping)
    instrument_name = str(
        info.get("instrument_name") or market.get("id") or market.get("symbol") or ""
    )
    if not instrument_name:
        raise ValueError(f"option market missing instrument name: {market!r}")
    symbol = str(market.get("symbol") or instrument_name)
    return {
        "instrument_name": instrument_name,
        "symbol": symbol,
        "underlying": _parse_underlying(market),
        "expiry_ms": _parse_expiry_ms(market),
        "option_type": _parse_option_type(market),
        "strike": _positive_float(
            _first_present(market.get("strike"), info.get("strike")), "strike"
        ),
        "contract_multiplier": _positive_float(
            _first_present(
                market.get("contractSize"), info.get("contract_size"), default=1.0
            ),
            "contract multiplier",
        ),
        "min_tick": _positive_float(
            _first_present(
                _nested(market, "precision", "price"),
                info.get("tick_size"),
                info.get("min_price_increment"),
                default=0.0005,
            ),
            "min tick",
        ),
        "min_lot": _positive_float(
            _first_present(
                _nested(market, "limits", "amount", "min"),
                info.get("min_trade_amount"),
                info.get("min_amount"),
                default=1.0,
            ),
            "min lot",
        ),
        "settlement_index": str(
            _first_present(
                info.get("settlement_index"), default=f"{_parse_underlying(market)}-USD"
            )
        ),
        "premium_currency": str(
            _first_present(
                info.get("quote_currency"), info.get("premium_currency"), default="ETH"
            )
        ),
        "fee_currency": str(
            _first_present(
                info.get("fee_currency"), info.get("settlement_currency"), default="ETH"
            )
        ),
        "collateral_currency": str(
            _first_present(
                info.get("settlement_currency"),
                info.get("collateral_currency"),
                default="ETH",
            )
        ),
    }


def _is_option_market(market: Mapping[str, Any]) -> bool:
    info = market.get("info") if isinstance(market.get("info"), Mapping) else {}
    assert isinstance(info, Mapping)
    return bool(
        market.get("option")
        or market.get("type") == "option"
        or info.get("kind") == "option"
        or str(info.get("instrument_name") or market.get("id") or "").endswith(("-C", "-P"))
    )


class DeribitOptionsAdapter(OptionChainProvider):
    """Read-only public Deribit option-chain provider used to populate caches."""

    name = "deribit"

    def __init__(
        self,
        *,
        client: object | None = None,
        source_id: str = "deribit-public",
        scenario_id: str = "live-deribit-public",
        source_quality: SourceQuality = SourceQuality.VENUE,
        rate_limit_s: float = 0.0,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self._client = client
        self._source_id = source_id
        self._scenario_id = scenario_id
        self._source_quality = source_quality
        self._rate_limit_s = rate_limit_s
        self._sleep = sleep

    def _exchange(self) -> object:
        if self._client is None:
            import ccxt  # lazy: live connectivity is optional and never needed for tests

            self._client = ccxt.deribit({"enableRateLimit": True})
        return self._client

    def _fetch_markets(self) -> list[Mapping[str, Any]]:
        client = self._exchange()
        if hasattr(client, "fetch_markets"):
            markets = client.fetch_markets()  # type: ignore[attr-defined]
        elif hasattr(client, "load_markets"):
            loaded = client.load_markets()  # type: ignore[attr-defined]
            markets = list(loaded.values()) if isinstance(loaded, Mapping) else loaded
        else:
            raise TypeError("Deribit options client must expose fetch_markets or load_markets")
        if not isinstance(markets, Sequence):
            raise ValueError("fetch_markets/load_markets must return a sequence")
        if len(markets) > _MAX_MARKETS:
            raise RuntimeError(f"Deribit option market list exceeded {_MAX_MARKETS} entries")
        return [m for m in markets if isinstance(m, Mapping)]

    def _option_markets(self, underlying: str) -> list[dict[str, Any]]:
        want = underlying.upper()
        parsed = [
            _parse_market(market)
            for market in self._fetch_markets()
            if _is_option_market(market) and _parse_underlying(market) == want
        ]
        parsed.sort(key=lambda m: (m["expiry_ms"], m["strike"], m["option_type"].value))
        return parsed

    def available_expiries(self, underlying: str) -> tuple[int, ...]:
        """Return Deribit option expiries for ``underlying`` from public market metadata."""

        return tuple(sorted({int(m["expiry_ms"]) for m in self._option_markets(underlying)}))

    def instrument_metadata(self, underlying: str) -> Mapping[str, Any]:
        """Return deterministic public metadata needed by cache validation."""

        markets = self._option_markets(underlying)
        return {
            "exchange": self.name,
            "underlying": underlying.upper(),
            "source_id": self._source_id,
            "source_quality": self._source_quality.value,
            "expiries_ms": sorted({int(m["expiry_ms"]) for m in markets}),
            "instruments": [
                {
                    "instrument_name": m["instrument_name"],
                    "symbol": m["symbol"],
                    "expiry_ms": m["expiry_ms"],
                    "option_type": m["option_type"].value,
                    "strike": m["strike"],
                    "contract_multiplier": m["contract_multiplier"],
                    "min_tick": m["min_tick"],
                    "min_lot": m["min_lot"],
                    "settlement_index": m["settlement_index"],
                    "premium_currency": m["premium_currency"],
                    "fee_currency": m["fee_currency"],
                    "collateral_currency": m["collateral_currency"],
                }
                for m in markets
            ],
        }

    def _fetch_ticker(self, market: Mapping[str, Any]) -> Mapping[str, Any]:
        client = self._exchange()
        symbol = str(market["symbol"])
        if hasattr(client, "fetch_ticker"):
            ticker = client.fetch_ticker(symbol)  # type: ignore[attr-defined]
        elif hasattr(client, "public_get_get_book_summary_by_instrument"):
            method = client.public_get_get_book_summary_by_instrument
            response = method({"instrument_name": market["instrument_name"]})
            result = response.get("result", []) if isinstance(response, Mapping) else []
            ticker = result[0] if result else {}
        else:
            raise TypeError("Deribit options client must expose fetch_ticker or book summary")
        if not isinstance(ticker, Mapping):
            raise ValueError(f"ticker for {symbol} must be a mapping")
        if self._rate_limit_s > 0.0:
            self._sleep(self._rate_limit_s)
        return ticker

    def chain_snapshot(self, underlying: str, ts_ms: int, expiry_ms: int) -> OptionChainSnapshot:
        """Return one public option-chain snapshot for ``underlying`` and ``expiry_ms``."""

        markets = [m for m in self._option_markets(underlying) if m["expiry_ms"] == expiry_ms]
        if not markets:
            raise KeyError(f"no Deribit options for {underlying.upper()} expiry {expiry_ms}")

        legs: list[OptionLeg] = []
        source_ts_values: list[int] = []
        settlement_index_price: float | None = None
        index_price: float | None = None
        for market in markets:
            ticker = self._fetch_ticker(market)
            info = ticker.get("info") if isinstance(ticker.get("info"), Mapping) else {}
            assert isinstance(info, Mapping)
            source_ts = _positive_int(
                _first_present(ticker.get("timestamp"), info.get("timestamp"), default=ts_ms),
                f"{market['instrument_name']} ticker timestamp",
            )
            source_ts_values.append(source_ts)
            bid = _nonnegative_float(
                _first_present(ticker.get("bid"), info.get("best_bid_price")),
                f"{market['instrument_name']} bid",
            )
            ask = _nonnegative_float(
                _first_present(ticker.get("ask"), info.get("best_ask_price")),
                f"{market['instrument_name']} ask",
            )
            if bid > ask:
                raise ValueError(
                    f"{market['instrument_name']} crossed book: bid {bid} > ask {ask}"
                )
            bid_amount = _positive_float(
                _first_present(ticker.get("bidVolume"), info.get("best_bid_amount")),
                f"{market['instrument_name']} bid amount",
            )
            ask_amount = _positive_float(
                _first_present(ticker.get("askVolume"), info.get("best_ask_amount")),
                f"{market['instrument_name']} ask amount",
            )
            settlement_index_price = _maybe_float(
                settlement_index_price,
                ticker.get("indexPrice"),
                info.get("index_price"),
            )
            index_price = _maybe_float(
                index_price,
                ticker.get("underlyingPrice"),
                info.get("underlying_price"),
            )
            legs.append(
                OptionLeg(
                    instrument_name=str(market["instrument_name"]),
                    underlying=underlying.upper(),
                    contract_multiplier=float(market["contract_multiplier"]),
                    option_type=market["option_type"],
                    side=Side.LONG,
                    strike=float(market["strike"]),
                    expiry_ms=int(market["expiry_ms"]),
                    settlement_style="european",
                    settlement_index=str(market["settlement_index"]),
                    premium_currency=str(market["premium_currency"]),
                    fee_currency=str(market["fee_currency"]),
                    collateral_currency=str(market["collateral_currency"]),
                    usd_conversion_source="deribit_public_index",
                    quote_ts_ms=source_ts,
                    quote_age_s=max(0.0, (ts_ms - source_ts) / 1000.0),
                    bid_price=bid,
                    bid_amount=bid_amount,
                    bid_iv=_nonnegative_float(
                        ticker.get("bidIv") or info.get("bid_iv") or 0.0,
                        f"{market['instrument_name']} bid iv",
                    ),
                    ask_price=ask,
                    ask_amount=ask_amount,
                    ask_iv=_nonnegative_float(
                        ticker.get("askIv") or info.get("ask_iv") or 0.0,
                        f"{market['instrument_name']} ask iv",
                    ),
                    mark_price=_maybe_float(ticker.get("markPrice"), info.get("mark_price")),
                    greek_provenance_key="deribit_vendor_greeks",
                    min_tick=float(market["min_tick"]),
                    min_lot=float(market["min_lot"]),
                    source_quality=self._source_quality,
                )
            )

        return OptionChainSnapshot(
            underlying=underlying.upper(),
            exchange=self.name,
            snapshot_ts_ms=_positive_int(ts_ms, "snapshot timestamp"),
            source_ts_ms=max(source_ts_values),
            source_id=self._source_id,
            scenario_id=self._scenario_id,
            settlement_index_price=settlement_index_price,
            index_price=index_price,
            usd_conversion_inputs={
                "source": "deribit_public_index",
                "underlying": underlying.upper(),
            },
            legs=tuple(legs),
            source_quality_map={"option_chain": self._source_quality},
            schema_version=_PROVIDER_SCHEMA_VERSION,
            manifest_sha256="live-public-no-manifest",
        )


__all__ = ["DeribitOptionsAdapter"]

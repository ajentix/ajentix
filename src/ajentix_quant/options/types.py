"""Immutable canonical option value objects for defined-risk VRP research."""

from __future__ import annotations

import hashlib
import json
import math
from collections.abc import Mapping
from dataclasses import dataclass, field, fields
from enum import StrEnum
from types import MappingProxyType
from typing import Any

from ajentix_quant.adapters.base import SourceQuality


class OptionType(StrEnum):
    """Canonical option contract type."""

    CALL = "call"
    PUT = "put"


class Side(StrEnum):
    """Canonical leg direction from the strategy/account perspective."""

    LONG = "long"
    SHORT = "short"


class StructureType(StrEnum):
    """Supported two-leg defined-risk VRP structures."""

    PUT_CREDIT_SPREAD = "put_credit_spread"
    CALL_CREDIT_SPREAD = "call_credit_spread"


def _set(instance: object, name: str, value: Any) -> None:
    object.__setattr__(instance, name, value)


def _coerce_enum[EnumT: StrEnum](enum_type: type[EnumT], name: str, value: EnumT | str) -> EnumT:
    try:
        return value if isinstance(value, enum_type) else enum_type(value)
    except (TypeError, ValueError) as exc:
        allowed = ", ".join(member.value for member in enum_type)
        raise ValueError(f"{name} must be one of: {allowed}") from exc


def _require_non_empty(name: str, value: str) -> str:
    if not isinstance(value, str) or not value:
        raise ValueError(f"{name} must be a non-empty string")
    return value


def _require_int(name: str, value: int, *, positive: bool = False) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError(f"{name} must be an integer")
    if positive and value <= 0:
        raise ValueError(f"{name} must be positive")
    if not positive and value < 0:
        raise ValueError(f"{name} must be non-negative")
    return value


def _require_finite(name: str, value: float) -> float:
    value = float(value)
    if not math.isfinite(value):
        raise ValueError(f"{name} must be finite")
    return value


def _require_non_negative(name: str, value: float) -> float:
    value = _require_finite(name, value)
    if value < 0.0:
        raise ValueError(f"{name} must be non-negative")
    return value


def _require_positive(name: str, value: float) -> float:
    value = _require_finite(name, value)
    if value <= 0.0:
        raise ValueError(f"{name} must be positive")
    return value


def _require_european(name: str, value: str) -> str:
    value = _require_non_empty(name, value)
    if value != "european":
        raise ValueError(f"{name} must be european")
    return value


def _freeze_mapping(mapping: Mapping[str, Any], name: str) -> Mapping[str, Any]:
    if not isinstance(mapping, Mapping):
        raise ValueError(f"{name} must be a mapping")
    return MappingProxyType(dict(mapping))


def _freeze_source_quality_map(
    mapping: Mapping[str, SourceQuality | str], name: str
) -> Mapping[str, SourceQuality]:
    if not isinstance(mapping, Mapping):
        raise ValueError(f"{name} must be a mapping")
    coerced = {
        str(key): _coerce_enum(SourceQuality, f"{name}[{key}]", value)
        for key, value in mapping.items()
    }
    return MappingProxyType(coerced)


@dataclass(frozen=True, kw_only=True)
class OptionLeg:
    """One immutable quoted option leg from a normalized chain snapshot."""

    instrument_name: str
    underlying: str
    contract_multiplier: float
    option_type: OptionType
    side: Side
    strike: float
    expiry_ms: int
    settlement_style: str
    settlement_index: str
    premium_currency: str
    fee_currency: str
    collateral_currency: str
    usd_conversion_source: str
    quote_ts_ms: int
    quote_age_s: float
    bid_price: float
    bid_amount: float
    bid_iv: float
    ask_price: float
    ask_amount: float
    ask_iv: float
    mark_price: float | None
    greek_provenance_key: str
    min_tick: float
    min_lot: float
    source_quality: SourceQuality

    def __post_init__(self) -> None:
        _set(
            self,
            "instrument_name",
            _require_non_empty("instrument_name", self.instrument_name),
        )
        _set(self, "underlying", _require_non_empty("underlying", self.underlying))
        _set(
            self,
            "contract_multiplier",
            _require_positive("contract_multiplier", self.contract_multiplier),
        )
        _set(
            self,
            "option_type",
            _coerce_enum(OptionType, "option_type", self.option_type),
        )
        _set(self, "side", _coerce_enum(Side, "side", self.side))
        _set(self, "strike", _require_positive("strike", self.strike))
        _set(
            self,
            "expiry_ms",
            _require_int("expiry_ms", self.expiry_ms, positive=True),
        )
        _set(
            self,
            "settlement_style",
            _require_european("settlement_style", self.settlement_style),
        )
        _set(
            self,
            "settlement_index",
            _require_non_empty("settlement_index", self.settlement_index),
        )
        _set(
            self,
            "premium_currency",
            _require_non_empty("premium_currency", self.premium_currency),
        )
        _set(self, "fee_currency", _require_non_empty("fee_currency", self.fee_currency))
        _set(
            self,
            "collateral_currency",
            _require_non_empty("collateral_currency", self.collateral_currency),
        )
        _set(
            self,
            "usd_conversion_source",
            _require_non_empty("usd_conversion_source", self.usd_conversion_source),
        )
        _set(self, "quote_ts_ms", _require_int("quote_ts_ms", self.quote_ts_ms))
        _set(
            self,
            "quote_age_s",
            _require_non_negative("quote_age_s", self.quote_age_s),
        )
        _set(self, "bid_price", _require_non_negative("bid_price", self.bid_price))
        _set(
            self,
            "bid_amount",
            _require_non_negative("bid_amount", self.bid_amount),
        )
        _set(self, "bid_iv", _require_non_negative("bid_iv", self.bid_iv))
        _set(self, "ask_price", _require_non_negative("ask_price", self.ask_price))
        _set(
            self,
            "ask_amount",
            _require_non_negative("ask_amount", self.ask_amount),
        )
        _set(self, "ask_iv", _require_non_negative("ask_iv", self.ask_iv))
        if self.mark_price is not None:
            _set(
                self,
                "mark_price",
                _require_non_negative("mark_price", self.mark_price),
            )
        _set(
            self,
            "greek_provenance_key",
            _require_non_empty("greek_provenance_key", self.greek_provenance_key),
        )
        _set(self, "min_tick", _require_positive("min_tick", self.min_tick))
        _set(self, "min_lot", _require_positive("min_lot", self.min_lot))
        _set(
            self,
            "source_quality",
            _coerce_enum(SourceQuality, "source_quality", self.source_quality),
        )


@dataclass(frozen=True, kw_only=True)
class OptionChainSnapshot:
    """Immutable normalized option chain snapshot consumed by strategy/backtests."""

    underlying: str
    exchange: str
    snapshot_ts_ms: int
    source_ts_ms: int
    source_id: str
    scenario_id: str
    settlement_index_price: float | None
    index_price: float | None
    usd_conversion_inputs: Mapping[str, Any]
    legs: tuple[OptionLeg, ...]
    source_quality_map: Mapping[str, SourceQuality]
    schema_version: str
    manifest_sha256: str

    def __post_init__(self) -> None:
        _set(self, "underlying", _require_non_empty("underlying", self.underlying))
        _set(self, "exchange", _require_non_empty("exchange", self.exchange))
        if self.exchange != "deribit":
            raise ValueError("exchange must be deribit")
        _set(
            self,
            "snapshot_ts_ms",
            _require_int("snapshot_ts_ms", self.snapshot_ts_ms, positive=True),
        )
        _set(
            self,
            "source_ts_ms",
            _require_int("source_ts_ms", self.source_ts_ms, positive=True),
        )
        _set(self, "source_id", _require_non_empty("source_id", self.source_id))
        _set(self, "scenario_id", _require_non_empty("scenario_id", self.scenario_id))
        if self.settlement_index_price is not None:
            _set(
                self,
                "settlement_index_price",
                _require_positive("settlement_index_price", self.settlement_index_price),
            )
        if self.index_price is not None:
            _set(self, "index_price", _require_positive("index_price", self.index_price))
        _set(
            self,
            "usd_conversion_inputs",
            _freeze_mapping(self.usd_conversion_inputs, "usd_conversion_inputs"),
        )
        legs = tuple(self.legs)
        if not legs:
            raise ValueError("legs must be non-empty")
        for leg in legs:
            if not isinstance(leg, OptionLeg):
                raise ValueError("legs must contain OptionLeg values")
            if leg.underlying != self.underlying:
                raise ValueError("all legs must match snapshot underlying")
        _set(self, "legs", legs)
        _set(
            self,
            "source_quality_map",
            _freeze_source_quality_map(self.source_quality_map, "source_quality_map"),
        )
        _set(
            self,
            "schema_version",
            _require_non_empty("schema_version", self.schema_version),
        )
        _set(
            self,
            "manifest_sha256",
            _require_non_empty("manifest_sha256", self.manifest_sha256),
        )

    def leg_by_instrument_name(self, instrument_name: str) -> OptionLeg:
        """Return the exact named leg; raise ``KeyError`` on miss."""

        for leg in self.legs:
            if leg.instrument_name == instrument_name:
                return leg
        raise KeyError(instrument_name)


def _canonical_leg(leg: OptionLeg) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for dataclass_field in fields(OptionLeg):
        value = getattr(leg, dataclass_field.name)
        out[dataclass_field.name] = (
            value.value if isinstance(value, StrEnum) else value
        )
    return out


def _canonical_structure_payload(
    structure: DefinedRiskStructure,
) -> dict[str, Any]:
    params: dict[str, Any] = {}
    for dataclass_field in fields(DefinedRiskStructure):
        if dataclass_field.name in {"structure_id", "legs"}:
            continue
        value = getattr(structure, dataclass_field.name)
        params[dataclass_field.name] = (
            value.value if isinstance(value, StrEnum) else value
        )
    legs = sorted(
        (_canonical_leg(leg) for leg in structure.legs),
        key=lambda item: item["instrument_name"],
    )
    return {"legs": legs, "params": params}


def _derive_structure_id(structure: DefinedRiskStructure) -> str:
    payload = _canonical_structure_payload(structure)
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    digest = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
    return f"drs-{digest[:12]}"


@dataclass(frozen=True, kw_only=True)
class DefinedRiskStructure:
    """Two-leg capped credit spread with a deterministic identity and invariant."""

    structure_type: StructureType
    legs: tuple[OptionLeg, ...]
    quantity: int
    entry_snapshot_id: str
    expiry_ms: int
    dte_days: int
    settlement_style: str
    settlement_index: str
    premium_currency: str
    fee_currency: str
    collateral_currency: str
    usd_conversion_source: str
    net_credit: float
    width: float
    fees: float
    max_loss_usd: float
    max_gain_usd: float
    entry_quote_ts_ms: int
    max_quote_age_s: float
    frozen_param_key: str
    structure_id: str = field(init=False)

    def __post_init__(self) -> None:
        _set(
            self,
            "structure_type",
            _coerce_enum(StructureType, "structure_type", self.structure_type),
        )
        legs = tuple(self.legs)
        _set(self, "legs", legs)
        _set(self, "quantity", _require_int("quantity", self.quantity, positive=True))
        _set(
            self,
            "entry_snapshot_id",
            _require_non_empty("entry_snapshot_id", self.entry_snapshot_id),
        )
        _set(self, "expiry_ms", _require_int("expiry_ms", self.expiry_ms, positive=True))
        _set(self, "dte_days", _require_int("dte_days", self.dte_days))
        _set(
            self,
            "settlement_style",
            _require_european("settlement_style", self.settlement_style),
        )
        _set(
            self,
            "settlement_index",
            _require_non_empty("settlement_index", self.settlement_index),
        )
        _set(
            self,
            "premium_currency",
            _require_non_empty("premium_currency", self.premium_currency),
        )
        _set(self, "fee_currency", _require_non_empty("fee_currency", self.fee_currency))
        _set(
            self,
            "collateral_currency",
            _require_non_empty("collateral_currency", self.collateral_currency),
        )
        _set(
            self,
            "usd_conversion_source",
            _require_non_empty("usd_conversion_source", self.usd_conversion_source),
        )
        _set(self, "net_credit", _require_positive("net_credit", self.net_credit))
        _set(self, "width", _require_positive("width", self.width))
        _set(self, "fees", _require_non_negative("fees", self.fees))
        _set(self, "max_loss_usd", _require_positive("max_loss_usd", self.max_loss_usd))
        _set(self, "max_gain_usd", _require_positive("max_gain_usd", self.max_gain_usd))
        _set(
            self,
            "entry_quote_ts_ms",
            _require_int("entry_quote_ts_ms", self.entry_quote_ts_ms, positive=True),
        )
        _set(
            self,
            "max_quote_age_s",
            _require_non_negative("max_quote_age_s", self.max_quote_age_s),
        )
        _set(
            self,
            "frozen_param_key",
            _require_non_empty("frozen_param_key", self.frozen_param_key),
        )
        self._validate_defined_risk()
        _set(self, "structure_id", _derive_structure_id(self))

    def _validate_defined_risk(self) -> None:
        if len(self.legs) != 2:
            raise ValueError("defined-risk structures must contain exactly two option legs")
        if not all(isinstance(leg, OptionLeg) for leg in self.legs):
            raise ValueError("legs must contain OptionLeg values")
        side_counts = {Side.LONG: 0, Side.SHORT: 0}
        for leg in self.legs:
            side_counts[leg.side] += 1
        if side_counts[Side.LONG] != 1 or side_counts[Side.SHORT] != 1:
            raise ValueError(
                "defined-risk structures require exactly one long leg and one short leg"
            )

        short_leg = next(leg for leg in self.legs if leg.side is Side.SHORT)
        long_leg = next(leg for leg in self.legs if leg.side is Side.LONG)
        self._validate_leg_consistency(short_leg, long_leg)
        self._validate_spread_shape(short_leg, long_leg)
        self._validate_max_loss(short_leg, long_leg)

    def _validate_leg_consistency(
        self, short_leg: OptionLeg, long_leg: OptionLeg
    ) -> None:
        shared_fields = (
            "underlying",
            "expiry_ms",
            "settlement_style",
            "settlement_index",
            "premium_currency",
            "fee_currency",
            "collateral_currency",
            "usd_conversion_source",
            "contract_multiplier",
        )
        for name in shared_fields:
            if getattr(short_leg, name) != getattr(long_leg, name):
                raise ValueError(f"spread legs must share {name}")
        if short_leg.expiry_ms != self.expiry_ms:
            raise ValueError("structure expiry_ms must match leg expiry_ms")
        for name in (
            "settlement_style",
            "settlement_index",
            "premium_currency",
            "fee_currency",
            "collateral_currency",
            "usd_conversion_source",
        ):
            if getattr(self, name) != getattr(short_leg, name):
                raise ValueError(f"structure {name} must match leg {name}")

    def _validate_spread_shape(self, short_leg: OptionLeg, long_leg: OptionLeg) -> None:
        if self.structure_type is StructureType.PUT_CREDIT_SPREAD:
            if (
                short_leg.option_type is not OptionType.PUT
                or long_leg.option_type is not OptionType.PUT
            ):
                raise ValueError("put_credit_spread requires put legs")
            if short_leg.strike <= long_leg.strike:
                raise ValueError(
                    "put_credit_spread must be capped by a lower-strike long put"
                )
        elif self.structure_type is StructureType.CALL_CREDIT_SPREAD:
            if (
                short_leg.option_type is not OptionType.CALL
                or long_leg.option_type is not OptionType.CALL
            ):
                raise ValueError("call_credit_spread requires call legs")
            if short_leg.strike >= long_leg.strike:
                raise ValueError(
                    "call_credit_spread must be capped by a higher-strike long call"
                )
        strike_width = abs(short_leg.strike - long_leg.strike)
        if not math.isclose(self.width, strike_width, rel_tol=1e-9, abs_tol=1e-9):
            raise ValueError("width must equal the absolute strike difference")
        if self.net_credit >= self.width:
            raise ValueError("net_credit must be less than width for capped max loss")

    def _validate_max_loss(self, short_leg: OptionLeg, long_leg: OptionLeg) -> None:
        multiplier = short_leg.contract_multiplier
        if not math.isclose(multiplier, long_leg.contract_multiplier, rel_tol=1e-12):
            raise ValueError("spread legs must share contract_multiplier")
        expected = (self.width - self.net_credit) * multiplier * self.quantity
        if expected <= 0.0:
            raise ValueError("defined-risk max loss must be capped and positive")
        if not math.isclose(
            self.max_loss_usd, expected, rel_tol=1e-9, abs_tol=1e-9
        ):
            raise ValueError(
                "max_loss_usd must equal "
                "(width - net_credit) * contract_multiplier * quantity"
            )


@dataclass(frozen=True, kw_only=True)
class OptionCostBreakdown:
    """Non-computing container for the future authorizing option cost path."""

    structure_id: str
    per_leg_crossing_cost: Mapping[str, float]
    fees: Mapping[str, float]
    min_tick_lot_rounding: Mapping[str, float]
    usd_conversion: Mapping[str, Any]
    entry_cost: float
    exit_cost: float
    expiry_settlement_cost: float
    safety_margin: float
    total_cost_usd: float
    net_credit_usd: float
    max_loss_usd: float
    assumptions_hash: str
    authorizing: bool
    non_authorizing_reason: str | None

    def __post_init__(self) -> None:
        _set(
            self,
            "structure_id",
            _require_non_empty("structure_id", self.structure_id),
        )
        _set(
            self,
            "per_leg_crossing_cost",
            _freeze_mapping(self.per_leg_crossing_cost, "per_leg_crossing_cost"),
        )
        _set(self, "fees", _freeze_mapping(self.fees, "fees"))
        _set(
            self,
            "min_tick_lot_rounding",
            _freeze_mapping(self.min_tick_lot_rounding, "min_tick_lot_rounding"),
        )
        _set(
            self,
            "usd_conversion",
            _freeze_mapping(self.usd_conversion, "usd_conversion"),
        )
        _set(self, "entry_cost", _require_non_negative("entry_cost", self.entry_cost))
        _set(self, "exit_cost", _require_non_negative("exit_cost", self.exit_cost))
        _set(
            self,
            "expiry_settlement_cost",
            _require_non_negative("expiry_settlement_cost", self.expiry_settlement_cost),
        )
        _set(
            self,
            "safety_margin",
            _require_non_negative("safety_margin", self.safety_margin),
        )
        _set(
            self,
            "total_cost_usd",
            _require_non_negative("total_cost_usd", self.total_cost_usd),
        )
        _set(self, "net_credit_usd", _require_finite("net_credit_usd", self.net_credit_usd))
        _set(self, "max_loss_usd", _require_positive("max_loss_usd", self.max_loss_usd))
        _set(
            self,
            "assumptions_hash",
            _require_non_empty("assumptions_hash", self.assumptions_hash),
        )
        if not isinstance(self.authorizing, bool):
            raise ValueError("authorizing must be boolean")
        if self.authorizing and self.non_authorizing_reason is not None:
            raise ValueError("authorizing breakdowns cannot carry non_authorizing_reason")
        if not self.authorizing:
            _require_non_empty("non_authorizing_reason", self.non_authorizing_reason or "")


__all__ = [
    "DefinedRiskStructure",
    "OptionChainSnapshot",
    "OptionCostBreakdown",
    "OptionLeg",
    "OptionType",
    "Side",
    "SourceQuality",
    "StructureType",
]

"""Deterministic on-disk cache: ``aq-cache-v1`` (manifest.json + canonical CSV files).

Stdlib only (no pandas/polars) so CI and the structural gate run with no heavy deps and no
network. Rows are written sorted + deduped via ``csv.writer``; ``manifest.json`` carries a
sha256 per file. Loading is **fail-closed**: schema mismatch, scenario-id mismatch, sha
mismatch, header mismatch, row-width mismatch, non-finite/negative numeric domains, manifest
row-count/symbol/venue/timeframe inconsistency, unsorted/duplicate timestamps, a disallowed or
missing ``source_quality``, or a missing required (per-symbol) stream all raise
``CacheValidationError``.

Layout::

    <cache_root>/<scenario_id>/
        manifest.json
        funding.csv      timestamp_ms,symbol,venue,rate,interval_hours,source
        ohlcv.csv        timestamp_ms,symbol,venue,market_type,price_type,timeframe,o,h,l,c,v,source

``ohlcv.csv`` volume is nullable (empty cell -> ``None``) for mark/index klines (they carry no
volume); trade-price rows require a finite non-negative volume. Raw cache prices are finite
``float``; the Decimal / scaled-int canonical money boundary begins in the G006 backtest
ledger, not here. Instrument metadata / fees / maintenance tiers are declared as manifest
streams (sha + source_quality validated) and parsed by their consumers (G004).
"""

from __future__ import annotations

import csv
import io
import json
import math
from collections.abc import Mapping, Sequence
from pathlib import Path

from ..adapters.base import (
    FundingRate,
    HistoricalCandle,
    MarketDataset,
    MarketType,
    PriceType,
    SourceQuality,
    StreamKey,
    StreamName,
    stream_spec,
)

SCHEMA_VERSION = "aq-cache-v1"
GENERATOR_VERSION = "ajentix-quant/phase1-g002"

_FUNDING_FILE = "funding.csv"
_OHLCV_FILE = "ohlcv.csv"

_FUNDING_HEADER = ["timestamp_ms", "symbol", "venue", "rate", "interval_hours", "source"]
_OHLCV_HEADER = [
    "timestamp_ms",
    "symbol",
    "venue",
    "market_type",
    "price_type",
    "timeframe",
    "open",
    "high",
    "low",
    "close",
    "volume",
    "source",
]

# Market-data streams a replay scenario must carry, per symbol, to be loadable by default.
DEFAULT_REQUIRED_STREAMS: tuple[StreamName, ...] = (
    StreamName.FUNDING_HISTORY,
    StreamName.SPOT_TRADE_OHLCV,
    StreamName.PERP_TRADE_OHLCV,
    StreamName.PERP_MARK_OHLCV,
)

# OHLCV StreamName <-> (market_type, price_type) mapping.
_OHLCV_STREAM_BY_KEY: dict[tuple[MarketType, PriceType], StreamName] = {
    (MarketType.SPOT, PriceType.TRADE): StreamName.SPOT_TRADE_OHLCV,
    (MarketType.LINEAR_PERP, PriceType.TRADE): StreamName.PERP_TRADE_OHLCV,
    (MarketType.LINEAR_PERP, PriceType.MARK): StreamName.PERP_MARK_OHLCV,
    (MarketType.LINEAR_PERP, PriceType.INDEX): StreamName.INDEX_OHLCV,
}
# (market_type, price_type) for a required OHLCV StreamName (inverse of the map above).
_KEY_BY_OHLCV_STREAM: dict[StreamName, tuple[MarketType, PriceType]] = {
    name: key for key, name in _OHLCV_STREAM_BY_KEY.items()
}


class CacheValidationError(Exception):
    """Raised when a cache fails fail-closed validation."""


def sha256_text(text: str) -> str:
    import hashlib

    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _require(condition: bool, message: str) -> None:
    if not condition:
        raise CacheValidationError(message)


def _ohlcv_stream_name(key: StreamKey) -> StreamName:
    name = _OHLCV_STREAM_BY_KEY.get((key.market_type, key.price_type))
    _require(
        name is not None,
        f"no StreamName for market_type={key.market_type.value} price_type={key.price_type.value}",
    )
    assert name is not None
    return name


def _fmt_num(value: float) -> str:
    # canonical, lossless round-trip for floats
    return repr(float(value))


def _require_finite(value: float, label: str) -> float:
    _require(math.isfinite(value), f"{label} must be finite, got {value!r}")
    return value


# ---------------------------------------------------------------------------
# write
# ---------------------------------------------------------------------------
def _dedupe_or_conflict(rows: list[tuple], key_len: int, label: str) -> list[tuple]:
    """Drop byte-identical duplicate primary keys; raise on conflicting duplicates."""
    seen: dict[tuple, tuple] = {}
    for row in rows:
        key = row[:key_len]
        prior = seen.get(key)
        if prior is None:
            seen[key] = row
        elif prior != row:
            raise CacheValidationError(f"{label} conflicting duplicate primary key: {key}")
    return list(seen.values())


def _funding_rows(funding: Mapping[str, Sequence[FundingRate]], venue: str) -> list[list[str]]:
    rows: list[tuple] = []
    for symbol, series in funding.items():
        for fr in series:
            _require_finite(fr.rate, f"funding[{symbol}].rate")
            _require(
                math.isfinite(fr.interval_hours) and fr.interval_hours > 0,
                f"funding[{symbol}].interval_hours must be finite > 0",
            )
            _require(fr.timestamp >= 0, f"funding[{symbol}].timestamp must be >= 0")
            rows.append(
                (str(fr.timestamp), fr.symbol, venue, _fmt_num(fr.rate),
                 _fmt_num(fr.interval_hours), "venue")
            )
    rows = _dedupe_or_conflict(rows, key_len=2, label="funding")  # key = (timestamp, symbol)
    rows.sort(key=lambda r: (r[1], int(r[0])))
    return [list(r) for r in rows]


def _ohlcv_rows(ohlcv: Mapping[StreamKey, Sequence[HistoricalCandle]]) -> list[list[str]]:
    rows: list[tuple] = []
    for key, series in ohlcv.items():
        is_trade = key.price_type is PriceType.TRADE
        for c in series:
            for fld in ("open", "high", "low", "close"):
                _require_finite(getattr(c, fld), f"ohlcv[{key}].{fld}")
            _require(c.timestamp_ms >= 0, f"ohlcv[{key}].timestamp must be >= 0")
            if is_trade:
                _require(
                    c.volume is not None and math.isfinite(c.volume) and c.volume >= 0,
                    f"ohlcv[{key}] trade volume must be finite >= 0",
                )
            elif c.volume is not None:
                _require(math.isfinite(c.volume) and c.volume >= 0, f"ohlcv[{key}] volume invalid")
            rows.append(
                (str(c.timestamp_ms), c.symbol, c.venue, c.market_type.value, c.price_type.value,
                 c.timeframe, _fmt_num(c.open), _fmt_num(c.high), _fmt_num(c.low),
                 _fmt_num(c.close), "" if c.volume is None else _fmt_num(c.volume), "venue")
            )
    # key = (timestamp, symbol, market_type, price_type)
    rows = _dedupe_or_conflict(
        [(r[0], r[1], r[3], r[4], *r) for r in rows], key_len=4, label="ohlcv"
    )
    rows = [r[4:] for r in rows]
    rows.sort(key=lambda r: (r[1], r[3], r[4], int(r[0])))
    return [list(r) for r in rows]


def _csv_text(header: Sequence[str], rows: Sequence[Sequence[str]]) -> str:
    buf = io.StringIO()
    writer = csv.writer(buf, lineterminator="\n")
    writer.writerow(header)
    for row in rows:
        writer.writerow(row)
    return buf.getvalue()


def write_cache(
    cache_root: str | Path,
    scenario_id: str,
    *,
    venue: str,
    timeframe: str,
    funding: Mapping[str, Sequence[FundingRate]],
    ohlcv: Mapping[StreamKey, Sequence[HistoricalCandle]],
    source_quality: Mapping[StreamName, SourceQuality],
    train_until_ms: int | None = None,
    param_freeze_hash: str | None = None,
) -> Path:
    """Write a deterministic ``aq-cache-v1`` scenario directory and return its path."""
    scenario_dir = Path(cache_root) / scenario_id
    scenario_dir.mkdir(parents=True, exist_ok=True)

    funding_rows = _funding_rows(funding, venue)
    ohlcv_rows = _ohlcv_rows(ohlcv)
    funding_text = _csv_text(_FUNDING_HEADER, funding_rows)
    ohlcv_text = _csv_text(_OHLCV_HEADER, ohlcv_rows)
    (scenario_dir / _FUNDING_FILE).write_text(funding_text, encoding="utf-8")
    (scenario_dir / _OHLCV_FILE).write_text(ohlcv_text, encoding="utf-8")

    symbols = sorted({*funding.keys(), *(k.symbol for k in ohlcv)})
    manifest = {
        "schema_version": SCHEMA_VERSION,
        "generator_version": GENERATOR_VERSION,
        "venue": venue,
        "timeframe": timeframe,
        "scenario_id": scenario_id,
        "symbols": symbols,
        "train_until_ms": train_until_ms,
        "param_freeze_hash": param_freeze_hash,
        "source_quality": {k.value: v.value for k, v in source_quality.items()},
        "sha256_by_file": {
            _FUNDING_FILE: sha256_text(funding_text),
            _OHLCV_FILE: sha256_text(ohlcv_text),
        },
        "row_counts": {_FUNDING_FILE: len(funding_rows), _OHLCV_FILE: len(ohlcv_rows)},
    }
    (scenario_dir / "manifest.json").write_text(
        json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    return scenario_dir


# ---------------------------------------------------------------------------
# load
# ---------------------------------------------------------------------------
def _read_manifest(scenario_dir: Path, scenario_id: str) -> dict:
    manifest_path = scenario_dir / "manifest.json"
    _require(manifest_path.is_file(), f"missing manifest.json in {scenario_dir}")
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise CacheValidationError(f"manifest.json is not valid JSON: {exc}") from exc
    _require(isinstance(manifest, dict), "manifest.json must be a JSON object")
    _require(
        manifest.get("schema_version") == SCHEMA_VERSION,
        f"schema_version must be {SCHEMA_VERSION}, got {manifest.get('schema_version')!r}",
    )
    _require(
        manifest.get("scenario_id") == scenario_id,
        f"manifest scenario_id {manifest.get('scenario_id')!r} != requested {scenario_id!r}",
    )
    return manifest


def _verify_sha(scenario_dir: Path, manifest: dict, filename: str) -> str:
    path = scenario_dir / filename
    _require(path.is_file(), f"missing {filename}")
    text = path.read_text(encoding="utf-8")
    expected = manifest.get("sha256_by_file", {}).get(filename)
    _require(expected is not None, f"manifest sha256_by_file missing {filename}")
    actual = sha256_text(text)
    _require(actual == expected, f"sha256 mismatch for {filename}: {actual} != {expected}")
    return text


def _parse_csv(text: str, header: Sequence[str], filename: str) -> list[dict[str, str]]:
    rows = list(csv.reader(text.splitlines()))
    _require(len(rows) >= 1, f"{filename} has no header")
    _require(rows[0] == list(header), f"{filename} header mismatch: {rows[0]} != {list(header)}")
    out: list[dict[str, str]] = []
    for i, r in enumerate(rows[1:], start=2):
        _require(len(r) == len(header), f"{filename} row {i} width {len(r)} != {len(header)}")
        out.append(dict(zip(header, r, strict=True)))
    return out


def _to_float(value: str, label: str) -> float:
    try:
        f = float(value)
    except ValueError as exc:
        raise CacheValidationError(f"{label} not a float: {value!r}") from exc
    return _require_finite(f, label)


def _to_int_nonneg(value: str, label: str) -> int:
    try:
        n = int(value)
    except ValueError as exc:
        raise CacheValidationError(f"{label} not an int: {value!r}") from exc
    _require(n >= 0, f"{label} must be >= 0, got {n}")
    return n


def _to_enum(enum_cls, value: str, label: str):
    try:
        return enum_cls(value)
    except ValueError as exc:
        raise CacheValidationError(f"{label} invalid {enum_cls.__name__}: {value!r}") from exc


def _validate_source_quality(manifest: dict) -> dict[StreamName, SourceQuality]:
    out: dict[StreamName, SourceQuality] = {}
    raw = manifest.get("source_quality", {})
    _require(isinstance(raw, dict), "manifest source_quality must be an object")
    for name_str, q_str in raw.items():
        name = _to_enum(StreamName, name_str, "source_quality key")
        quality = _to_enum(SourceQuality, q_str, f"source_quality[{name_str}]")
        allowed = stream_spec(name).allowed_source_quality
        _require(
            quality in allowed,
            f"stream {name.value} source_quality {quality.value} not allowed "
            f"(allowed: {[a.value for a in allowed]})",
        )
        out[name] = quality
    return out


def load_dataset(
    cache_root: str | Path,
    scenario_id: str,
    *,
    required_streams: tuple[StreamName, ...] | None = None,
) -> MarketDataset:
    """Load + fail-closed validate a scenario into a ``MarketDataset`` (no network)."""
    scenario_dir = Path(cache_root) / scenario_id
    manifest = _read_manifest(scenario_dir, scenario_id)
    source_quality = _validate_source_quality(manifest)
    venue = str(manifest["venue"])
    timeframe = str(manifest["timeframe"])

    funding_text = _verify_sha(scenario_dir, manifest, _FUNDING_FILE)
    ohlcv_text = _verify_sha(scenario_dir, manifest, _OHLCV_FILE)
    funding_records = _parse_csv(funding_text, _FUNDING_HEADER, _FUNDING_FILE)
    ohlcv_records = _parse_csv(ohlcv_text, _OHLCV_HEADER, _OHLCV_FILE)

    row_counts = manifest.get("row_counts", {})
    _require(
        row_counts.get(_FUNDING_FILE) == len(funding_records),
        f"manifest row_counts[{_FUNDING_FILE}] != actual {len(funding_records)}",
    )
    _require(
        row_counts.get(_OHLCV_FILE) == len(ohlcv_records),
        f"manifest row_counts[{_OHLCV_FILE}] != actual {len(ohlcv_records)}",
    )

    funding = _load_funding(funding_records, venue)
    ohlcv = _load_ohlcv(ohlcv_records, venue, timeframe)

    parsed_symbols = sorted({*funding.keys(), *(k.symbol for k in ohlcv)})
    _require(
        sorted(map(str, manifest.get("symbols", []))) == parsed_symbols,
        f"manifest symbols {manifest.get('symbols')} != parsed {parsed_symbols}",
    )

    _validate_required_streams(funding, ohlcv, parsed_symbols, source_quality,
                               required_streams or DEFAULT_REQUIRED_STREAMS)

    return MarketDataset(
        venue=venue,
        timeframe=timeframe,
        scenario_id=str(manifest["scenario_id"]),
        symbols=tuple(parsed_symbols),
        funding=funding,
        ohlcv=ohlcv,
        source_quality=source_quality,
        train_until_ms=manifest.get("train_until_ms"),
        param_freeze_hash=manifest.get("param_freeze_hash"),
    )


def _validate_required_streams(
    funding: dict[str, tuple[FundingRate, ...]],
    ohlcv: dict[StreamKey, tuple[HistoricalCandle, ...]],
    symbols: list[str],
    source_quality: dict[StreamName, SourceQuality],
    required: tuple[StreamName, ...],
) -> None:
    # 1) required streams must be present per-symbol (presence is the primary failure)
    for name in required:
        for symbol in symbols:
            if name is StreamName.FUNDING_HISTORY:
                _require(
                    bool(funding.get(symbol)),
                    f"required stream missing: {name.value} for {symbol}",
                )
            else:
                mt, pt = _KEY_BY_OHLCV_STREAM[name]
                _require(
                    bool(ohlcv.get(StreamKey(symbol, mt, pt))),
                    f"required stream missing: {name.value} for {symbol}",
                )
    # 2) every present stream must carry a source_quality label (fail-closed provenance)
    present: set[StreamName] = set()
    if funding:
        present.add(StreamName.FUNDING_HISTORY)
    present |= {_ohlcv_stream_name(k) for k in ohlcv}
    for name in present:
        _require(name in source_quality, f"missing source_quality for present stream {name.value}")


def _load_funding(records: list[dict[str, str]], venue: str) -> dict[str, tuple[FundingRate, ...]]:
    by_symbol: dict[str, list[FundingRate]] = {}
    for rec in records:
        _require(rec["venue"] == venue, f"funding row venue {rec['venue']!r} != manifest {venue!r}")
        ih = _to_float(rec["interval_hours"], "funding.interval_hours")
        _require(ih > 0, "funding.interval_hours must be > 0")
        fr = FundingRate(
            symbol=rec["symbol"],
            rate=_to_float(rec["rate"], "funding.rate"),
            interval_hours=ih,
            timestamp=_to_int_nonneg(rec["timestamp_ms"], "funding.timestamp_ms"),
        )
        by_symbol.setdefault(fr.symbol, []).append(fr)
    out: dict[str, tuple[FundingRate, ...]] = {}
    for symbol, series in by_symbol.items():
        _assert_sorted_unique([s.timestamp for s in series], f"funding[{symbol}]")
        out[symbol] = tuple(series)
    return out


def _load_ohlcv(
    records: list[dict[str, str]], venue: str, timeframe: str
) -> dict[StreamKey, tuple[HistoricalCandle, ...]]:
    by_key: dict[StreamKey, list[HistoricalCandle]] = {}
    for rec in records:
        _require(rec["venue"] == venue, f"ohlcv row venue {rec['venue']!r} != manifest {venue!r}")
        _require(
            rec["timeframe"] == timeframe,
            f"ohlcv row timeframe {rec['timeframe']!r} != manifest {timeframe!r}",
        )
        price_type = _to_enum(PriceType, rec["price_type"], "ohlcv.price_type")
        vol_raw = rec["volume"]
        if vol_raw == "":
            volume: float | None = None
        else:
            volume = _to_float(vol_raw, "ohlcv.volume")
            _require(volume >= 0, "ohlcv.volume must be >= 0")
        if price_type is PriceType.TRADE:
            _require(volume is not None, "trade-price OHLCV must carry a volume")
        candle = HistoricalCandle(
            timestamp_ms=_to_int_nonneg(rec["timestamp_ms"], "ohlcv.timestamp_ms"),
            symbol=rec["symbol"],
            venue=rec["venue"],
            market_type=_to_enum(MarketType, rec["market_type"], "ohlcv.market_type"),
            price_type=price_type,
            timeframe=rec["timeframe"],
            open=_to_float(rec["open"], "ohlcv.open"),
            high=_to_float(rec["high"], "ohlcv.high"),
            low=_to_float(rec["low"], "ohlcv.low"),
            close=_to_float(rec["close"], "ohlcv.close"),
            volume=volume,
        )
        key = StreamKey(candle.symbol, candle.market_type, candle.price_type)
        by_key.setdefault(key, []).append(candle)
    out: dict[StreamKey, tuple[HistoricalCandle, ...]] = {}
    for key, series in by_key.items():
        _assert_sorted_unique([c.timestamp_ms for c in series], f"ohlcv[{key}]")
        out[key] = tuple(series)
    return out


def _assert_sorted_unique(timestamps: list[int], label: str) -> None:
    for i in range(1, len(timestamps)):
        _require(timestamps[i] > timestamps[i - 1], f"{label} timestamps not strictly ascending")

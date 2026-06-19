"""G002: aq-cache-v1 write/load round-trip + fail-closed validation."""

import json

import pytest

from ajentix_quant.adapters.base import (
    FundingRate,
    HistoricalCandle,
    MarketType,
    PriceType,
    SourceQuality,
    StreamKey,
    StreamName,
)
from ajentix_quant.data import cache as cache_mod
from ajentix_quant.data.cache import (
    CacheValidationError,
    load_dataset,
    write_cache,
)

VENUE = "bybit"
SYM = "BTC/USDT:USDT"


def _funding(n=4, start=0, step=8 * 3600 * 1000, rate=0.0001):
    return [
        FundingRate(symbol=SYM, rate=rate, interval_hours=8.0, timestamp=start + i * step)
        for i in range(n)
    ]


def _candles(market_type, price_type, n=4, start=0, step=3600 * 1000, with_vol=True):
    return [
        HistoricalCandle(
            timestamp_ms=start + i * step,
            symbol=SYM,
            venue=VENUE,
            market_type=market_type,
            price_type=price_type,
            timeframe="1h",
            open=100.0 + i,
            high=101.0 + i,
            low=99.0 + i,
            close=100.5 + i,
            volume=(10.0 + i) if with_vol else None,
        )
        for i in range(n)
    ]


def _full_dataset_kwargs():
    spot = StreamKey(SYM, MarketType.SPOT, PriceType.TRADE)
    perp_trade = StreamKey(SYM, MarketType.LINEAR_PERP, PriceType.TRADE)
    perp_mark = StreamKey(SYM, MarketType.LINEAR_PERP, PriceType.MARK)
    return {
        "venue": VENUE,
        "timeframe": "1h",
        "funding": {SYM: _funding()},
        "ohlcv": {
            spot: _candles(MarketType.SPOT, PriceType.TRADE),
            perp_trade: _candles(MarketType.LINEAR_PERP, PriceType.TRADE),
            perp_mark: _candles(MarketType.LINEAR_PERP, PriceType.MARK, with_vol=False),
        },
        "source_quality": {
            StreamName.FUNDING_HISTORY: SourceQuality.FIXTURE,
            StreamName.SPOT_TRADE_OHLCV: SourceQuality.FIXTURE,
            StreamName.PERP_TRADE_OHLCV: SourceQuality.FIXTURE,
            StreamName.PERP_MARK_OHLCV: SourceQuality.FIXTURE,
        },
        "train_until_ms": 8 * 3600 * 1000,
        "param_freeze_hash": "abc123",
    }


def test_write_load_round_trip(tmp_path):
    write_cache(tmp_path, "sc1", **_full_dataset_kwargs())
    ds = load_dataset(tmp_path, "sc1")
    assert ds.venue == VENUE
    assert ds.scenario_id == "sc1"
    assert ds.symbols == (SYM,)
    assert ds.train_until_ms == 8 * 3600 * 1000
    assert ds.param_freeze_hash == "abc123"
    assert len(ds.funding[SYM]) == 4
    assert ds.funding[SYM][0].rate == 0.0001
    mark = ds.ohlcv[StreamKey(SYM, MarketType.LINEAR_PERP, PriceType.MARK)]
    assert mark[0].volume is None  # mark klines carry no volume
    perp = ds.ohlcv[StreamKey(SYM, MarketType.LINEAR_PERP, PriceType.TRADE)]
    assert perp[0].volume == 10.0


def test_load_is_deterministic(tmp_path):
    write_cache(tmp_path, "sc1", **_full_dataset_kwargs())
    a = load_dataset(tmp_path, "sc1")
    b = load_dataset(tmp_path, "sc1")
    assert a == b


def test_schema_version_mismatch_fails_closed(tmp_path):
    write_cache(tmp_path, "sc1", **_full_dataset_kwargs())
    mpath = tmp_path / "sc1" / "manifest.json"
    m = json.loads(mpath.read_text())
    m["schema_version"] = "aq-cache-v999"
    mpath.write_text(json.dumps(m))
    with pytest.raises(CacheValidationError, match="schema_version"):
        load_dataset(tmp_path, "sc1")


def test_sha_mismatch_fails_closed(tmp_path):
    write_cache(tmp_path, "sc1", **_full_dataset_kwargs())
    fpath = tmp_path / "sc1" / "funding.csv"
    text = fpath.read_text()
    fpath.write_text(text.replace("0.0001", "0.0009"))  # tamper data, sha now stale
    with pytest.raises(CacheValidationError, match="sha256 mismatch"):
        load_dataset(tmp_path, "sc1")


def test_non_ascending_timestamps_fail_closed(tmp_path):
    write_cache(tmp_path, "sc1", **_full_dataset_kwargs())
    fpath = tmp_path / "sc1" / "funding.csv"
    lines = fpath.read_text().splitlines()
    # set the 2nd data row's timestamp equal to the 1st -> not strictly ascending
    # (keep row count stable so the row_counts guard does not fire first)
    cols1 = lines[1].split(",")
    cols2 = lines[2].split(",")
    cols2[0] = cols1[0]
    lines[2] = ",".join(cols2)
    new_text = "\n".join(lines) + "\n"
    fpath.write_text(new_text)
    mpath = tmp_path / "sc1" / "manifest.json"
    m = json.loads(mpath.read_text())
    m["sha256_by_file"]["funding.csv"] = cache_mod.sha256_text(new_text)
    mpath.write_text(json.dumps(m))
    with pytest.raises(CacheValidationError, match="not strictly ascending"):
        load_dataset(tmp_path, "sc1")


def test_missing_required_stream_fails_closed(tmp_path):
    kwargs = _full_dataset_kwargs()
    # drop the perp MARK stream -> required for default load
    del kwargs["ohlcv"][StreamKey(SYM, MarketType.LINEAR_PERP, PriceType.MARK)]
    del kwargs["source_quality"][StreamName.PERP_MARK_OHLCV]
    write_cache(tmp_path, "sc1", **kwargs)
    with pytest.raises(CacheValidationError, match="required stream missing"):
        load_dataset(tmp_path, "sc1")


def test_disallowed_source_quality_fails_closed(tmp_path):
    kwargs = _full_dataset_kwargs()
    # PROXY is not allowed for the hard PERP_MARK stream
    kwargs["source_quality"][StreamName.PERP_MARK_OHLCV] = SourceQuality.PROXY
    write_cache(tmp_path, "sc1", **kwargs)
    with pytest.raises(CacheValidationError, match="source_quality"):
        load_dataset(tmp_path, "sc1")

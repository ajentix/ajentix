"""G002 red-team cache validation and replay boundary coverage."""

import json

import pytest

from ajentix_quant.adapters.base import (
    FundingRateHistoryRequest,
    MarketType,
    PriceType,
    SourceQuality,
    StreamKey,
    StreamName,
)
from ajentix_quant.data import cache as cache_mod
from ajentix_quant.data.cache import CacheValidationError, load_dataset, sha256_text, write_cache
from ajentix_quant.data.replay import ReplayVenueAdapter
from test_data_cache import SYM, _full_dataset_kwargs

SCENARIO = "g002-redteam"
PERP_MARK = StreamKey(SYM, MarketType.LINEAR_PERP, PriceType.MARK)
PERP_TRADE = StreamKey(SYM, MarketType.LINEAR_PERP, PriceType.TRADE)


def _write_fixture(cache_root, scenario_id=SCENARIO):
    return write_cache(cache_root, scenario_id, **_full_dataset_kwargs())


def _rewrite_manifest_sha(scenario_dir, filename, text):
    manifest_path = scenario_dir / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["sha256_by_file"][filename] = cache_mod.sha256_text(text)
    manifest_path.write_text(
        json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )


def test_tampered_ohlcv_body_fails_sha_validation(tmp_path):
    scenario_dir = _write_fixture(tmp_path)
    ohlcv_path = scenario_dir / "ohlcv.csv"
    original = ohlcv_path.read_text(encoding="utf-8")

    ohlcv_path.write_text(original.replace("100.5", "999.5", 1), encoding="utf-8")

    with pytest.raises(CacheValidationError, match=r"sha256 mismatch for ohlcv\.csv"):
        load_dataset(tmp_path, SCENARIO)


def test_ohlcv_out_of_order_timestamps_fail_after_sha_is_fixed(tmp_path):
    scenario_dir = _write_fixture(tmp_path)
    ohlcv_path = scenario_dir / "ohlcv.csv"
    lines = ohlcv_path.read_text(encoding="utf-8").splitlines()
    header, rows = lines[0], lines[1:]

    first_mark_index = next(
        i for i, row in enumerate(rows) if ",linear_perp,mark," in row and row.startswith("0,")
    )
    assert ",linear_perp,mark," in rows[first_mark_index + 1]
    rows[first_mark_index], rows[first_mark_index + 1] = (
        rows[first_mark_index + 1],
        rows[first_mark_index],
    )
    new_text = "\n".join([header, *rows]) + "\n"
    ohlcv_path.write_text(new_text, encoding="utf-8")
    _rewrite_manifest_sha(scenario_dir, "ohlcv.csv", new_text)

    with pytest.raises(CacheValidationError, match="not strictly ascending"):
        load_dataset(tmp_path, SCENARIO)


def test_wrong_ohlcv_header_fails_after_sha_is_fixed(tmp_path):
    scenario_dir = _write_fixture(tmp_path)
    ohlcv_path = scenario_dir / "ohlcv.csv"
    lines = ohlcv_path.read_text(encoding="utf-8").splitlines()
    fields = lines[0].split(",")
    fields[-1] = "tampered_source"
    lines[0] = ",".join(fields)
    new_text = "\n".join(lines) + "\n"
    ohlcv_path.write_text(new_text, encoding="utf-8")
    _rewrite_manifest_sha(scenario_dir, "ohlcv.csv", new_text)

    with pytest.raises(CacheValidationError, match=r"ohlcv\.csv header mismatch"):
        load_dataset(tmp_path, SCENARIO)


def test_missing_manifest_fails_closed(tmp_path):
    scenario_dir = _write_fixture(tmp_path)
    (scenario_dir / "manifest.json").unlink()

    with pytest.raises(CacheValidationError, match="missing manifest.json"):
        load_dataset(tmp_path, SCENARIO)


@pytest.mark.parametrize(
    "quality",
    [SourceQuality.FROZEN_SNAPSHOT, SourceQuality.VENUE, SourceQuality.FIXTURE],
)
def test_perp_mark_accepts_hard_claim_source_qualities(tmp_path, quality):
    kwargs = _full_dataset_kwargs()
    kwargs["source_quality"] = dict(kwargs["source_quality"])
    kwargs["source_quality"][StreamName.PERP_MARK_OHLCV] = quality

    write_cache(tmp_path, SCENARIO, **kwargs)
    dataset = load_dataset(tmp_path, SCENARIO)

    assert dataset.source_quality[StreamName.PERP_MARK_OHLCV] is quality


@pytest.mark.parametrize("quality", [SourceQuality.PROXY, SourceQuality.ABSENT])
def test_perp_mark_rejects_proxy_and_absent_source_quality(tmp_path, quality):
    kwargs = _full_dataset_kwargs()
    kwargs["source_quality"] = dict(kwargs["source_quality"])
    kwargs["source_quality"][StreamName.PERP_MARK_OHLCV] = quality

    write_cache(tmp_path, SCENARIO, **kwargs)

    with pytest.raises(CacheValidationError, match="perp_mark_ohlcv source_quality"):
        load_dataset(tmp_path, SCENARIO)


def test_explicit_required_index_stream_missing_fails_closed(tmp_path):
    _write_fixture(tmp_path)

    with pytest.raises(CacheValidationError, match="required stream missing.*index_ohlcv"):
        load_dataset(tmp_path, SCENARIO, required_streams=(StreamName.INDEX_OHLCV,))


def test_replay_range_filters_include_boundaries_and_empty_ranges(tmp_path):
    _write_fixture(tmp_path)
    adapter = ReplayVenueAdapter.from_cache(tmp_path, SCENARIO)

    funding_step = 8 * 3600 * 1000
    funding_request = FundingRateHistoryRequest(
        symbol=SYM,
        since_ms=0,
        until_ms=3 * funding_step,
    )
    funding_rows = adapter.fetch_funding_rate_history(funding_request)
    expected = [0, funding_step, 2 * funding_step, 3 * funding_step]
    assert [row.timestamp for row in funding_rows] == expected
    assert (
        adapter.fetch_funding_rate_history(
            FundingRateHistoryRequest(
                symbol=SYM, since_ms=4 * funding_step, until_ms=5 * funding_step
            )
        )
        == []
    )

    ohlcv_step = 3600 * 1000
    ohlcv_rows = adapter.fetch_ohlcv_history(
        SYM,
        "1h",
        0,
        3 * ohlcv_step,
        market_type=MarketType.LINEAR_PERP,
        price_type=PriceType.MARK,
    )
    expected_ts = [0, ohlcv_step, 2 * ohlcv_step, 3 * ohlcv_step]
    assert [row.timestamp_ms for row in ohlcv_rows] == expected_ts
    assert (
        adapter.fetch_ohlcv_history(
            SYM,
            "1h",
            4 * ohlcv_step,
            5 * ohlcv_step,
            market_type=MarketType.LINEAR_PERP,
            price_type=PriceType.MARK,
        )
        == []
    )


def test_direct_market_dataset_replay_adapter_works_without_from_cache(tmp_path):
    _write_fixture(tmp_path)
    dataset = load_dataset(tmp_path, SCENARIO)

    adapter = ReplayVenueAdapter(dataset)

    assert adapter.fetch_funding_rate(SYM).timestamp == 3 * 8 * 3600 * 1000
    assert adapter.fetch_ohlcv(SYM, limit=2)[0].timestamp == 2 * 3600 * 1000


def test_writing_identical_inputs_to_two_dirs_produces_identical_csv_bytes(tmp_path):
    left = _write_fixture(tmp_path / "left")
    right = _write_fixture(tmp_path / "right")

    for filename in ("funding.csv", "ohlcv.csv"):
        left_bytes = (left / filename).read_bytes()
        right_bytes = (right / filename).read_bytes()
        assert left_bytes == right_bytes
        assert sha256_text(left_bytes.decode("utf-8")) == sha256_text(right_bytes.decode("utf-8"))


def test_nullable_mark_volume_and_trade_volume_round_trip_with_exact_types(tmp_path):
    _write_fixture(tmp_path)

    dataset = load_dataset(tmp_path, SCENARIO)

    mark_volumes = [row.volume for row in dataset.ohlcv[PERP_MARK]]
    trade_volumes = [row.volume for row in dataset.ohlcv[PERP_TRADE]]
    assert mark_volumes == [None, None, None, None]
    assert trade_volumes == [10.0, 11.0, 12.0, 13.0]
    assert all(isinstance(volume, float) for volume in trade_volumes)

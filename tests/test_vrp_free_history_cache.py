from __future__ import annotations

import json
from pathlib import Path

import pytest

from ajentix_quant.adapters.base import SourceQuality
from ajentix_quant.data.vrp_free_history_cache import (
    INDEX_PATH_FILE,
    TRADES_FILE,
    VrpFreeHistoryCacheValidationError,
    load_vrp_free_history_cache,
    load_vrp_free_history_manifest,
    sha256_text,
    write_vrp_free_history_cache,
)
from ajentix_quant.research.vrp_free_preregistration import DEFAULT_SCENARIO_ID

FIXTURE_PATH = (
    Path(__file__).parent
    / "fixtures"
    / "vrp_free_history"
    / "eth_option_trades_fixture.jsonl"
)
START_MS = 1725148800000
MID_MS = 1725177600000
END_MS = 1725206400000
STRESS_WINDOWS = [{"id": "fixture_stress", "start_ts_ms": START_MS, "end_ts_ms": MID_MS}]


def _fixture_rows() -> list[dict]:
    return [json.loads(line) for line in FIXTURE_PATH.read_text(encoding="utf-8").splitlines()]


def _write_fixture_cache(root: Path) -> Path:
    return write_vrp_free_history_cache(
        root,
        DEFAULT_SCENARIO_ID,
        raw_rows=_fixture_rows(),
        currency="ETH",
        start_ts_ms=START_MS,
        end_ts_ms=END_MS,
        download_timestamp_ms=START_MS,
        source_ids=["fixture-deribit-history"],
        source_url_ids=["fixture://vrp-free-history/eth-option-trades"],
        source_quality={
            "option_trades": SourceQuality.FIXTURE,
            "underlying_index": SourceQuality.FIXTURE,
        },
        acquisition_tool_version="fixture-vrp-free-history-v1",
        stress_windows=STRESS_WINDOWS,
    )


def test_raw_manifest_reproducibility_and_no_network_loader(tmp_path):
    a = _write_fixture_cache(tmp_path / "a")
    b = _write_fixture_cache(tmp_path / "b")

    manifest_a = (a / "manifest.json").read_text(encoding="utf-8")
    manifest_b = (b / "manifest.json").read_text(encoding="utf-8")
    assert manifest_a == manifest_b
    assert (a / TRADES_FILE).read_text(encoding="utf-8") == (b / TRADES_FILE).read_text(
        encoding="utf-8"
    )
    assert (a / INDEX_PATH_FILE).read_text(encoding="utf-8") == (b / INDEX_PATH_FILE).read_text(
        encoding="utf-8"
    )
    assert sha256_text(manifest_a) == sha256_text(manifest_b)

    loaded = load_vrp_free_history_cache(tmp_path / "a", DEFAULT_SCENARIO_ID)
    assert len(loaded.trades) == 6
    assert len(loaded.index_path) == 3
    assert loaded.manifest["cache_fabricated"] is False


def test_coverage_manifest_math_for_expiry_strike_type_dte_moneyness_fold_grid_and_stress(tmp_path):
    scenario_dir = _write_fixture_cache(tmp_path)
    manifest = load_vrp_free_history_manifest(tmp_path, DEFAULT_SCENARIO_ID)
    coverage = manifest["coverage"]

    assert manifest["schema_version"] == "aq-vrp-free-history-cache-v1"
    assert manifest["row_counts"] == {
        TRADES_FILE: 6,
        INDEX_PATH_FILE: 3,
        "trades": 6,
        "underlying_index_points": 3,
    }
    assert set(coverage["by_expiry"]) == {"2024-09-27T08:00:00Z", "2024-10-25T08:00:00Z"}
    assert coverage["by_strike"]["2400.0"]["trade_count"] == 3
    assert coverage["by_strike"]["2800.0"]["trade_count"] == 3
    assert coverage["by_option_type"]["put"]["trade_count"] == 3
    assert coverage["by_option_type"]["call"]["trade_count"] == 3
    assert coverage["by_dte_bucket"]["dte_21"]["trade_count"] == 3
    assert coverage["by_dte_bucket"]["dte_45"]["trade_count"] == 3
    assert coverage["by_moneyness_bucket"]["near"]["trade_count"] == 3
    assert coverage["by_moneyness_bucket"]["wing"]["trade_count"] == 3
    assert coverage["by_fold"]["F1"]["train_count"] == 6
    assert coverage["by_fold"]["F1"]["test_count"] == 0
    assert coverage["snapshot_grid_8h"]["required_timestamps_ms"] == [START_MS, MID_MS, END_MS]
    assert coverage["snapshot_grid_8h"]["covered_timestamps_ms"] == [START_MS, MID_MS, END_MS]
    assert coverage["snapshot_grid_8h"]["missing_timestamps_ms"] == []
    assert coverage["stress_windows"]["covered_window_ids"] == ["fixture_stress"]
    assert coverage["stress_windows"]["missing_window_ids"] == []
    assert (scenario_dir / "manifest.json").is_file()


@pytest.mark.parametrize("field", ["iv", "index_price", "amount"])
def test_fail_closed_on_missing_required_trade_inputs(tmp_path, field):
    rows = _fixture_rows()
    rows[0].pop(field)

    with pytest.raises(VrpFreeHistoryCacheValidationError, match=field):
        write_vrp_free_history_cache(
            tmp_path,
            DEFAULT_SCENARIO_ID,
            raw_rows=rows,
            currency="ETH",
            start_ts_ms=START_MS,
            end_ts_ms=END_MS,
            download_timestamp_ms=START_MS,
            source_ids=["fixture-deribit-history"],
            source_url_ids=["fixture://vrp-free-history/eth-option-trades"],
            source_quality={"option_trades": SourceQuality.FIXTURE},
            stress_windows=STRESS_WINDOWS,
        )


def test_fail_closed_on_time_gap_beyond_frozen_tolerance(tmp_path):
    with pytest.raises(VrpFreeHistoryCacheValidationError, match="time gap"):
        write_vrp_free_history_cache(
            tmp_path,
            DEFAULT_SCENARIO_ID,
            raw_rows=_fixture_rows(),
            currency="ETH",
            start_ts_ms=START_MS,
            end_ts_ms=END_MS,
            download_timestamp_ms=START_MS,
            source_ids=["fixture-deribit-history"],
            source_url_ids=["fixture://vrp-free-history/eth-option-trades"],
            source_quality={"option_trades": SourceQuality.FIXTURE},
            max_time_gap_ms=1,
            stress_windows=STRESS_WINDOWS,
        )


def test_exact_underlying_index_path_manifest_and_conflict_detection(tmp_path):
    _write_fixture_cache(tmp_path)
    loaded = load_vrp_free_history_cache(tmp_path, DEFAULT_SCENARIO_ID)

    assert [(p.timestamp_ms, p.underlying, p.index_price) for p in loaded.index_path] == [
        (START_MS, "ETH", 2500.0),
        (MID_MS, "ETH", 2520.0),
        (END_MS, "ETH", 2510.0),
    ]
    index_manifest = loaded.manifest["underlying_index_path"]
    assert index_manifest["file"] == INDEX_PATH_FILE
    assert index_manifest["source_field"] == "trade_rows.index_price"
    assert index_manifest["exact"] is True
    assert index_manifest["fabricated"] is False
    assert index_manifest["timestamps_ms"] == [START_MS, MID_MS, END_MS]

    rows = _fixture_rows()
    conflict = dict(rows[1])
    conflict["trade_id"] = "fixture-conflicting-index"
    conflict["index_price"] = 2501.0
    rows.append(conflict)
    with pytest.raises(VrpFreeHistoryCacheValidationError, match="conflicting index_price"):
        write_vrp_free_history_cache(
            tmp_path / "conflict",
            DEFAULT_SCENARIO_ID,
            raw_rows=rows,
            currency="ETH",
            start_ts_ms=START_MS,
            end_ts_ms=END_MS,
            download_timestamp_ms=START_MS,
            source_ids=["fixture-deribit-history"],
            source_url_ids=["fixture://vrp-free-history/eth-option-trades"],
            source_quality={"option_trades": SourceQuality.FIXTURE},
            stress_windows=STRESS_WINDOWS,
        )


def test_loader_fails_closed_on_sha_mismatch_without_network(tmp_path):
    scenario_dir = _write_fixture_cache(tmp_path)
    trades_path = scenario_dir / TRADES_FILE
    trades_path.write_text(trades_path.read_text(encoding="utf-8") + "{}\n", encoding="utf-8")

    with pytest.raises(VrpFreeHistoryCacheValidationError, match="sha256 mismatch"):
        load_vrp_free_history_cache(tmp_path, DEFAULT_SCENARIO_ID)


def test_source_quality_labels_fixture_as_non_authorizing(tmp_path):
    _write_fixture_cache(tmp_path)
    manifest = load_vrp_free_history_manifest(tmp_path, DEFAULT_SCENARIO_ID)

    assert manifest["source_quality"] == {
        "option_trades": SourceQuality.FIXTURE.value,
        "underlying_index": SourceQuality.FIXTURE.value,
    }
    assert manifest["required_column_coverage"] == {
        "trade_id": True,
        "instrument_name": True,
        "timestamp": True,
        "trade_seq": True,
        "price": True,
        "mark_price": True,
        "iv": True,
        "index_price": True,
        "amount": True,
        "direction": True,
        "tick_direction": True,
        "contracts": True,
    }

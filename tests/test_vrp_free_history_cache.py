from __future__ import annotations

import argparse
import json
from pathlib import Path

import pytest
from scripts import collect_deribit_history

from ajentix_quant.adapters.base import SourceQuality
from ajentix_quant.data.vrp_free_history_cache import (
    DEFAULT_MAX_NON_IV_BEARING_RATE,
    INDEX_PATH_FILE,
    TRADES_FILE,
    VrpFreeHistoryCacheValidationError,
    load_vrp_free_history_cache,
    load_vrp_free_history_manifest,
    partition_iv_bearing_trades,
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


def _make_unique_fixture_rows(count: int) -> list[dict]:
    base_rows = _fixture_rows()
    rows: list[dict] = []
    for index in range(count):
        row = dict(base_rows[index % len(base_rows)])
        row["trade_id"] = f"collector-valid-{index:04d}"
        row["trade_seq"] = index + 1
        rows.append(row)
    return rows


def _collector_args(
    tmp_path: Path, max_non_iv_bearing_rate: float = DEFAULT_MAX_NON_IV_BEARING_RATE
):
    return argparse.Namespace(
        currency="ETH",
        start="2024-09-01T00:00:00Z",
        end="2024-09-01T16:00:00Z",
        scenario_id=DEFAULT_SCENARIO_ID,
        raw_source_root="raw",
        reports_dir="reports",
        count=1_000,
        chunk_hours=1.0,
        rate_limit_s=0.0,
        max_non_iv_bearing_rate=max_non_iv_bearing_rate,
    )


def _patch_collector_provider(monkeypatch: pytest.MonkeyPatch, rows: list[dict]) -> None:
    class FakeProvider:
        endpoint = "fixture://deribit-history"

        def __init__(self, rate_limit_s: float):
            self.rate_limit_s = rate_limit_s

        def fetch_option_trades(self, **_kwargs):
            return [dict(row) for row in rows]

    monkeypatch.setattr(collect_deribit_history, "DeribitHistoryTradeProvider", FakeProvider)


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


def test_partition_iv_bearing_trades_excludes_non_priced_prints_and_preserves_order():
    base_rows = _fixture_rows()

    def make_row(
        index: int,
        trade_id: str,
        *,
        iv: object = 12.5,
        mark_price: object = 0.05,
        omit_iv: bool = False,
        omit_mark_price: bool = False,
    ) -> dict:
        row = dict(base_rows[index % len(base_rows)])
        row["trade_id"] = trade_id
        row["trade_seq"] = 9_000 + index
        if omit_iv:
            row.pop("iv", None)
        else:
            row["iv"] = iv
        if omit_mark_price:
            row.pop("mark_price", None)
        else:
            row["mark_price"] = mark_price
        return row

    rows = [
        make_row(0, "valid-a", iv=12.5, mark_price=0.05),
        make_row(1, "iv-zero", iv=0.0, mark_price=0.05),
        make_row(2, "iv-none", iv=None, mark_price=0.05),
        make_row(3, "iv-missing", mark_price=0.05, omit_iv=True),
        make_row(4, "iv-negative", iv=-1.0, mark_price=0.05),
        make_row(5, "iv-nan", iv=float("nan"), mark_price=0.05),
        make_row(6, "mark-zero", iv=12.5, mark_price=0.0),
        make_row(7, "mark-missing", iv=12.5, omit_mark_price=True),
        make_row(8, "valid-b", iv="3.25", mark_price="0.07"),
    ]

    usable_rows, excluded_count = partition_iv_bearing_trades(rows)

    assert [row["trade_id"] for row in usable_rows] == ["valid-a", "valid-b"]
    assert usable_rows[0] is rows[0]
    assert usable_rows[1] is rows[-1]
    assert excluded_count == 7


def test_partition_iv_bearing_trades_fails_loud_on_corruption_behind_no_quote():
    rows = _fixture_rows()
    corrupted = dict(rows[0])
    corrupted["iv"] = 0.0
    corrupted.pop("index_price")
    rows[0] = corrupted

    with pytest.raises(VrpFreeHistoryCacheValidationError, match="index_price"):
        partition_iv_bearing_trades(rows)


def test_partition_iv_bearing_trades_excludes_structurally_sound_no_quote_print():
    row = dict(_fixture_rows()[0])
    row["iv"] = 0.0
    row["mark_price"] = 0.0

    usable_rows, excluded_count = partition_iv_bearing_trades([row])

    assert usable_rows == ()
    assert excluded_count == 1


@pytest.mark.parametrize(("field", "value"), [("iv", True), ("mark_price", False)])
def test_boolean_quote_values_are_not_excluded_and_fail_strict_parser(tmp_path, field, value):
    row = dict(_fixture_rows()[0])
    row["trade_id"] = f"fixture-boolean-{field}"
    row[field] = value

    usable_rows, excluded_count = partition_iv_bearing_trades([row])

    assert usable_rows == (row,)
    assert excluded_count == 0
    with pytest.raises(VrpFreeHistoryCacheValidationError, match=field):
        write_vrp_free_history_cache(
            tmp_path,
            DEFAULT_SCENARIO_ID,
            raw_rows=usable_rows,
            currency="ETH",
            start_ts_ms=START_MS,
            end_ts_ms=END_MS,
            download_timestamp_ms=START_MS,
            source_ids=["fixture-deribit-history"],
            source_url_ids=["fixture://vrp-free-history/eth-option-trades"],
            source_quality={"option_trades": SourceQuality.FIXTURE},
            stress_windows=STRESS_WINDOWS,
        )


def test_collector_excludes_non_iv_rows_below_threshold_and_reports_accounting(
    tmp_path, monkeypatch
):
    monkeypatch.delenv("CI", raising=False)
    rows = _make_unique_fixture_rows(10)
    non_iv_row = dict(rows[0])
    non_iv_row["trade_id"] = "collector-non-iv-0001"
    non_iv_row["trade_seq"] = 10_001
    non_iv_row["iv"] = 0.0
    rows.insert(3, non_iv_row)
    _patch_collector_provider(monkeypatch, rows)

    payload = collect_deribit_history._collect(_collector_args(tmp_path), tmp_path)

    assert payload["status"] == collect_deribit_history.STATUS_POPULATED
    assert payload["total_fetched"] == 11
    assert payload["non_iv_bearing_excluded"] == 1
    assert payload["non_iv_bearing_rate"] == pytest.approx(1 / 11)
    assert payload["max_non_iv_bearing_rate"] == DEFAULT_MAX_NON_IV_BEARING_RATE
    assert payload["trade_rows"] == 10
    loaded = load_vrp_free_history_cache(tmp_path / "raw", DEFAULT_SCENARIO_ID)
    assert len(loaded.raw_rows) == 10
    assert all(float(row["iv"]) > 0.0 for row in loaded.raw_rows)


def test_collector_fails_closed_when_non_iv_rows_exceed_threshold(tmp_path, monkeypatch):
    monkeypatch.delenv("CI", raising=False)
    rows = _make_unique_fixture_rows(6)
    non_iv_row = dict(rows[0])
    non_iv_row["trade_id"] = "collector-non-iv-0001"
    non_iv_row["trade_seq"] = 10_001
    non_iv_row["iv"] = None
    rows.append(non_iv_row)
    _patch_collector_provider(monkeypatch, rows)

    payload = collect_deribit_history._collect(_collector_args(tmp_path), tmp_path)

    assert payload["status"] == collect_deribit_history.STATUS_DATA_BLOCKER
    assert payload["reason_codes"] == ["EXCESSIVE_NON_IV_BEARING_ROWS"]
    assert payload["total_fetched"] == 7
    assert payload["non_iv_bearing_excluded"] == 1
    assert payload["non_iv_bearing_rate"] == pytest.approx(1 / 7)
    assert payload["max_non_iv_bearing_rate"] == DEFAULT_MAX_NON_IV_BEARING_RATE
    assert not (tmp_path / "raw" / DEFAULT_SCENARIO_ID).exists()


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


def test_warmup_cache_load_round_trips_with_later_coverage_start(tmp_path):
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
        source_quality={
            "option_trades": SourceQuality.FIXTURE,
            "underlying_index": SourceQuality.FIXTURE,
        },
        coverage_start_ts_ms=MID_MS,
        stress_windows=STRESS_WINDOWS,
    )

    loaded = load_vrp_free_history_cache(tmp_path, DEFAULT_SCENARIO_ID)

    assert loaded.manifest["date_range"] == {
        "start_ts_ms": START_MS,
        "end_ts_ms": END_MS,
        "coverage_start_ts_ms": MID_MS,
    }
    assert loaded.manifest["coverage"]["snapshot_grid_8h"]["required_timestamps_ms"] == [
        MID_MS,
        END_MS,
    ]


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
    assert (
        index_manifest["same_ms_collision_reconciliation"]
        == "lower_median_real_observed_reading"
    )
    assert index_manifest["same_ms_max_rel_spread_threshold"] == pytest.approx(0.05)
    assert index_manifest["timestamps_ms"] == [START_MS, MID_MS, END_MS]

    rows = _fixture_rows()
    conflict = dict(rows[1])
    conflict["trade_id"] = "fixture-conflicting-index"
    conflict["index_price"] = 3000.0
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

def test_sub_ms_index_noise_within_tolerance_is_reconciled(tmp_path):
    # Two real same-millisecond trades can report marginally different index_price as
    # the Deribit index ticks sub-millisecond. A tiny relative diff (within tolerance)
    # is deterministically reconciled to the first stable-sorted observation, not
    # failed-closed and never fabricated.
    rows = _fixture_rows()
    noisy = dict(rows[1])
    noisy["trade_id"] = "fixture-subms-index-noise"
    noisy["trade_seq"] = int(rows[1]["trade_seq"]) + 1000
    noisy["index_price"] = 2500.5  # +0.02% vs 2500.0 at START_MS, within tolerance
    rows.append(noisy)

    write_vrp_free_history_cache(
        tmp_path / "noise",
        DEFAULT_SCENARIO_ID,
        raw_rows=rows,
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
        stress_windows=STRESS_WINDOWS,
    )
    loaded = load_vrp_free_history_cache(tmp_path / "noise", DEFAULT_SCENARIO_ID)
    start_point = next(p for p in loaded.index_path if p.timestamp_ms == START_MS)
    assert start_point.index_price == 2500.0


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

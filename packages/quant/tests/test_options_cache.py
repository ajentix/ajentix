from __future__ import annotations

import csv
import io
import json
import shutil
from pathlib import Path

import pytest

from ajentix_quant.adapters.base import SourceQuality
from ajentix_quant.data.options_cache import (
    OPTION_CHAIN_FILE,
    OptionsCacheValidationError,
    load_normalized_cache,
    load_normalized_manifest,
    load_raw_source_manifest,
    sha256_text,
    write_normalized_cache,
    write_raw_source_manifest,
)

SCENARIO = "tiny_eth_options_v1"
FIXTURE_ROOT = Path(__file__).parent / "fixtures" / "deribit_options"
RAW_FIXTURE_ROOT = FIXTURE_ROOT / "raw"
NORMALIZED_FIXTURE_ROOT = FIXTURE_ROOT / "normalized"


def _manifest_text(manifest: dict) -> str:
    return json.dumps(manifest, indent=2, sort_keys=True) + "\n"


def _copy_normalized_fixture(tmp_path: Path) -> Path:
    root = tmp_path / "normalized"
    shutil.copytree(NORMALIZED_FIXTURE_ROOT / SCENARIO, root / SCENARIO)
    return root


def _rewrite_manifest_for_option_chain(root: Path) -> None:
    scenario_dir = root / SCENARIO
    csv_text = (scenario_dir / OPTION_CHAIN_FILE).read_text(encoding="utf-8")
    manifest_path = scenario_dir / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["sha256_by_file"][OPTION_CHAIN_FILE] = sha256_text(csv_text)
    manifest["file_sizes"][OPTION_CHAIN_FILE] = len(csv_text.encode("utf-8"))
    manifest_path.write_text(_manifest_text(manifest), encoding="utf-8")


def _rewrite_manifest(root: Path, update) -> None:
    manifest_path = root / SCENARIO / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    update(manifest)
    manifest_path.write_text(_manifest_text(manifest), encoding="utf-8")


def _write_csv_rows(path: Path, rows: list[list[str]]) -> None:
    buf = io.StringIO()
    writer = csv.writer(buf, lineterminator="\n")
    writer.writerows(rows)
    path.write_text(buf.getvalue(), encoding="utf-8")


def test_fixture_loads_as_non_authorizing_option_chain_snapshots():
    raw_manifest = load_raw_source_manifest(RAW_FIXTURE_ROOT, SCENARIO)
    normalized_manifest = load_normalized_manifest(NORMALIZED_FIXTURE_ROOT, SCENARIO)
    snapshots = load_normalized_cache(NORMALIZED_FIXTURE_ROOT, SCENARIO)

    assert raw_manifest["manifest_kind"] == "raw_source"
    assert normalized_manifest["manifest_kind"] == "normalized"
    assert normalized_manifest["source_quality"]["option_chain"] == SourceQuality.FIXTURE.value
    assert "option_chain" in normalized_manifest["non_authorizing_source_quality_keys"]
    assert len(snapshots) == 4
    assert snapshots[0].schema_version == "aq-options-cache-v1"
    assert snapshots[0].source_quality_map["option_chain"] is SourceQuality.FIXTURE
    assert all(snapshot.legs for snapshot in snapshots)


def test_raw_and_normalized_manifests_are_reproducible_for_identical_inputs(tmp_path):
    raw_text = "{\"fixture\":true,\"row\":1}\n"
    raw_kwargs = {
        "source_files": {"raw_options_chain.jsonl": raw_text},
        "source_ids": ["fixture-deribit-options"],
        "currency": "ETH",
        "start_ts_ms": 1,
        "end_ts_ms": 2,
        "download_timestamp_ms": 1,
        "source_uri_ids": ["fixture://deribit/options/repro"],
        "license_budget_note": "fixture only",
        "acquisition_tool_version": "fixture-generator-v1",
        "source_quality": {"option_chain": SourceQuality.FIXTURE},
    }
    write_raw_source_manifest(tmp_path / "raw_a", SCENARIO, **raw_kwargs)
    write_raw_source_manifest(tmp_path / "raw_b", SCENARIO, **raw_kwargs)
    raw_a = (tmp_path / "raw_a" / SCENARIO / "manifest.json").read_text(encoding="utf-8")
    raw_b = (tmp_path / "raw_b" / SCENARIO / "manifest.json").read_text(encoding="utf-8")
    assert raw_a == raw_b
    assert sha256_text(raw_a) == sha256_text(raw_b)

    snapshots = load_normalized_cache(NORMALIZED_FIXTURE_ROOT, SCENARIO)
    write_normalized_cache(
        tmp_path / "norm_a",
        SCENARIO,
        snapshots=snapshots,
        source_ids=["fixture-deribit-options"],
        raw_manifest_sha256=sha256_text(raw_a),
        fold_coverage_timestamps_ms={"fixture_fold": [1717200000000, 1717203600000]},
        stress_selector_input_coverage={
            "underlying_index_timestamps_ms": [1717200000000, 1717203600000]
        },
    )
    write_normalized_cache(
        tmp_path / "norm_b",
        SCENARIO,
        snapshots=snapshots,
        source_ids=["fixture-deribit-options"],
        raw_manifest_sha256=sha256_text(raw_a),
        fold_coverage_timestamps_ms={"fixture_fold": [1717200000000, 1717203600000]},
        stress_selector_input_coverage={
            "underlying_index_timestamps_ms": [1717200000000, 1717203600000]
        },
    )
    norm_a = (tmp_path / "norm_a" / SCENARIO / "manifest.json").read_text(encoding="utf-8")
    norm_b = (tmp_path / "norm_b" / SCENARIO / "manifest.json").read_text(encoding="utf-8")
    csv_a = (tmp_path / "norm_a" / SCENARIO / OPTION_CHAIN_FILE).read_text(encoding="utf-8")
    csv_b = (tmp_path / "norm_b" / SCENARIO / OPTION_CHAIN_FILE).read_text(encoding="utf-8")
    assert norm_a == norm_b
    assert csv_a == csv_b
    assert sha256_text(norm_a) == sha256_text(norm_b)


def test_schema_version_mismatch_fails_closed(tmp_path):
    root = _copy_normalized_fixture(tmp_path)
    _rewrite_manifest(root, lambda manifest: manifest.__setitem__("schema_version", "broken"))

    with pytest.raises(OptionsCacheValidationError, match="schema_version"):
        load_normalized_cache(root, SCENARIO)


def test_sha_mismatch_fails_closed(tmp_path):
    root = _copy_normalized_fixture(tmp_path)
    path = root / SCENARIO / OPTION_CHAIN_FILE
    path.write_text(path.read_text(encoding="utf-8").replace("0.041", "0.040"), encoding="utf-8")

    with pytest.raises(OptionsCacheValidationError, match="sha256 mismatch"):
        load_normalized_cache(root, SCENARIO)


def test_unsorted_or_duplicate_coverage_timestamps_fail_closed(tmp_path):
    root = _copy_normalized_fixture(tmp_path)

    def reverse_coverage(manifest: dict) -> None:
        manifest["coverage_timestamps_ms"]["ETH"] = [1717203600000, 1717200000000]

    _rewrite_manifest(root, reverse_coverage)

    with pytest.raises(OptionsCacheValidationError, match="coverage_timestamps_ms"):
        load_normalized_cache(root, SCENARIO)


def test_option_chain_rows_must_be_strictly_sorted(tmp_path):
    root = _copy_normalized_fixture(tmp_path)
    scenario_dir = root / SCENARIO
    path = scenario_dir / OPTION_CHAIN_FILE
    rows = list(csv.reader(path.read_text(encoding="utf-8").splitlines()))
    rows[1], rows[2] = rows[2], rows[1]
    _write_csv_rows(path, rows)
    _rewrite_manifest_for_option_chain(root)

    with pytest.raises(OptionsCacheValidationError, match="not strictly ascending"):
        load_normalized_cache(root, SCENARIO)


def test_bid_greater_than_ask_fails_closed_even_with_updated_sha(tmp_path):
    root = _copy_normalized_fixture(tmp_path)
    scenario_dir = root / SCENARIO
    path = scenario_dir / OPTION_CHAIN_FILE
    rows = list(csv.reader(path.read_text(encoding="utf-8").splitlines()))
    bid_idx = rows[0].index("bid_price")
    rows[1][bid_idx] = "0.099"
    _write_csv_rows(path, rows)
    _rewrite_manifest_for_option_chain(root)

    with pytest.raises(OptionsCacheValidationError, match="bid > ask"):
        load_normalized_cache(root, SCENARIO)


def test_non_positive_amount_fails_closed_even_with_updated_sha(tmp_path):
    root = _copy_normalized_fixture(tmp_path)
    scenario_dir = root / SCENARIO
    path = scenario_dir / OPTION_CHAIN_FILE
    rows = list(csv.reader(path.read_text(encoding="utf-8").splitlines()))
    amount_idx = rows[0].index("bid_amount")
    rows[1][amount_idx] = "0.0"
    _write_csv_rows(path, rows)
    _rewrite_manifest_for_option_chain(root)

    with pytest.raises(OptionsCacheValidationError, match="bid_amount must be positive"):
        load_normalized_cache(root, SCENARIO)


def test_missing_expiry_lifecycle_fails_closed(tmp_path):
    root = _copy_normalized_fixture(tmp_path)
    _rewrite_manifest(root, lambda manifest: manifest.__setitem__("expiry_lifecycle", []))

    with pytest.raises(OptionsCacheValidationError, match="expiry_lifecycle"):
        load_normalized_cache(root, SCENARIO)


def test_invalid_source_quality_label_fails_closed(tmp_path):
    root = _copy_normalized_fixture(tmp_path)

    def corrupt_source_quality(manifest: dict) -> None:
        manifest["source_quality"]["option_chain"] = "mystery"

    _rewrite_manifest(root, corrupt_source_quality)

    with pytest.raises(OptionsCacheValidationError, match="SourceQuality"):
        load_normalized_cache(root, SCENARIO)

from __future__ import annotations

import csv
import hashlib
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest

from ajentix_quant.adapters.base import SourceQuality
from ajentix_quant.data.tardis_free_spread_calibration import write_spread_calibration_cache
from ajentix_quant.data.vrp_free_history_cache import (
    load_vrp_free_history_cache,
    write_vrp_free_history_cache,
)
from ajentix_quant.options.iv_surface_reconstruction import (
    reconstruct_from_history_dataset,
    write_reconstructed_chain_cache,
)
from ajentix_quant.research import vrp_free_preregistration as vrp

REPO_ROOT = Path(__file__).resolve().parents[1]
SCENARIO_ID = vrp.DEFAULT_SCENARIO_ID
HISTORY_FIXTURE = (
    REPO_ROOT / "tests" / "fixtures" / "vrp_free_history" / "eth_option_trades_fixture.jsonl"
)
TARDIS_FIXTURE = (
    REPO_ROOT / "tests" / "fixtures" / "tardis_free_spread_samples" / "options_chain_fixture.csv"
)
START_MS = 1725148800000
MID_MS = 1725177600000
END_MS = 1725206400000
STRESS_WINDOWS = ({"id": "fixture_stress", "start_ts_ms": START_MS, "end_ts_ms": MID_MS},)


@dataclass(frozen=True)
class RealManifestChain:
    raw_root: Path
    raw_manifest_path: Path
    reconstructed_root: Path
    reconstructed_manifest_path: Path
    tardis_sample_root: Path
    tardis_sample_manifest_path: Path
    spread_calibration_root: Path
    spread_calibration_manifest_path: Path
    precalibration_artifact_path: Path
    stress_selector_input_path: Path
    out_dir: Path


def _sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _sha256_file(path: Path) -> str:
    return _sha256_text(path.read_text(encoding="utf-8"))


def _write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def _fixture_rows() -> list[dict[str, Any]]:
    return [json.loads(line) for line in HISTORY_FIXTURE.read_text(encoding="utf-8").splitlines()]


def _write_raw_history_cache(root: Path) -> tuple[Path, Path]:
    raw_root = root / "raw-history"
    scenario_dir = write_vrp_free_history_cache(
        raw_root,
        SCENARIO_ID,
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
    return raw_root, scenario_dir / "manifest.json"


def _write_reconstructed_cache(
    root: Path, raw_root: Path, raw_manifest_path: Path
) -> tuple[Path, Path]:
    reconstructed_root = root / "reconstructed-chains"
    dataset = load_vrp_free_history_cache(raw_root, SCENARIO_ID)
    reconstructed_chains = reconstruct_from_history_dataset(dataset)
    scenario_dir = write_reconstructed_chain_cache(
        reconstructed_root,
        SCENARIO_ID,
        reconstructed_chains=reconstructed_chains,
        raw_manifest_sha256=_sha256_file(raw_manifest_path),
    )
    return reconstructed_root, scenario_dir / "manifest.json"


def _write_tardis_sample_manifest(root: Path) -> tuple[Path, Path]:
    tardis_sample_root = root / "tardis-samples"
    manifest_path = tardis_sample_root / SCENARIO_ID / "manifest.json"
    text = TARDIS_FIXTURE.read_text(encoding="utf-8")
    rows = list(csv.DictReader(text.splitlines()))
    _write_json(
        manifest_path,
        {
            "schema_version": "aq-vrp-free-tardis-sample-manifest-v1",
            "manifest_kind": "tardis_sample",
            "scenario_id": SCENARIO_ID,
            "exchange": "deribit",
            "source_files": [
                {
                    "filename": TARDIS_FIXTURE.name,
                    "sha256": _sha256_text(text),
                    "file_size": len(text.encode("utf-8")),
                    "row_count": len(rows),
                }
            ],
            "source_uri_ids": ["fixture://tardis-free-spread-samples/options_chain_fixture.csv"],
            "sample_months_observed": sorted({row["sample_month"] for row in rows}),
            "cache_fabricated": False,
            "no_fabrication_policy": "Local committed Tardis FREE fixture rows only.",
        },
    )
    return tardis_sample_root, manifest_path


def _write_spread_calibration_manifest(root: Path) -> tuple[Path, Path]:
    spread_calibration_root = root / "spread-calibration"
    scenario_dir = write_spread_calibration_cache(
        spread_calibration_root,
        SCENARIO_ID,
        csv_paths=[TARDIS_FIXTURE],
        precalibration_config_sha256=vrp.precalibration_config_sha256(),
    )
    return spread_calibration_root, scenario_dir / "manifest.json"


def _write_precalibration_artifact(root: Path) -> Path:
    return vrp.write_precalibration_artifact(root, out_dir="precalibration")


def _write_stress_selector_input(root: Path) -> Path:
    path = root / "stress-selector-input.json"
    _write_json(
        path,
        {
            "schema_version": "vrp-free-stress-selector-input-v1",
            "scenario_id": SCENARIO_ID,
            "stress_windows": list(STRESS_WINDOWS),
        },
    )
    return path


def _build_real_manifest_chain(root: Path) -> RealManifestChain:
    raw_root, raw_manifest_path = _write_raw_history_cache(root)
    reconstructed_root, reconstructed_manifest_path = _write_reconstructed_cache(
        root,
        raw_root,
        raw_manifest_path,
    )
    tardis_sample_root, tardis_sample_manifest_path = _write_tardis_sample_manifest(root)
    spread_calibration_root, spread_calibration_manifest_path = (
        _write_spread_calibration_manifest(root)
    )
    precalibration_artifact_path = _write_precalibration_artifact(root)
    stress_selector_input_path = _write_stress_selector_input(root)
    return RealManifestChain(
        raw_root=raw_root,
        raw_manifest_path=raw_manifest_path,
        reconstructed_root=reconstructed_root,
        reconstructed_manifest_path=reconstructed_manifest_path,
        tardis_sample_root=tardis_sample_root,
        tardis_sample_manifest_path=tardis_sample_manifest_path,
        spread_calibration_root=spread_calibration_root,
        spread_calibration_manifest_path=spread_calibration_manifest_path,
        precalibration_artifact_path=precalibration_artifact_path,
        stress_selector_input_path=stress_selector_input_path,
        out_dir=root / "preregistration-out",
    )


def _emit_preregistration(chain: RealManifestChain, *, out_dir: Path | None = None) -> Path:
    return vrp.write_preregistration(
        REPO_ROOT,
        raw_manifest_path=chain.raw_manifest_path,
        reconstructed_manifest_path=chain.reconstructed_manifest_path,
        tardis_sample_manifest_path=chain.tardis_sample_manifest_path,
        spread_calibration_manifest_path=chain.spread_calibration_manifest_path,
        precalibration_artifact_path=chain.precalibration_artifact_path,
        stress_selector_input_path=chain.stress_selector_input_path,
        out_dir=(out_dir or chain.out_dir).as_posix(),
    )


def _assert_no_artifact_written(out_dir: Path) -> None:
    assert not out_dir.exists() or list(out_dir.glob("*.json")) == []


def test_full_real_manifest_freeze_emits_and_verifies_valid(tmp_path):
    chain = _build_real_manifest_chain(tmp_path)

    artifact_path = _emit_preregistration(chain)
    loaded = vrp.load_preregistration(artifact_path)
    result = vrp.verify_preregistration(loaded, REPO_ROOT)

    assert artifact_path.is_file()
    assert artifact_path.parent == chain.out_dir
    assert loaded["run_id"].startswith("vrp-free-")
    assert result.valid is True
    assert result.run_status == "valid"
    assert result.mismatches == ()
    assert loaded["raw_source_manifest_sha256"][SCENARIO_ID] != vrp.MISSING_SHA
    assert loaded["reconstructed_cache_manifest_sha256"][SCENARIO_ID] != vrp.MISSING_SHA
    assert loaded["tardis_sample_manifest_sha256"][SCENARIO_ID] != vrp.MISSING_SHA
    assert loaded["spread_calibration_manifest_sha256"][SCENARIO_ID] != vrp.MISSING_SHA
    assert loaded["precalibration_artifact_sha256"] != vrp.MISSING_SHA
    assert (
        loaded["spread_calibration_precalibration_config_sha256"][SCENARIO_ID]
        == vrp.precalibration_config_sha256()
    )


def test_emit_refuses_when_a_required_manifest_is_missing(tmp_path):
    missing_cases = (
        ("reconstructed", "reconstructed-cache", "reconstructed_manifest_path"),
        ("spread-calibration", "spread-calibration", "spread_calibration_manifest_path"),
    )
    for case_name, error_label, manifest_attr in missing_cases:
        case_root = tmp_path / case_name
        chain = _build_real_manifest_chain(case_root)
        getattr(chain, manifest_attr).unlink()
        out_dir = case_root / "out-after-missing-manifest"

        with pytest.raises(vrp.PreregistrationError, match=error_label):
            _emit_preregistration(chain, out_dir=out_dir)

        _assert_no_artifact_written(out_dir)


def test_calibration_output_must_carry_unchanged_precalibration_hash(tmp_path):
    chain = _build_real_manifest_chain(tmp_path)
    manifest = json.loads(chain.spread_calibration_manifest_path.read_text(encoding="utf-8"))
    assert manifest["precalibration_config_sha256"] == vrp.precalibration_config_sha256()
    manifest["precalibration_config_sha256"] = "0" * 64
    _write_json(chain.spread_calibration_manifest_path, manifest)
    out_dir = tmp_path / "out-after-mutated-calibration"

    with pytest.raises(vrp.PreregistrationError, match="pre-calibration config"):
        _emit_preregistration(chain, out_dir=out_dir)

    _assert_no_artifact_written(out_dir)


def test_post_freeze_source_drift_invalidates_emitted_artifact(tmp_path):
    chain = _build_real_manifest_chain(tmp_path)
    artifact_path = _emit_preregistration(chain)
    loaded = vrp.load_preregistration(artifact_path)
    assert vrp.verify_preregistration(loaded, REPO_ROOT).valid is True

    loaded["raw_source_manifest_sha256"][SCENARIO_ID] = "0" * 64
    result = vrp.verify_preregistration(loaded, REPO_ROOT)

    assert result.valid is False
    assert result.run_status == "invalid"
    assert any("raw-source manifest drift" in mismatch for mismatch in result.mismatches)


def test_economics_refusal_contract_on_missing_artifact(tmp_path):
    with pytest.raises(
        vrp.PreregistrationError, match="missing VRP-free pre-registration artifact"
    ):
        vrp.load_preregistration(tmp_path / "missing-preregistration.json")

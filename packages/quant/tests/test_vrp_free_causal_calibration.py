from __future__ import annotations

import csv
import importlib.util
import io
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from ajentix_quant.data.tardis_free_spread_calibration import (
    STATUS_RESOLVED,
    load_spread_calibration_manifest,
    load_tardis_free_structure_samples,
    resolve_spread_quantiles,
)
from ajentix_quant.research.vrp_free_preregistration import (
    DEFAULT_SCENARIO_ID,
    PreregistrationError,
    build_precalibration_artifact,
    precalibration_config_sha256,
    write_precalibration_artifact,
)

FIXTURE = (
    Path(__file__).parent
    / "fixtures"
    / "tardis_free_spread_samples"
    / "options_chain_fixture.csv"
)
SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "calibrate_vrp_free_spreads.py"


def _load_cli() -> Any:
    spec = importlib.util.spec_from_file_location("calibrate_vrp_free_spreads_under_test", SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _write_future_augmented_fixture(tmp_path: Path) -> Path:
    rows = list(csv.reader(FIXTURE.read_text(encoding="utf-8").splitlines()))
    header = rows[0]
    out_rows = rows[1:]
    ts = datetime(2025, 4, 1, tzinfo=UTC)
    exp = ts + timedelta(days=30)
    for j in range(1, 31):
        for role, strike, bid in (("long", 1900.0, 0.100), ("short", 1940.0, 0.120)):
            row = dict.fromkeys(header, "")
            row.update(
                {
                    "sample_timestamp": ts.strftime("%Y-%m-%dT%H:%M:%SZ"),
                    "sample_month": "2025-04-01",
                    "underlying": "ETH",
                    "instrument_name": f"ETH-{exp.strftime('%d%b%y').upper()}-{int(strike)}-P",
                    "expiration": exp.strftime("%Y-%m-%dT%H:%M:%SZ"),
                    "strike": repr(strike),
                    "option_type": "put",
                    "bid_price": repr(bid),
                    "ask_price": repr(bid + 1.0),
                    "index_price": "2000.0",
                    "contract_multiplier": "1.0",
                    "quantity": "1.0",
                    "trailing_30d_rv_annualized": "0.50",
                    "abs_24h_return": "0.02",
                    "structure_sample_id": f"future-put-near-normal-{j:02d}",
                    "leg_role": role,
                }
            )
            out_rows.append([row[column] for column in header])
    buf = io.StringIO()
    writer = csv.writer(buf, lineterminator="\n")
    writer.writerow(header)
    writer.writerows(out_rows)
    path = tmp_path / "future_augmented_options_chain.csv"
    path.write_text(buf.getvalue(), encoding="utf-8")
    return path


def test_fold_calibration_excludes_samples_after_train_end(tmp_path):
    path = _write_future_augmented_fixture(tmp_path)
    samples = load_tardis_free_structure_samples([path])

    f1 = resolve_spread_quantiles(
        samples,
        option_type="put",
        dte_bucket="dte_30",
        moneyness_bucket="near",
        regime_label="normal",
        fold_id="F1",
    )
    f2 = resolve_spread_quantiles(
        samples,
        option_type="put",
        dte_bucket="dte_30",
        moneyness_bucket="near",
        regime_label="normal",
        fold_id="F2",
    )

    assert f1.status == STATUS_RESOLVED
    assert f1.sample_count == 30
    assert f1.sample_months[-1] == "2025-01-01"
    assert f1.p75_round_trip_structure_spread_usd == pytest.approx(92.0)
    assert f1.fold_train_end == "2025-03-01T00:00:00Z"

    assert f2.status == STATUS_RESOLVED
    assert f2.sample_count == 60
    assert "2025-04-01" in f2.sample_months
    assert f2.p75_round_trip_structure_spread_usd > 1000.0


def test_calibration_cli_refuses_missing_precalibration_artifact(tmp_path):
    cli = _load_cli()

    with pytest.raises(PreregistrationError, match="missing VRP-free pre-calibration artifact"):
        cli.main(
            [
                "--input-csv",
                str(FIXTURE),
                "--precalibration-artifact",
                str(tmp_path / "missing.json"),
                "--spread-calibration-root",
                str(tmp_path / "cache"),
            ]
        )


def test_calibration_cli_refuses_mismatched_precalibration_hash(tmp_path):
    cli = _load_cli()
    artifact = build_precalibration_artifact()
    artifact["precalibration_config_sha256"] = "bad"
    artifact_path = tmp_path / "bad-precalibration.json"
    artifact_path.write_text(
        json.dumps(artifact, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )

    with pytest.raises(PreregistrationError, match="config hash drift"):
        cli.main(
            [
                "--input-csv",
                str(FIXTURE),
                "--precalibration-artifact",
                str(artifact_path),
                "--spread-calibration-root",
                str(tmp_path / "cache"),
            ]
        )


def test_emitted_manifest_carries_matching_precalibration_config_hash(tmp_path):
    cli = _load_cli()
    artifact_path = write_precalibration_artifact(tmp_path, out_dir="pre")
    cache_root = tmp_path / "spread-cache"

    assert (
        cli.main(
            [
                "--input-csv",
                str(FIXTURE),
                "--precalibration-artifact",
                str(artifact_path),
                "--spread-calibration-root",
                str(cache_root),
                "--json",
            ]
        )
        == 0
    )

    manifest = load_spread_calibration_manifest(cache_root, DEFAULT_SCENARIO_ID)
    assert manifest["precalibration_config_sha256"] == precalibration_config_sha256()
    assert manifest["schema_version"] == "aq-vrp-free-spread-calibration-v1"
    assert manifest["manifest_kind"] == "spread_calibration"
    assert manifest["spread_source_quality"] == "calibrated_spread_sample"
    assert manifest["free_source_quality"] == "reconstructed_from_real_trade_iv"
    assert manifest["non_authorizing_reason"] == "reconstructed_from_real_trade_iv"
    assert manifest["authorizing"] is False
    assert manifest["capital_go_allowed"] is False
    assert manifest["cache_fabricated"] is False
    assert manifest["row_counts"]["structure_spread_samples"] == 32

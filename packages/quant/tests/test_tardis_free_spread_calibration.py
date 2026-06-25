from __future__ import annotations

import csv
import io
from pathlib import Path

import pytest

from ajentix_quant.data.tardis_free_spread_calibration import (
    STATUS_INCONCLUSIVE,
    STATUS_RESOLVED,
    TardisFreeSpreadCalibrationError,
    abs_log_moneyness_to_bucket,
    dte_days_to_bucket,
    load_tardis_free_structure_samples,
    regime_label,
    resolve_spread_quantiles,
)

FIXTURE = (
    Path(__file__).parent
    / "fixtures"
    / "tardis_free_spread_samples"
    / "options_chain_fixture.csv"
)


def _samples():
    return load_tardis_free_structure_samples([FIXTURE])


def test_bucket_and_regime_assignment_helpers_use_frozen_taxonomy():
    assert dte_days_to_bucket(14.0) == "dte_21"
    assert dte_days_to_bucket(27.0) == "dte_21"
    assert dte_days_to_bucket(28.0) == "dte_30"
    assert dte_days_to_bucket(39.0) == "dte_45"
    assert dte_days_to_bucket(61.0) == "out_of_grid"

    assert abs_log_moneyness_to_bucket(0.0) == "atm"
    assert abs_log_moneyness_to_bucket(0.03) == "atm"
    assert abs_log_moneyness_to_bucket(0.0301) == "near"
    assert abs_log_moneyness_to_bucket(0.15) == "wing"
    assert abs_log_moneyness_to_bucket(0.3001) == "out_of_grid"

    assert regime_label(0.60, 0.079) == "normal"
    assert regime_label(0.61, 0.01) == "high_vol"
    assert regime_label(0.50, 0.08) == "high_vol"
    assert regime_label(1.01, 0.01) == "tail"
    assert regime_label(0.90, 0.12) == "tail"


def test_per_bin_p50_p75_structure_spread_usd_is_hand_checked():
    samples = _samples()
    assert len(samples) == 32

    resolution = resolve_spread_quantiles(
        samples,
        option_type="put",
        dte_bucket="dte_30",
        moneyness_bucket="near",
        regime_label="normal",
    )

    assert resolution.status == STATUS_RESOLVED
    assert resolution.resolved_level == "option_type+dte_bucket+moneyness_bucket+regime_label"
    assert resolution.sample_count == 30
    assert resolution.distinct_month_count == 6
    # Fixture spreads are 4, 8, ..., 120 USD per two-leg structure.
    # Nearest-rank p50 = 15th value = 60; p75 = 23rd value = 92.
    assert resolution.p50_round_trip_structure_spread_usd == pytest.approx(60.0)
    assert resolution.p75_round_trip_structure_spread_usd == pytest.approx(92.0)


def test_fallback_order_uses_broader_real_samples_without_fabricating_regime():
    resolution = resolve_spread_quantiles(
        _samples(),
        option_type="put",
        dte_bucket="dte_30",
        moneyness_bucket="near",
        regime_label="high_vol",
    )

    assert resolution.status == STATUS_RESOLVED
    assert resolution.resolved_level == "option_type+dte_bucket+moneyness_bucket"
    assert resolution.p50_round_trip_structure_spread_usd == pytest.approx(60.0)
    assert resolution.p75_round_trip_structure_spread_usd == pytest.approx(92.0)
    assert resolution.sample_months == (
        "2024-08-01",
        "2024-09-01",
        "2024-10-01",
        "2024-11-01",
        "2024-12-01",
        "2025-01-01",
    )


def test_insufficient_bin_remains_inconclusive_after_all_fallbacks():
    resolution = resolve_spread_quantiles(
        _samples(),
        option_type="call",
        dte_bucket="dte_21",
        moneyness_bucket="far",
        regime_label="normal",
    )

    assert resolution.status == STATUS_INCONCLUSIVE
    assert resolution.resolved_level == "fail_closed"
    assert "sample_count 2 < 30" in resolution.reason
    assert resolution.p50_round_trip_structure_spread_usd is None
    assert resolution.p75_round_trip_structure_spread_usd is None


def test_missing_real_bid_ask_input_fails_closed_instead_of_fabricating(tmp_path):
    rows = list(csv.reader(FIXTURE.read_text(encoding="utf-8").splitlines()))
    ask_idx = rows[0].index("ask_price")
    rows[1][ask_idx] = ""
    buf = io.StringIO()
    writer = csv.writer(buf, lineterminator="\n")
    writer.writerows(rows)
    path = tmp_path / "missing_ask.csv"
    path.write_text(buf.getvalue(), encoding="utf-8")

    with pytest.raises(TardisFreeSpreadCalibrationError, match="ask_price"):
        load_tardis_free_structure_samples([path])

def test_forged_sample_month_label_mismatching_timestamp_is_rejected(tmp_path):
    # Red-team: a malformed/overfit CSV that relabels a same-day structure under a
    # different frozen month must NOT be trusted. The month is derived from the
    # authoritative sample_timestamp; a mismatched explicit label fails closed.
    rows = list(csv.reader(FIXTURE.read_text(encoding="utf-8").splitlines()))
    header = rows[0]
    ts_idx = header.index("sample_timestamp")
    month_idx = header.index("sample_month")
    # Row 1 has sample_timestamp 2024-08-01 -> derived month 2024-08-01. Forge a
    # different frozen month label while leaving the timestamp untouched.
    assert rows[1][ts_idx].startswith("2024-08-01")
    rows[1][month_idx] = "2024-12-01"
    buf = io.StringIO()
    writer = csv.writer(buf, lineterminator="\n")
    writer.writerows(rows)
    path = tmp_path / "forged_month.csv"
    path.write_text(buf.getvalue(), encoding="utf-8")

    with pytest.raises(TardisFreeSpreadCalibrationError, match="does not match"):
        load_tardis_free_structure_samples([path])


def test_volume_at_a_single_month_cannot_fake_distinct_month_coverage(tmp_path):
    # Red-team: 30 valid put-near-normal structures all sampled at the SAME real
    # timestamp/month must remain INCONCLUSIVE because distinct-month coverage
    # (min_distinct_months_per_bin=6) is computed from the authoritative month,
    # not from sample volume.
    header = [
        "sample_timestamp",
        "sample_month",
        "underlying",
        "instrument_name",
        "expiration",
        "strike",
        "option_type",
        "bid_price",
        "ask_price",
        "index_price",
        "contract_multiplier",
        "quantity",
        "trailing_30d_rv_annualized",
        "abs_24h_return",
        "structure_sample_id",
        "leg_role",
    ]
    rows = [header]
    for i in range(1, 31):
        sid = f"put-near-onemonth-{i:02d}"
        # Two near-the-money put legs, ~30 DTE from a single 2024-08-01 sample.
        rows.append([
            "2024-08-01T00:00:00Z", "2024-08-01", "ETH",
            "ETH-31AUG24-1900-P", "2024-08-31T00:00:00Z", "1900.0", "put",
            "0.1", f"{0.1 + i * 0.001}", "2000.0", "1.0", "1.0", "0.50", "0.02",
            sid, "long",
        ])
        rows.append([
            "2024-08-01T00:00:00Z", "2024-08-01", "ETH",
            "ETH-31AUG24-1940-P", "2024-08-31T00:00:00Z", "1940.0", "put",
            "0.12", f"{0.12 + i * 0.001}", "2000.0", "1.0", "1.0", "0.50", "0.02",
            sid, "short",
        ])
    buf = io.StringIO()
    writer = csv.writer(buf, lineterminator="\n")
    writer.writerows(rows)
    path = tmp_path / "single_month_volume.csv"
    path.write_text(buf.getvalue(), encoding="utf-8")

    samples = load_tardis_free_structure_samples([path])
    assert len(samples) == 30

    resolution = resolve_spread_quantiles(
        samples,
        option_type="put",
        dte_bucket="dte_30",
        moneyness_bucket="near",
        regime_label="normal",
    )

    assert resolution.status == STATUS_INCONCLUSIVE
    assert resolution.resolved_level == "fail_closed"
    assert resolution.distinct_month_count <= 1
    assert resolution.p50_round_trip_structure_spread_usd is None
    assert resolution.p75_round_trip_structure_spread_usd is None

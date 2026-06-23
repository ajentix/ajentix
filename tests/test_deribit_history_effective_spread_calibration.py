from __future__ import annotations

import importlib.util
import json
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from ajentix_quant.adapters.base import SourceQuality
from ajentix_quant.data.deribit_history_effective_spread_calibration import (
    EFFECTIVE_SPREAD_SOURCE_BASIS,
    RESOLVED_EFFECTIVE_SPREAD_REASON,
    SELECTION_BIAS_CAVEAT,
    DeribitHistoryEffectiveSpreadCalibrationError,
    effective_spread_leg_samples,
    effective_spread_structure_samples,
    load_effective_spread_calibration_manifest,
    raw_source_manifest_sha256,
    resolve_effective_spread_quantiles,
    write_effective_spread_calibration_cache,
)
from ajentix_quant.data.tardis_free_spread_calibration import (
    SPREAD_BINS_FILE,
    STATUS_INCONCLUSIVE,
    STATUS_RESOLVED,
    resolve_spread_quantiles,
)
from ajentix_quant.data.vrp_free_history_cache import (
    IndexPathPoint,
    parse_deribit_history_trade,
    write_vrp_free_history_cache,
)
from ajentix_quant.research.vrp_free_preregistration import DEFAULT_SCENARIO_ID

FIXTURE_PATH = (
    Path(__file__).parent / "fixtures" / "vrp_free_history" / "eth_option_trades_fixture.jsonl"
)
SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "calibrate_deribit_effective_spreads.py"
DAY_MS = 86_400_000


def _fixture_rows() -> list[dict[str, Any]]:
    return [json.loads(line) for line in FIXTURE_PATH.read_text(encoding="utf-8").splitlines()]


def _dt_ms(year: int, month: int, day: int) -> int:
    return int(datetime(year, month, day, tzinfo=UTC).timestamp() * 1000)


def _dt_from_ms(timestamp_ms: int) -> datetime:
    return datetime.fromtimestamp(timestamp_ms / 1000, tz=UTC)


def _expiry_token(timestamp_ms: int, *, dte_days: int = 30) -> str:
    expiry = _dt_from_ms(timestamp_ms) + timedelta(days=dte_days)
    return expiry.strftime("%d%b%y").upper()


def _daily_index_path(
    start_ms: int, end_ms: int, *, index_price: float = 2000.0
) -> tuple[IndexPathPoint, ...]:
    points: list[IndexPathPoint] = []
    current = start_ms
    while current <= end_ms:
        points.append(
            IndexPathPoint(timestamp_ms=current, underlying="ETH", index_price=index_price)
        )
        current += DAY_MS
    return tuple(points)


def _trade_row(
    *,
    trade_id: str,
    timestamp_ms: int,
    trade_seq: int,
    strike: float,
    option_type: str = "put",
    mark_price: float = 0.100,
    half_spread_eth: float = 0.001,
    index_price: float = 2000.0,
) -> dict[str, Any]:
    suffix = "P" if option_type == "put" else "C"
    return {
        "trade_id": trade_id,
        "trade_seq": trade_seq,
        "instrument_name": f"ETH-{_expiry_token(timestamp_ms)}-{int(strike)}-{suffix}",
        "timestamp": timestamp_ms,
        "price": mark_price + half_spread_eth,
        "mark_price": mark_price,
        "iv": 65.0,
        "index_price": index_price,
        "amount": 1.0,
        "contracts": 1.0,
        "direction": "buy",
        "tick_direction": 1,
    }


def _structure_rows(
    *,
    future: bool = False,
    include_base: bool = True,
    daily_start_ms: int | None = None,
    daily_end_ms: int | None = None,
) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    if include_base:
        months = [(2024, 8), (2024, 9), (2024, 10), (2024, 11), (2024, 12), (2025, 1)]
        for i in range(1, 31):
            year, month = months[(i - 1) // 5]
            day = ((i - 1) % 5) + 1
            ts = _dt_ms(year, month, day)
            target_structure_usd = 4.0 * i
            half = target_structure_usd / (4.0 * 2000.0)
            for leg, strike in enumerate((1900.0, 1940.0), start=1):
                rows.append(
                    _trade_row(
                        trade_id=f"base-{i:02d}-{leg}",
                        timestamp_ms=ts,
                        trade_seq=i * 10 + leg,
                        strike=strike,
                        half_spread_eth=half,
                        index_price=2000.0,
                    )
                )
    if future:
        for i in range(1, 31):
            ts = _dt_ms(2025, 4, i)
            target_structure_usd = 2000.0 + 4.0 * i
            half = target_structure_usd / (4.0 * 2000.0)
            for leg, strike in enumerate((1900.0, 1940.0), start=1):
                rows.append(
                    _trade_row(
                        trade_id=f"future-{i:02d}-{leg}",
                        timestamp_ms=ts,
                        trade_seq=10_000 + i * 10 + leg,
                        strike=strike,
                        mark_price=1.0,
                        half_spread_eth=half,
                        index_price=2000.0,
                    )
                )
    if daily_start_ms is not None and daily_end_ms is not None:
        seq = 50_000
        current = daily_start_ms
        while current <= daily_end_ms:
            for leg, strike in enumerate((1900.0, 1940.0), start=1):
                rows.append(
                    _trade_row(
                        trade_id=f"daily-{current}-{leg}",
                        timestamp_ms=current,
                        trade_seq=seq + leg,
                        strike=strike,
                        half_spread_eth=0.001,
                        index_price=2000.0,
                    )
                )
            seq += 10
            current += DAY_MS
    return rows


def _parsed(rows: list[dict[str, Any]]):
    return tuple(parse_deribit_history_trade(row) for row in rows)


def _index_for_rows(
    rows: list[dict[str, Any]], *, index_price: float = 2000.0
) -> tuple[IndexPathPoint, ...]:
    min_ts = min(int(row["timestamp"]) for row in rows) - 30 * DAY_MS
    max_ts = max(int(row["timestamp"]) for row in rows)
    return _daily_index_path(min_ts, max_ts, index_price=index_price)


def _load_cli() -> Any:
    spec = importlib.util.spec_from_file_location(
        "calibrate_deribit_effective_spreads_under_test", SCRIPT
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_effective_spread_bid_ask_synthesis_and_hand_checked_usd_math() -> None:
    trade = parse_deribit_history_trade(_fixture_rows()[0])
    index_path = _daily_index_path(
        trade.timestamp_ms - 30 * DAY_MS,
        trade.timestamp_ms,
        index_price=2500.0,
    )

    leg = effective_spread_leg_samples([trade], index_path)[0]

    # Fixture row: price=0.052 ETH, mark=0.053 ETH.
    # half=0.001 ETH, full effective spread=0.002 ETH, USD=0.002*2500=$5.
    assert leg.bid_price == pytest.approx(0.052)
    assert leg.ask_price == pytest.approx(0.054)
    assert leg.ask_price - leg.bid_price == pytest.approx(2 * abs(0.052 - 0.053))
    assert leg.round_trip_leg_crossing_usd == pytest.approx(5.0)
    assert leg.sample_month == "2024-09-01"
    assert leg.dte_bucket == "dte_21"
    assert leg.moneyness_bucket == "near"
    assert leg.regime_label == "normal"


def test_structure_samples_reuse_g003_resolver_for_resolved_p50_p75() -> None:
    rows = _structure_rows()
    trades = _parsed(rows)
    samples = effective_spread_structure_samples(trades, _index_for_rows(rows))

    assert len(samples) == 30
    assert samples[0].round_trip_structure_spread_usd == pytest.approx(4.0)
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
    assert resolution.p50_round_trip_structure_spread_usd == pytest.approx(60.0)
    assert resolution.p75_round_trip_structure_spread_usd == pytest.approx(92.0)


def test_fold_causal_resolution_excludes_trades_after_train_end() -> None:
    rows = _structure_rows(future=True)
    trades = _parsed(rows)
    index_path = _index_for_rows(rows)

    f1 = resolve_effective_spread_quantiles(
        trades,
        index_path,
        option_type="put",
        dte_bucket="dte_30",
        moneyness_bucket="near",
        regime_label="normal",
        fold_id="F1",
    )
    f2 = resolve_effective_spread_quantiles(
        trades,
        index_path,
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


@pytest.mark.parametrize(
    ("field", "value", "match"),
    [
        ("mark_price", None, "mark_price"),
        ("mark_price", float("nan"), "mark_price"),
        ("index_price", 0.0, "index_price"),
        ("price", float("inf"), "price"),
        ("instrument_name", "ETH-NOT-A-REAL-OPTION", "instrument_name"),
    ],
)
def test_fail_closed_on_missing_nonfinite_and_unparseable_trade_inputs(field, value, match) -> None:
    rows = _structure_rows()
    trade = _parsed(rows)[0]
    bad_trade = replace(trade, **{field: value})

    with pytest.raises(DeribitHistoryEffectiveSpreadCalibrationError, match=match):
        effective_spread_leg_samples([bad_trade], _index_for_rows(rows))


def test_fail_closed_on_missing_index_lookback_coverage() -> None:
    rows = _structure_rows()
    trade = _parsed(rows)[0]
    bad_index_path = (
        IndexPathPoint(timestamp_ms=trade.timestamp_ms, underlying="ETH", index_price=2000.0),
    )

    with pytest.raises(DeribitHistoryEffectiveSpreadCalibrationError, match="lookback"):
        effective_spread_leg_samples([trade], bad_index_path)


def test_thin_bin_remains_inconclusive_without_fabricated_spread() -> None:
    rows = _structure_rows()[:2]
    samples = effective_spread_structure_samples(_parsed(rows), _index_for_rows(rows))

    resolution = resolve_spread_quantiles(
        samples,
        option_type="put",
        dte_bucket="dte_30",
        moneyness_bucket="near",
        regime_label="normal",
    )

    assert len(samples) == 1
    assert resolution.status == STATUS_INCONCLUSIVE
    assert resolution.resolved_level == "fail_closed"
    assert resolution.p50_round_trip_structure_spread_usd is None
    assert resolution.p75_round_trip_structure_spread_usd is None
    assert "sample_count 1 < 30" in resolution.reason


def test_manifest_contains_honest_labels_hashes_and_is_deterministic(tmp_path: Path) -> None:
    rows = _structure_rows()
    trades = _parsed(rows)
    index_path = _index_for_rows(rows)
    raw_manifest = {
        "schema_version": "fixture-raw-cache",
        "manifest_kind": "raw_source",
        "scenario_id": DEFAULT_SCENARIO_ID,
        "cache_fabricated": False,
        "sha256_by_file": {"trades.jsonl": "fixture"},
    }

    a = write_effective_spread_calibration_cache(
        tmp_path / "a",
        DEFAULT_SCENARIO_ID,
        trades=trades,
        index_path=index_path,
        raw_source_manifest=raw_manifest,
        precalibration_config_sha256="fixture-precalibration",
    )
    b = write_effective_spread_calibration_cache(
        tmp_path / "b",
        DEFAULT_SCENARIO_ID,
        trades=trades,
        index_path=index_path,
        raw_source_manifest=raw_manifest,
        precalibration_config_sha256="fixture-precalibration",
    )

    assert (a / "manifest.json").read_text(encoding="utf-8") == (b / "manifest.json").read_text(
        encoding="utf-8"
    )
    manifest = load_effective_spread_calibration_manifest(tmp_path / "a", DEFAULT_SCENARIO_ID)
    assert manifest["spread_basis"] == EFFECTIVE_SPREAD_SOURCE_BASIS
    assert manifest["effective_spread_source_quality"] == EFFECTIVE_SPREAD_SOURCE_BASIS
    assert manifest["spread_source_quality"] == "calibrated_spread_sample"
    assert manifest["authorizing"] is False
    assert manifest["capital_go_allowed"] is False
    assert manifest["cache_fabricated"] is False
    assert manifest["selection_bias_caveat"] == SELECTION_BIAS_CAVEAT
    assert "executed-trade proxy" in manifest["selection_bias_caveat"]
    assert manifest["source_trade_cache_manifest_sha"] == raw_source_manifest_sha256(raw_manifest)
    assert len(manifest["sha256_by_file"][SPREAD_BINS_FILE]) == 64
    assert manifest["row_counts"]["structure_spread_samples"] == 30
    assert len(manifest["per_bin_resolutions"]) == manifest["row_counts"][SPREAD_BINS_FILE]
    # Resolved rows must carry the honest effective-spread reason, never a Tardis/quoted
    # bid-ask source claim inherited from the reused G003 resolver.
    resolved = [r for r in manifest["per_bin_resolutions"] if r["status"] == STATUS_RESOLVED]
    assert resolved
    assert all(r["reason"] == RESOLVED_EFFECTIVE_SPREAD_REASON for r in resolved)
    assert all("tardis" not in r["reason"].lower() for r in manifest["per_bin_resolutions"])
    assert all("bid_ask" not in r["reason"].lower() for r in manifest["per_bin_resolutions"])
    bins_text = (a / SPREAD_BINS_FILE).read_text(encoding="utf-8")
    assert "resolved_from_real_tardis_bid_ask_samples" not in bins_text
    assert RESOLVED_EFFECTIVE_SPREAD_REASON in bins_text


def test_cli_loads_g002_cache_and_writes_effective_spread_cache_network_free(
    tmp_path: Path,
) -> None:
    raw_root = tmp_path / "raw"
    cache_root = tmp_path / "effective-cache"
    raw_rows = _structure_rows(
        include_base=False,
        daily_start_ms=_dt_ms(2024, 7, 1),
        daily_end_ms=_dt_ms(2024, 9, 5),
    )
    write_vrp_free_history_cache(
        raw_root,
        DEFAULT_SCENARIO_ID,
        raw_rows=raw_rows,
        currency="ETH",
        start_ts_ms=min(int(row["timestamp"]) for row in raw_rows),
        end_ts_ms=max(int(row["timestamp"]) for row in raw_rows),
        download_timestamp_ms=min(int(row["timestamp"]) for row in raw_rows),
        source_ids=["fixture-deribit-history"],
        source_url_ids=["fixture://deribit-effective-spread"],
        source_quality={
            "option_trades": SourceQuality.FIXTURE,
            "underlying_index": SourceQuality.FIXTURE,
        },
        acquisition_tool_version="fixture-effective-spread-cli-test-v1",
    )

    cli = _load_cli()
    assert (
        cli.main(
            [
                "--raw-source-root",
                str(raw_root),
                "--effective-spread-calibration-root",
                str(cache_root),
                "--min-sample-timestamp-ms",
                str(_dt_ms(2024, 8, 1)),
                "--json",
            ]
        )
        == 0
    )
    manifest = load_effective_spread_calibration_manifest(cache_root, DEFAULT_SCENARIO_ID)
    assert manifest["spread_basis"] == EFFECTIVE_SPREAD_SOURCE_BASIS
    assert manifest["sample_filter"]["min_timestamp_ms"] == _dt_ms(2024, 8, 1)
    assert manifest["authorizing"] is False
    assert manifest["capital_go_allowed"] is False
    assert manifest["cache_fabricated"] is False

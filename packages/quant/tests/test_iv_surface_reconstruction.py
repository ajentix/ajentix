from __future__ import annotations

import json
from pathlib import Path

import pytest

from ajentix_quant.adapters.base import SourceQuality
from ajentix_quant.data.options_cache import load_normalized_cache
from ajentix_quant.data.vrp_free_history_cache import (
    load_vrp_free_history_cache,
    write_vrp_free_history_cache,
)
from ajentix_quant.options.iv_surface_reconstruction import (
    LINEAGE_FILE,
    IVSurfaceCoverageError,
    reconstruct_from_history_dataset,
    reconstruct_iv_surface_at,
    reconstructed_chains_sha256,
    write_reconstructed_chain_cache,
)
from ajentix_quant.options.valuation import black_scholes_value_greeks, year_fraction_act_365
from ajentix_quant.research.vrp_free_preregistration import (
    DEFAULT_SCENARIO_ID,
    PLAN_RECONSTRUCTION_CONFIG,
    PLAN_SOURCE_QUALITY_BRIDGE,
)

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


def _loaded_fixture(tmp_path: Path):
    _write_fixture_cache(tmp_path)
    return load_vrp_free_history_cache(tmp_path, DEFAULT_SCENARIO_ID)


def _leg(chains, instrument_name: str):
    for chain in chains:
        for leg in chain.snapshot.legs:
            if leg.instrument_name == instrument_name:
                return chain, leg
    raise AssertionError(f"missing reconstructed leg {instrument_name}")


def test_bs_priced_snapshot_matches_black_scholes_diagnostics(tmp_path):
    dataset = _loaded_fixture(tmp_path)
    chains = reconstruct_from_history_dataset(
        dataset,
        snapshot_timestamps_ms=[START_MS],
    )

    chain, leg = _leg(chains, "ETH-27SEP24-2400-P")
    expected = black_scholes_value_greeks(
        option_type=leg.option_type,
        spot=2500.0,
        strike=2400.0,
        time_to_expiry_years=year_fraction_act_365(
            snapshot_ts_ms=START_MS,
            expiry_ms=leg.expiry_ms,
        ),
        volatility=0.652,
    )

    assert leg.bid_price == pytest.approx(expected.value / 2500.0)
    assert leg.ask_price == pytest.approx(leg.bid_price)
    assert leg.mark_price == pytest.approx(leg.bid_price)
    assert leg.bid_iv == pytest.approx(0.652)
    assert leg.ask_iv == pytest.approx(0.652)
    assert leg.greek_provenance_key == PLAN_RECONSTRUCTION_CONFIG["pricing_model"]
    assert leg.quote_ts_ms == START_MS
    assert leg.quote_age_s == 0.0
    assert leg.source_quality is SourceQuality.FIXTURE
    assert leg.source_quality is not SourceQuality.VENUE

    assert chain.lineage.free_source_quality == PLAN_SOURCE_QUALITY_BRIDGE["free_source_quality"]
    assert chain.lineage.authorizing is False
    assert chain.lineage.capital_go_allowed is False
    assert chain.lineage.non_authorizing_reason == "reconstructed_from_real_trade_iv"
    assert chain.lineage.legs[0].model_value_usd == pytest.approx(expected.value)


def test_no_extrapolation_outside_observed_instruments_or_strikes(tmp_path):
    dataset = _loaded_fixture(tmp_path)
    chains = reconstruct_from_history_dataset(dataset, snapshot_timestamps_ms=[START_MS])
    reconstructed_strikes = sorted(
        {leg.strike for chain in chains for leg in chain.snapshot.legs}
    )

    assert reconstructed_strikes == [2400.0, 2800.0]
    assert all(
        leg.instrument_name != "ETH-27SEP24-2600-P"
        for chain in chains
        for leg in chain.snapshot.legs
    )
    with pytest.raises(IVSurfaceCoverageError, match="missing_required_instrument_coverage"):
        reconstruct_iv_surface_at(
            dataset.trades,
            snapshot_ts_ms=START_MS,
            index_path=dataset.index_path,
            required_instrument_names=["ETH-27SEP24-2600-P"],
        )


def test_reconstruction_and_cache_manifest_are_reproducible(tmp_path):
    dataset = _loaded_fixture(tmp_path / "raw")
    chains_a = reconstruct_from_history_dataset(
        dataset,
        snapshot_timestamps_ms=[START_MS, MID_MS],
    )
    chains_b = reconstruct_from_history_dataset(
        dataset,
        snapshot_timestamps_ms=[MID_MS, START_MS],
    )

    assert reconstructed_chains_sha256(chains_a) == reconstructed_chains_sha256(chains_b)

    dir_a = write_reconstructed_chain_cache(
        tmp_path / "cache-a",
        DEFAULT_SCENARIO_ID,
        reconstructed_chains=chains_a,
        raw_manifest_sha256="raw-fixture-sha",
    )
    dir_b = write_reconstructed_chain_cache(
        tmp_path / "cache-b",
        DEFAULT_SCENARIO_ID,
        reconstructed_chains=chains_b,
        raw_manifest_sha256="raw-fixture-sha",
    )

    assert (dir_a / "manifest.json").read_text(encoding="utf-8") == (
        dir_b / "manifest.json"
    ).read_text(encoding="utf-8")
    assert (dir_a / "option_chains.csv").read_text(encoding="utf-8") == (
        dir_b / "option_chains.csv"
    ).read_text(encoding="utf-8")
    assert (dir_a / LINEAGE_FILE).read_text(encoding="utf-8") == (
        dir_b / LINEAGE_FILE
    ).read_text(encoding="utf-8")

    manifest = json.loads((dir_a / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["fabricated_quotes_or_spreads"] is False
    assert manifest["synthetic_model_prices"] is True
    assert (
        manifest["free_lineage"]["legacy_option_leg_source_quality"]
        == "SourceQuality.FIXTURE"
    )
    assert (
        manifest["free_lineage"]["forbidden_option_leg_source_quality"]
        == "SourceQuality.VENUE"
    )
    loaded_snapshots = load_normalized_cache(tmp_path / "cache-a", DEFAULT_SCENARIO_ID)
    assert sum(len(snapshot.legs) for snapshot in loaded_snapshots) == 4


def test_reconstructed_legs_use_fixture_not_venue(tmp_path):
    dataset = _loaded_fixture(tmp_path)
    chains = reconstruct_from_history_dataset(
        dataset,
        snapshot_timestamps_ms=[START_MS, MID_MS, END_MS],
    )

    assert chains
    assert all(
        leg.source_quality is SourceQuality.FIXTURE
        for chain in chains
        for leg in chain.snapshot.legs
    )
    assert not any(
        leg.source_quality is SourceQuality.VENUE
        for chain in chains
        for leg in chain.snapshot.legs
    )
    assert all(
        value is SourceQuality.FIXTURE
        for chain in chains
        for value in chain.snapshot.source_quality_map.values()
    )


def test_missing_required_coverage_fails_closed_without_rows(tmp_path):
    dataset = _loaded_fixture(tmp_path)

    with pytest.raises(IVSurfaceCoverageError) as excinfo:
        reconstruct_iv_surface_at(
            dataset.trades,
            snapshot_ts_ms=START_MS - 1,
            index_path=dataset.index_path,
        )

    assert excinfo.value.status == "INCONCLUSIVE"
    assert "missing_index_coverage" in str(excinfo.value)

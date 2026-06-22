"""G001 Phase 0: VRP pre-registration governance in schema/dry-run mode."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from ajentix_quant.research import vrp_preregistration as vrp

REPO_ROOT = Path(__file__).resolve().parents[1]


def _build() -> dict[str, object]:
    return vrp.build_preregistration(REPO_ROOT)


def test_build_is_deterministic_and_run_id_derives_from_content_hash():
    a = _build()
    b = _build()
    assert a == b
    assert a["run_id"] == b["run_id"]
    assert str(a["run_id"]).startswith("vrp-")
    assert str(a["content_hash"])[:12] == str(a["run_id"]).removeprefix("vrp-")


def test_fresh_schema_build_verifies_valid_without_real_cache():
    artifact = _build()
    result = vrp.verify_preregistration(artifact, REPO_ROOT)
    assert result.valid is True
    assert result.run_status == "valid"
    assert result.mismatches == ()


def test_write_preregistration_refuses_emit_without_required_manifests(tmp_path):
    with pytest.raises(vrp.PreregistrationError):
        vrp.write_preregistration(REPO_ROOT, out_dir=tmp_path.as_posix())
    assert list(tmp_path.iterdir()) == []


def _write_manifest(root: Path, payload: dict[str, object]) -> Path:
    path = root / vrp.DEFAULT_SCENARIO_ID / "manifest.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, sort_keys=True) + "\n", encoding="utf-8")
    return path


def test_write_and_load_round_trip_with_tiny_fixture_manifests(tmp_path):
    raw_manifest = _write_manifest(tmp_path / "raw", {"kind": "raw", "rows": 0})
    normalized_manifest = _write_manifest(
        tmp_path / "cache", {"kind": "normalized", "rows": 0}
    )
    dest = vrp.write_preregistration(
        REPO_ROOT,
        raw_manifest_path=raw_manifest,
        normalized_manifest_path=normalized_manifest,
        out_dir=(tmp_path / "out").as_posix(),
    )
    loaded = vrp.load_preregistration(dest)
    result = vrp.verify_preregistration(loaded, REPO_ROOT)
    assert dest.is_file()
    assert loaded["run_id"].startswith("vrp-")
    assert result.valid is True


def test_locked_plan_values_present():
    artifact = _build()
    plan = artifact["plan"]

    assert plan["schema_version"] == vrp.SCHEMA_VERSION
    assert plan["authorizing_universe"] == "ETH_credit_spreads_only"
    assert plan["scenarios"] == {"ETH": "deribit_options_eth_vrp_v1"}
    assert "BTC" not in plan["scenarios"]
    assert plan["primary_equity_usd"] == 1000.0
    assert plan["equity_grid"] == [500.0, 1000.0, 2000.0]
    assert plan["risk_limits"] == {
        "reserve_pct": 0.25,
        "per_structure_max_loss_pct": 0.25,
        "aggregate_max_defined_risk_pct": 0.40,
    }
    assert len(plan["folds"]) == 7
    assert plan["warmup_start"] == "2024-08-01T00:00:00Z"
    assert plan["coverage_window"] == [
        "2024-09-01T00:00:00Z",
        "2026-06-01T00:00:00Z",
    ]

    grid = plan["structure_grid"]
    assert grid["search_space_version"] == "vrp-eth-credit-spread-grid-v1"
    assert grid["structure_types"] == ["put_credit_spread", "call_credit_spread"]
    assert grid["dte_targets"] == [21, 30, 45]
    assert grid["short_leg_abs_delta"] == [0.10, 0.16, 0.25]
    assert grid["width_usd"] == [100, 150, 200]
    assert grid["min_credit_to_width"] == [0.15, 0.20]
    assert grid["exit_rule"] == {
        "profit_take_frac": 0.50,
        "stop_loss_credit_mult": 2.0,
        "else": "hold_to_european_settlement",
    }
    assert grid["rolls"] is False
    assert grid["candidates_per_fold"] == 108
    assert grid["selection_cost_mode"] == "taker_roundtrip_plus_crossing"
    assert grid["selection_equity_usd"] == 1000.0

    assert plan["cost_path"] == {
        "identity": "ajentix_quant.backtest.option_costs:evaluate_structure_costs",
        "maker_can_authorize": False,
    }
    assert plan["greek_provenance"] == {
        "selection_source": "vendor_cached_hashed_preferred_else_local",
        "local_formula": "black_scholes",
        "day_count": "act/365",
        "risk_free_rate": 0.0,
        "dividend": 0.0,
        "timestamp_convention": "utc_snapshot",
        "local_greeks_role": "diagnostic_only",
        "deterministic_tie_breakers": True,
    }
    assert plan["settlement"] == {
        "style": "european",
        "settlement_index": "deribit_eth_index",
        "premium_currency": "ETH",
        "fee_currency": "ETH",
        "collateral_currency": "USDC_or_ETH",
        "contract_multiplier": 1.0,
        "usd_conversion_source": "deribit_eth_index",
        "expiry_exit_rule": "european_settlement",
        "missing_settlement": "fail_closed",
    }
    assert plan["stress_rule"] == {
        "method": "top_k_realized_vol_expansion",
        "k": 3,
        "window_hours": 24,
        "non_overlapping": True,
        "score": "rv_24h_over_trailing_30d_rv",
        "tie_break": ["max_abs_1h_return", "earliest_utc_start"],
        "inputs": "underlying_index_only",
        "coverage_window": ["2024-09-01T00:00:00Z", "2026-06-01T00:00:00Z"],
        "missing_required_coverage": "INCONCLUSIVE",
    }
    assert plan["trial_budget"] == {
        "grid_versions": 1,
        "max_train_trials": 756,
        "max_heldout_evals": 7,
        "multiplicity_method": "hard_trial_budget_cap",
        "no_hidden_retries": True,
        "no_fold_deletion": True,
    }
    assert plan["go_bar"]["source_quality_required"] == "venue_full_historical_chain"
    assert plan["go_bar"]["min_sharpe"] == 0.8
    assert plan["go_bar"]["max_mdd_incl_stress"] == 0.25
    assert plan["go_bar"]["min_folds_nonneg"] == 4
    assert plan["go_bar"]["total_folds"] == 7
    assert plan["go_bar"]["min_folds_with_entries"] == 3
    assert plan["go_bar"]["min_total_entries"] == 10
    assert plan["go_bar"]["max_single_fold_pnl_share"] == 0.50
    assert plan["go_bar"]["max_single_cluster_pnl_share"] == 0.35
    assert plan["go_bar"]["sharpe_inflation_audit_threshold"] == 2.5
    assert set(plan["go_bar"]["non_authorizing"]) >= {
        "maker",
        "fixture",
        "btc",
        "naked",
    }


def test_load_missing_or_garbage_raises(tmp_path):
    with pytest.raises(vrp.PreregistrationError):
        vrp.load_preregistration(tmp_path / "missing.json")
    bad = tmp_path / "bad.json"
    bad.write_text("not json{", encoding="utf-8")
    with pytest.raises(vrp.PreregistrationError):
        vrp.load_preregistration(bad)

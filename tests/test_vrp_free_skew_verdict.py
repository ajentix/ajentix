from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import scripts.run_vrp_free_skew_verdict as runner


class _FakeReport:
    def __init__(self, payload: dict):
        self._payload = payload

    def as_dict(self) -> dict:
        return dict(self._payload)


def test_skew_runner_wires_effective_spread_stress_cost_and_writes_reports(
    tmp_path: Path, monkeypatch
) -> None:
    coverage_start = 1_730_419_200_000
    coverage_end = 1_730_505_600_000
    kept_trade = SimpleNamespace(timestamp_ms=coverage_start)
    dropped_warmup_trade = SimpleNamespace(timestamp_ms=coverage_start - 1)
    index_path = (SimpleNamespace(timestamp_ms=coverage_start, index_price=2500.0),)
    raw_dataset = SimpleNamespace(
        manifest={
            "date_range": {
                "start_ts_ms": coverage_start - 86_400_000,
                "coverage_start_ts_ms": coverage_start,
                "end_ts_ms": coverage_end,
            },
            "raw_manifest_sha256": "raw-sha",
        },
        trades=(dropped_warmup_trade, kept_trade),
        index_path=index_path,
    )
    snapshots = (SimpleNamespace(underlying="ETH", snapshot_ts_ms=coverage_start),)
    calibration_samples = (SimpleNamespace(sample_month="2024-11-01"),)
    selected_structure = SimpleNamespace(structure_id="official-structure")
    diagnostic_structure = SimpleNamespace(structure_id="diagnostic-structure")
    stress_payload = {
        "schema_version": "aq-vrp-free-stress-v1",
        "scenario_id": runner.DEFAULT_SCENARIO_ID,
        "status": "RAN",
        "ran": True,
        "reason_codes": [],
        "max_loss_ok": True,
        "selected_windows": [{"window_id": "W1"}],
        "max_loss_evidence": [{"structure_id": "official-structure"}],
        "lineage": {"authorizing": False, "capital_go_allowed": False},
    }
    fake_stress = _FakeReport(stress_payload)
    walk_payload = {
        "schema_version": "aq-vrp-free-walk-forward-economics-v1",
        "run_status": "valid",
        "outcome": "NO_GO",
        "verdict": "NO_GO",
        "authorizing": False,
        "capital_go_allowed": False,
        "committed_run_status": "valid",
        "committed_clean_heldout_positive": False,
        "committed_reason_codes": ["INSUFFICIENT_HELDOUT_ENTRIES"],
        "fold_ids": ["BOUND_2024_11_REAL_CACHE"],
        "free_lineage": {"authorizing": False, "capital_go_allowed": False},
        "lineage_valid": True,
        "lineage_mismatches": [],
        "cost_budget_status": "INCONCLUSIVE",
        "stress_status": "RAN",
        "stress_max_loss_ok": True,
        "stress_ran": True,
        "stress": stress_payload,
        "cost_budget_evidence": [{"structure_id": "official-structure"}],
        "fold_economics": [
            {
                "fold_id": "BOUND_2024_11_REAL_CACHE",
                "entries": 1,
                "pnl_usd": -1.0,
                "cost_budget_statuses": ["INCONCLUSIVE"],
            }
        ],
        "reason_codes": ["FREE_COST_BUDGET_INCONCLUSIVE"],
        "committed_hard_gate_report": {"reason_codes": []},
    }
    final_payload = {
        "schema_version": "aq-vrp-free-final-verdict-v1",
        "scenario_id": runner.DEFAULT_SCENARIO_ID,
        "run_status": "valid",
        "verdict": "NO_GO",
        "reason_codes": ["ECONOMIC_FAILURE"],
        "authorizing": False,
        "capital_go_allowed": False,
    }
    calls: dict[str, object] = {}

    def fake_effective_samples(trades, idx_path):
        calls["coverage_trades"] = tuple(trades)
        calls["effective_index_path"] = idx_path
        return calibration_samples

    def fake_fold_inputs(snap_arg, *, symbol, folds):
        calls["folds"] = tuple(folds)
        calls["fold_snapshots"] = snap_arg
        calls["fold_symbol"] = symbol
        return (
            {"BOUND_2024_11_REAL_CACHE": SimpleNamespace(selected_param_keys=("k",))},
            {"BOUND_2024_11_REAL_CACHE": SimpleNamespace()},
            {"BOUND_2024_11_REAL_CACHE": (selected_structure,)},
            [SimpleNamespace(fold_id="BOUND_2024_11_REAL_CACHE")],
            [{"fold_id": "BOUND_2024_11_REAL_CACHE", "breakeven": {"outcome": "INCONCLUSIVE"}}],
        )

    def fake_stress_eval(**kwargs):
        calls["stress_kwargs"] = kwargs
        return fake_stress

    def fake_run_walk_forward(**kwargs):
        calls["walk_kwargs"] = kwargs
        return _FakeReport(walk_payload)

    def fake_final_verdict(**kwargs):
        calls["final_kwargs"] = kwargs
        return _FakeReport(final_payload)

    monkeypatch.setattr(runner, "load_vrp_free_history_cache", lambda *_: raw_dataset)
    monkeypatch.setattr(
        runner,
        "load_normalized_manifest",
        lambda *_: {"source_quality": {"option_chain": "fixture"}},
    )
    monkeypatch.setattr(runner, "load_normalized_cache", lambda *_: snapshots)
    monkeypatch.setattr(runner, "effective_spread_structure_samples", fake_effective_samples)
    monkeypatch.setattr(runner, "_fold_inputs_for_folds", fake_fold_inputs)
    monkeypatch.setattr(runner, "evaluate_exact_underlying_stress", fake_stress_eval)
    monkeypatch.setattr(runner, "run_free_walk_forward", fake_run_walk_forward)
    monkeypatch.setattr(runner, "decide_vrp_free_final_verdict", fake_final_verdict)
    monkeypatch.setattr(
        runner, "_diagnostic_candidate_structures", lambda *_, **__: (diagnostic_structure,)
    )
    monkeypatch.setattr(
        runner,
        "_structure_summary",
        lambda structure: {"structure_id": structure.structure_id},
    )
    monkeypatch.setattr(
        runner,
        "_diagnostic_stress",
        lambda **_: {
            "diagnostic": True,
            "status": "RAN",
            "ran": True,
            "reason_codes": [],
            "authorizing": False,
            "capital_go_allowed": False,
            "gating": False,
        },
    )
    monkeypatch.setattr(
        runner,
        "_diagnostic_cost_budget",
        lambda **_: {
            "diagnostic": True,
            "authorizing": False,
            "capital_go_allowed": False,
            "gating": False,
            "observed_quantile_rows_resolved": 1,
            "frozen_resolution_rows_resolved": 1,
            "rows": [{"observed_p50_round_trip_structure_spread_usd": 1.0}],
        },
    )

    exit_code = runner.main(
        [
            "--repo-root",
            tmp_path.as_posix(),
            "--raw-source-root",
            "raw",
            "--reconstructed-cache-root",
            "recon",
            "--reports-dir",
            "reports",
        ]
    )

    assert exit_code == 0
    assert calls["coverage_trades"] == (kept_trade,)
    assert calls["effective_index_path"] is index_path
    stress_kwargs = calls["stress_kwargs"]
    assert stress_kwargs["structures"] == (selected_structure,)
    assert stress_kwargs["index_path"] is index_path
    assert stress_kwargs["reconstructed_chains"] is snapshots
    walk_kwargs = calls["walk_kwargs"]
    assert walk_kwargs["calibration_samples"] is calibration_samples
    assert walk_kwargs["stress_result"] is fake_stress
    assert walk_kwargs["expected_folds"][0]["test_end"] == "2024-11-03T00:00:00Z"
    assert (
        calls["final_kwargs"]["tardis_spread_calibration_manifest"]["source_basis"]
        == runner.EFFECTIVE_SPREAD_SOURCE_BASIS
    )

    report_path = tmp_path / "reports" / "vrp_free_skew_verdict.json"
    markdown_path = tmp_path / "reports" / "vrp_free_skew_verdict.md"
    payload = json.loads(report_path.read_text(encoding="utf-8"))
    assert markdown_path.is_file()
    assert payload["verdict"] == "NO_GO"
    assert payload["spread_basis"]["source_basis"] == "deribit_history_trade_vs_mark_effective"
    assert payload["official"]["walk_forward"]["stress_ran"] is True
    assert payload["official"]["cost_budget"]["evidence_count"] == 1
    assert payload["diagnostics"]["cost_budget"]["observed_quantile_rows_resolved"] == 1


def test_stress_grid_root_cause_flags_non_hourly_free_index_path() -> None:
    # Trade-time index path: no point lands on an exact-hour boundary.
    irregular = tuple(
        SimpleNamespace(timestamp_ms=1_730_419_200_000 + offset)
        for offset in (11_161, 30_534, 37_011)
    )
    root = runner._stress_grid_root_cause(irregular)
    assert root["code"] == "FREE_INDEX_PATH_NOT_HOURLY_GRID"
    assert root["on_hour_points"] == 0
    assert root["index_points"] == 3
    assert root["fabricated"] is False


def test_stress_grid_root_cause_flags_sparse_hourly_coverage() -> None:
    # On-the-hour points exist but are too sparse for the frozen k windows.
    hourly = tuple(
        SimpleNamespace(timestamp_ms=1_730_419_200_000 + i * runner._HOUR_MS) for i in range(3)
    )
    root = runner._stress_grid_root_cause(hourly)
    assert root["code"] == "INSUFFICIENT_STRESS_WINDOW_COVERAGE"
    assert root["on_hour_points"] == 3
    assert root["fabricated"] is False

"""Strategy-v2 G004 adversarial tests for the final verdict and ADR gate.

These tests deliberately try illegitimate ADR-0002 promotion paths and drive the
final-verdict CLI over tmp_path-only synthetic fixtures. The pre-registration crypto is
monkeypatched here because the hash/lineage verifier has separate coverage.
"""

from __future__ import annotations

import hashlib
import importlib.util
import json
from pathlib import Path
from typing import Any

import pytest

from ajentix_quant.research.final_verdict import (
    ADR_REASON_CONCENTRATION_FAILURE,
    ADR_REASON_FOLD_COLLAPSE,
    ADR_REASON_INVALID_LINEAGE,
    ADR_REASON_MAKER_ONLY_DEPENDENCE,
    ADR_REASON_NO_CLEAN_HELDOUT_GO,
    REASON_INVALID_LINEAGE,
    REASON_PREREGISTRATION_INVALID,
    VERDICT_GO,
    VERDICT_INCONCLUSIVE,
    VERDICT_NO_GO,
    VerdictInputs,
    build_verdict_inputs,
    decide_final_verdict,
    should_promote_adr_0002,
)
from ajentix_quant.research.preregistration import VerifyResult

REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPT = REPO_ROOT / "scripts" / "run_final_verdict.py"
ADR_REL = Path("docs") / "adr" / "0002-strategy-v2-hard-performance-gate.md"
SYMBOLS = ("BTC/USDT:USDT", "ETH/USDT:USDT")


def _load_cli() -> Any:
    spec = importlib.util.spec_from_file_location("run_final_verdict_redteam", SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _promotable_inputs(**overrides: Any) -> VerdictInputs:
    base: dict[str, Any] = {
        "preregistration_valid": True,
        "lineage_consistent": True,
        "a1_go_symbols": SYMBOLS,
        "a1_no_go_symbols": (),
        "a1_inconclusive_symbols": (),
        "walk_forward_ran": True,
        "held_out_clean_go": True,
        "pivot_ran": True,
        "pivot_any_clears": False,
        "fold_collapse": False,
        "concentration_failure": False,
        "maker_only_dependence": False,
    }
    base.update(overrides)
    return VerdictInputs(**base)


@pytest.mark.parametrize(
    ("case_name", "overrides", "expected_block_reasons"),
    [
        (
            "go_verdict_without_clean_heldout",
            {"held_out_clean_go": False},
            [ADR_REASON_NO_CLEAN_HELDOUT_GO],
        ),
        (
            "clean_heldout_without_a1_go_symbols",
            {"a1_go_symbols": ()},
            [ADR_REASON_NO_CLEAN_HELDOUT_GO],
        ),
        (
            "go_clean_with_fold_collapse",
            {"fold_collapse": True},
            [ADR_REASON_FOLD_COLLAPSE],
        ),
        (
            "go_clean_with_concentration_failure",
            {"concentration_failure": True},
            [ADR_REASON_CONCENTRATION_FAILURE],
        ),
        (
            "go_clean_with_maker_only_dependence",
            {"maker_only_dependence": True},
            [ADR_REASON_MAKER_ONLY_DEPENDENCE],
        ),
        (
            "go_clean_with_inconsistent_lineage",
            {"lineage_consistent": False},
            [ADR_REASON_INVALID_LINEAGE],
        ),
        (
            "go_clean_with_invalid_preregistration",
            {"preregistration_valid": False},
            [ADR_REASON_INVALID_LINEAGE],
        ),
    ],
)
def test_illegitimate_adr_promotion_paths_fail_closed(
    case_name: str,
    overrides: dict[str, Any],
    expected_block_reasons: list[str],
) -> None:
    del case_name
    inputs = _promotable_inputs(**overrides)

    promoted, observed_block_reasons = should_promote_adr_0002(VERDICT_GO, inputs)

    assert promoted is False
    assert observed_block_reasons == expected_block_reasons


def test_only_legitimate_clean_preregistered_heldout_go_promotes_adr() -> None:
    inputs = _promotable_inputs()

    verdict, verdict_reasons = decide_final_verdict(inputs)
    promoted, block_reasons = should_promote_adr_0002(verdict, inputs)

    assert verdict == VERDICT_GO
    assert verdict_reasons
    assert promoted is True
    assert block_reasons == []


def test_pure_verdict_inputs_and_decision_are_deterministic() -> None:
    breakeven_summary = {
        "a1_go_symbols": ["BTC/USDT:USDT"],
        "a1_no_go_symbols": ["ETH/USDT:USDT"],
        "a1_inconclusive_symbols": [],
    }
    walk_forward = {
        "ran": True,
        "clean_heldout_go": True,
        "fold_collapse": False,
        "concentration_failure": False,
    }
    pivot_summary = {"ran": True, "any_candidate_clears": False}

    first_inputs = build_verdict_inputs(
        preregistration_valid=True,
        lineage_consistent=True,
        breakeven_summary=dict(breakeven_summary),
        walk_forward=dict(walk_forward),
        pivot_summary=dict(pivot_summary),
    )
    second_inputs = build_verdict_inputs(
        preregistration_valid=True,
        lineage_consistent=True,
        breakeven_summary=dict(breakeven_summary),
        walk_forward=dict(walk_forward),
        pivot_summary=dict(pivot_summary),
    )

    assert first_inputs == second_inputs
    assert decide_final_verdict(first_inputs) == decide_final_verdict(second_inputs)


def _write_prereg(repo: Path, run_id: str = "stratv2-redteam001") -> Path:
    prereg_dir = repo / "docs" / "preregistration"
    prereg_dir.mkdir(parents=True, exist_ok=True)
    path = prereg_dir / f"{run_id}.json"
    payload = {
        "schema_version": "stratv2-prereg-v1",
        "run_id": run_id,
        "content_hash": "redteam-content-hash",
        "plan": {
            "trial_budget": {
                "grid_versions": 1,
                "max_primary_train_trials": 224,
                "max_primary_heldout_evals": 14,
                "max_secondary_sensitivity_evals": 42,
                "total_heldout_cap": 56,
                "multiplicity_method": "hard_trial_budget_cap",
                "no_hidden_retries": True,
                "no_fold_deletion": True,
            }
        },
    }
    path.write_text(json.dumps(payload, sort_keys=True), encoding="utf-8")
    return path


def _write_breakeven(
    repo: Path,
    *,
    a1_decision: str,
    branch_decision: str,
    btc_run_status: str = "valid",
    eth_run_status: str = "valid",
) -> None:
    reports = repo / "reports"
    reports.mkdir(parents=True, exist_ok=True)
    by_symbol = {
        symbol: {
            "a1_decision": a1_decision,
            "branch_decision": branch_decision,
            "reason_codes": ([] if a1_decision == "GO" else ["SYNTHETIC_NO_GO"]),
        }
        for symbol in SYMBOLS
    }
    statuses = {"btc": btc_run_status, "eth": eth_run_status}
    for stem, symbol in (("btc", SYMBOLS[0]), ("eth", SYMBOLS[1])):
        payload = {
            "run_status": statuses[stem],
            "run_id": "stratv2-redteam001",
            "content_hash": f"breakeven-{stem}",
            "preregistration_sha256": "synthetic-prereg-sha",
            "scenario_id": f"{stem}_v2_synthetic",
            "symbol": symbol,
            "branch_summary": {"by_symbol": by_symbol},
            "breakeven": {"funding_rows_insample": 1096},
        }
        (reports / f"breakeven_{stem}_v2.json").write_text(
            json.dumps(payload, sort_keys=True), encoding="utf-8"
        )


def _write_pivot(
    repo: Path,
    *,
    any_clears: bool = False,
    run_status: str = "valid",
) -> None:
    reports = repo / "reports"
    reports.mkdir(parents=True, exist_ok=True)
    payload = {
        "run_status": run_status,
        "run_id": "stratv2-redteam001",
        "content_hash": "pivot-redteam",
        "preregistration_sha256": "synthetic-prereg-sha",
        "overall": {
            "any_candidate_clears": any_clears,
            "clearing_candidate_ids": ["HL_DIRECT_SYN"] if any_clears else [],
            "conclusion": "synthetic pivot fixture",
        },
        "candidates": [
            {
                "candidate_id": "HL_DIRECT_SYN",
                "candidate_type": "hl_direct_funding",
                "symbol": "X/USDC:USDC",
                "venue": "hyperliquid",
                "clears": any_clears,
                "qualifying_24h_window_pct": 0.05,
                "qualifying_24h_window_count": 10,
                "cluster_count": 3,
                "max_single_week_share": 0.67,
                "primary_slippage_bps_per_leg": 8.5,
                "reason_codes": [] if any_clears else ["WEEKLY_CONCENTRATION_TOO_HIGH"],
            }
        ],
    }
    (reports / "pivot_venue_feasibility_v2.json").write_text(
        json.dumps(payload, sort_keys=True), encoding="utf-8"
    )


def _write_walk_forward(
    repo: Path,
    *,
    clean_heldout_go: bool,
    fold_collapse: bool = False,
    concentration_failure: bool = False,
    run_status: str = "valid",
) -> None:
    reports = repo / "reports"
    reports.mkdir(parents=True, exist_ok=True)
    payload = {
        "run_status": run_status,
        "run_id": "stratv2-redteam001",
        "content_hash": "walk-forward-redteam",
        "preregistration_sha256": "synthetic-prereg-sha",
        "clean_heldout_go": clean_heldout_go,
        "fold_collapse": fold_collapse,
        "concentration_failure": concentration_failure,
    }
    (reports / "walk_forward_redteam.json").write_text(
        json.dumps(payload, sort_keys=True), encoding="utf-8"
    )


def _valid_verify(*_args: Any, **_kwargs: Any) -> VerifyResult:
    return VerifyResult(valid=True, run_status="valid", mismatches=())


def _invalid_verify(*_args: Any, **_kwargs: Any) -> VerifyResult:
    return VerifyResult(
        valid=False,
        run_status="invalid",
        mismatches=("synthetic preregistration mismatch",),
    )


def _read_final_payload(repo: Path) -> dict[str, Any]:
    return json.loads(
        (repo / "reports" / "strategy_v2_final_verdict.json").read_text(
            encoding="utf-8"
        )
    )


def _content_hash_body(payload: dict[str, Any]) -> dict[str, Any]:
    return {
        key: value
        for key, value in payload.items()
        if key not in {"content_hash", "generated_at"}
    }


def test_cli_invalid_prereg_fails_closed_with_no_adr(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cli = _load_cli()
    _write_prereg(tmp_path)
    _write_breakeven(tmp_path, a1_decision="NO_GO", branch_decision="A2")
    _write_pivot(tmp_path)
    monkeypatch.setattr(cli, "verify_preregistration", _invalid_verify)

    rc = cli.main(["--repo-root", str(tmp_path)])
    payload = _read_final_payload(tmp_path)

    assert rc == 1
    assert payload["run_status"] == "invalid"
    assert payload["verdict"] == VERDICT_INCONCLUSIVE
    assert payload["verdict_reasons"] == [REASON_PREREGISTRATION_INVALID]
    assert payload["preregistration"]["valid"] is False
    assert payload["lineage"]["consistent"] is False
    assert payload["adr_0002"]["promoted"] is False
    assert ADR_REASON_INVALID_LINEAGE in payload["adr_0002"]["block_reasons"]
    assert not (tmp_path / ADR_REL).exists()


def test_cli_no_go_report_references_valid_on_disk_prereg_and_writes_no_adr(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cli = _load_cli()
    prereg = _write_prereg(tmp_path)
    _write_breakeven(tmp_path, a1_decision="NO_GO", branch_decision="A2")
    _write_pivot(tmp_path)
    monkeypatch.setattr(cli, "verify_preregistration", _valid_verify)

    rc = cli.main(["--repo-root", str(tmp_path)])
    payload = _read_final_payload(tmp_path)

    assert rc == 0
    assert payload["run_status"] == "valid"
    assert payload["verdict"] == VERDICT_NO_GO
    assert payload["preregistration"]["valid"] is True
    assert payload["preregistration"]["file_sha256"] == hashlib.sha256(
        prereg.read_bytes()
    ).hexdigest()
    assert payload["adr_0002"]["promoted"] is False
    assert not (tmp_path / ADR_REL).exists()


def test_cli_clean_heldout_go_writes_adr_and_go_verdict(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cli = _load_cli()
    _write_prereg(tmp_path)
    _write_breakeven(tmp_path, a1_decision="GO", branch_decision="A1")
    _write_pivot(tmp_path)
    _write_walk_forward(tmp_path, clean_heldout_go=True)
    monkeypatch.setattr(cli, "verify_preregistration", _valid_verify)

    rc = cli.main(["--repo-root", str(tmp_path)])
    payload = _read_final_payload(tmp_path)
    adr_path = tmp_path / ADR_REL

    assert rc == 0
    assert payload["run_status"] == "valid"
    assert payload["verdict"] == VERDICT_GO
    assert payload["walk_forward"]["clean_heldout_go"] is True
    assert payload["adr_0002"]["promoted"] is True
    assert payload["adr_0002"]["block_reasons"] == []
    assert adr_path.exists()
    assert "ADR-0002" in adr_path.read_text(encoding="utf-8")


def test_cli_report_content_hash_is_stable_for_identical_inputs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cli = _load_cli()
    _write_prereg(tmp_path)
    _write_breakeven(tmp_path, a1_decision="NO_GO", branch_decision="A2")
    _write_pivot(tmp_path)
    monkeypatch.setattr(cli, "verify_preregistration", _valid_verify)

    first_rc = cli.main(["--repo-root", str(tmp_path)])
    first_payload = _read_final_payload(tmp_path)
    second_rc = cli.main(["--repo-root", str(tmp_path)])
    second_payload = _read_final_payload(tmp_path)

    assert first_rc == 0
    assert second_rc == 0
    assert first_payload["content_hash"] == second_payload["content_hash"]
    assert first_payload["content_hash"] == cli._canonical_sha256(
        _content_hash_body(first_payload)
    )
    assert second_payload["content_hash"] == cli._canonical_sha256(
        _content_hash_body(second_payload)
    )


def test_cli_lineage_tampering_in_any_upstream_report_is_inconclusive(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cli = _load_cli()
    _write_prereg(tmp_path)
    _write_breakeven(
        tmp_path,
        a1_decision="GO",
        branch_decision="A1",
        btc_run_status="invalid",
    )
    _write_pivot(tmp_path)
    _write_walk_forward(tmp_path, clean_heldout_go=True)
    monkeypatch.setattr(cli, "verify_preregistration", _valid_verify)

    rc = cli.main(["--repo-root", str(tmp_path)])
    payload = _read_final_payload(tmp_path)

    assert rc == 0
    assert payload["lineage"]["consistent"] is False
    assert payload["breakeven"]["lineage"][0]["run_status"] == "invalid"
    assert payload["verdict"] == VERDICT_INCONCLUSIVE
    assert payload["verdict_reasons"] == [REASON_INVALID_LINEAGE]
    assert payload["adr_0002"]["promoted"] is False
    assert payload["adr_0002"]["block_reasons"] == [
        ADR_REASON_NO_CLEAN_HELDOUT_GO,
        ADR_REASON_INVALID_LINEAGE,
    ]
    assert not (tmp_path / ADR_REL).exists()

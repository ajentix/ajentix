"""Tests for the strategy-v2 G004 final-verdict aggregation + ADR-0002 gate.

The pure decision/gate functions are tested directly; the CLI is driven end-to-end over
synthetic fixtures in a tmp repo (CI-safe: no network, no committed reports/), with the
pre-registration crypto stubbed because lineage hashing is covered by the
preregistration tests. The two goal-mandated assertions are:
  1. the final report references a valid pre-registration sha, and
  2. ADR-0002 exists ONLY when there is a clean pre-registered held-out GO.
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
    ADR_REASON_NO_CLEAN_HELDOUT_GO,
    REASON_A1_NO_GO_ALL_SYMBOLS,
    REASON_NO_A2_PIVOT_CANDIDATE_CLEARS,
    REASON_NO_WALK_FORWARD_HELDOUT_RUN,
    REASON_PREREGISTRATION_INVALID,
    VERDICT_GO,
    VERDICT_INCONCLUSIVE,
    VERDICT_NO_GO,
    VERDICT_PIVOT_CANDIDATE_CLEARED,
    VerdictInputs,
    decide_final_verdict,
    should_promote_adr_0002,
    summarize_breakeven,
    summarize_pivot,
)
from ajentix_quant.research.preregistration import VerifyResult

REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPT = REPO_ROOT / "scripts" / "run_final_verdict.py"
ADR_REL = Path("docs") / "adr" / "0002-strategy-v2-hard-performance-gate.md"


def _load_cli() -> Any:
    spec = importlib.util.spec_from_file_location("run_final_verdict_under_test", SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _inputs(**overrides: Any) -> VerdictInputs:
    base: dict[str, Any] = {
        "preregistration_valid": True,
        "lineage_consistent": True,
        "a1_go_symbols": (),
        "a1_no_go_symbols": ("BTC/USDT:USDT", "ETH/USDT:USDT"),
        "a1_inconclusive_symbols": (),
        "walk_forward_ran": False,
        "held_out_clean_go": False,
        "pivot_ran": True,
        "pivot_any_clears": False,
        "fold_collapse": False,
        "concentration_failure": False,
        "maker_only_dependence": False,
    }
    base.update(overrides)
    return VerdictInputs(**base)


# --- pure decision logic ---------------------------------------------------------------


def test_no_go_when_all_a1_no_go_and_no_pivot_clears() -> None:
    inp = _inputs()
    verdict, reasons = decide_final_verdict(inp)
    assert verdict == VERDICT_NO_GO
    assert REASON_A1_NO_GO_ALL_SYMBOLS in reasons
    assert REASON_NO_WALK_FORWARD_HELDOUT_RUN in reasons
    assert REASON_NO_A2_PIVOT_CANDIDATE_CLEARS in reasons
    promoted, block = should_promote_adr_0002(verdict, inp)
    assert promoted is False
    assert ADR_REASON_NO_CLEAN_HELDOUT_GO in block


def test_go_only_with_clean_heldout_promotes_adr() -> None:
    inp = _inputs(
        a1_go_symbols=("BTC/USDT:USDT",),
        a1_no_go_symbols=(),
        walk_forward_ran=True,
        held_out_clean_go=True,
    )
    verdict, _ = decide_final_verdict(inp)
    assert verdict == VERDICT_GO
    promoted, block = should_promote_adr_0002(verdict, inp)
    assert promoted is True
    assert block == []


def test_adr_blocked_by_fold_collapse_even_if_go() -> None:
    # Defense-in-depth: an independent fold-collapse signal blocks ADR even on a claimed GO.
    inp = _inputs(
        a1_go_symbols=("BTC/USDT:USDT",),
        a1_no_go_symbols=(),
        walk_forward_ran=True,
        held_out_clean_go=True,
        fold_collapse=True,
        concentration_failure=True,
    )
    verdict, _ = decide_final_verdict(inp)
    promoted, block = should_promote_adr_0002(verdict, inp)
    assert promoted is False
    assert ADR_REASON_FOLD_COLLAPSE in block
    assert ADR_REASON_CONCENTRATION_FAILURE in block


def test_invalid_prereg_is_inconclusive() -> None:
    verdict, reasons = decide_final_verdict(_inputs(preregistration_valid=False))
    assert verdict == VERDICT_INCONCLUSIVE
    assert reasons == [REASON_PREREGISTRATION_INVALID]


def test_inconsistent_lineage_is_inconclusive() -> None:
    verdict, _ = decide_final_verdict(_inputs(lineage_consistent=False))
    assert verdict == VERDICT_INCONCLUSIVE


def test_pivot_cleared_is_not_a_go_and_does_not_promote_adr() -> None:
    inp = _inputs(pivot_any_clears=True)
    verdict, _ = decide_final_verdict(inp)
    assert verdict == VERDICT_PIVOT_CANDIDATE_CLEARED
    promoted, block = should_promote_adr_0002(verdict, inp)
    assert promoted is False
    assert ADR_REASON_NO_CLEAN_HELDOUT_GO in block


def test_summarize_breakeven_routes_by_decision() -> None:
    reports = [
        {
            "scenario_id": "btc_v1",
            "branch_summary": {
                "by_symbol": {
                    "BTC/USDT:USDT": {
                        "a1_decision": "NO_GO",
                        "branch_decision": "A2",
                        "reason_codes": ["H21_PRIMARY_A1_BAR_NOT_MET"],
                    },
                    "ETH/USDT:USDT": {
                        "a1_decision": "GO",
                        "branch_decision": "A1",
                        "reason_codes": [],
                    },
                }
            },
            "breakeven": {"funding_rows_insample": 1096},
        }
    ]
    summary = summarize_breakeven(reports)
    assert summary["a1_go_symbols"] == ["ETH/USDT:USDT"]
    assert summary["a1_no_go_symbols"] == ["BTC/USDT:USDT"]
    assert summary["branch_by_symbol"]["BTC/USDT:USDT"] == "A2"


def test_summarize_pivot_extracts_concentration_metrics() -> None:
    report = {
        "overall": {
            "any_candidate_clears": False,
            "clearing_candidate_ids": [],
            "conclusion": "no evidence-supported pivot candidate yet",
        },
        "candidates": [
            {
                "candidate_id": "HL_DIRECT_STBL",
                "clears": False,
                "max_single_week_share": 0.6726,
                "primary_slippage_bps_per_leg": 8.5255,
                "reason_codes": ["WEEKLY_CONCENTRATION_TOO_HIGH"],
            }
        ],
    }
    summary = summarize_pivot(report)
    assert summary["any_candidate_clears"] is False
    assert summary["candidates"][0]["max_single_week_share"] == 0.6726


# --- CLI end-to-end over synthetic fixtures --------------------------------------------


def _write_prereg(repo: Path, run_id: str = "stratv2-testrun001") -> Path:
    prereg_dir = repo / "docs" / "preregistration"
    prereg_dir.mkdir(parents=True, exist_ok=True)
    path = prereg_dir / f"{run_id}.json"
    path.write_text(
        json.dumps(
            {
                "run_id": run_id,
                "content_hash": "deadbeefcafef00d",
                "plan": {
                    "trial_budget": {
                        "max_primary_train_trials": 224,
                        "max_primary_heldout_evals": 14,
                        "max_secondary_sensitivity_evals": 42,
                        "total_heldout_cap": 56,
                    }
                },
            }
        ),
        encoding="utf-8",
    )
    return path


def _write_breakeven(repo: Path, *, a1_decision: str, branch: str) -> None:
    reports = repo / "reports"
    reports.mkdir(parents=True, exist_ok=True)
    by_symbol = {
        sym: {
            "a1_decision": a1_decision,
            "branch_decision": branch,
            "reason_codes": ["R"],
        }
        for sym in ("BTC/USDT:USDT", "ETH/USDT:USDT")
    }
    for stem in ("btc", "eth"):
        payload = {
            "run_status": "valid",
            "run_id": "stratv2-be",
            "content_hash": "be",
            "preregistration_sha256": "besha",
            "scenario_id": f"{stem}_v1",
            "branch_summary": {"by_symbol": by_symbol},
            "breakeven": {"funding_rows_insample": 1096},
        }
        (reports / f"breakeven_{stem}_v2.json").write_text(
            json.dumps(payload), encoding="utf-8"
        )


def _write_pivot(repo: Path, *, any_clears: bool) -> None:
    reports = repo / "reports"
    reports.mkdir(parents=True, exist_ok=True)
    payload = {
        "run_status": "valid",
        "run_id": "stratv2-pv",
        "content_hash": "pv",
        "preregistration_sha256": "pvsha",
        "overall": {
            "any_candidate_clears": any_clears,
            "clearing_candidate_ids": (["HL_DIRECT_X"] if any_clears else []),
            "conclusion": "synthetic",
        },
        "candidates": [
            {
                "candidate_id": "HL_DIRECT_X",
                "candidate_type": "hl_direct_funding",
                "symbol": "X/USDC:USDC",
                "venue": "hyperliquid",
                "clears": any_clears,
                "qualifying_24h_window_pct": 0.05,
                "qualifying_24h_window_count": 10,
                "cluster_count": 3,
                "max_single_week_share": 0.67,
                "primary_slippage_bps_per_leg": 8.5,
                "reason_codes": ["WEEKLY_CONCENTRATION_TOO_HIGH"],
            }
        ],
    }
    (reports / "pivot_venue_feasibility_v2.json").write_text(
        json.dumps(payload), encoding="utf-8"
    )


def _valid_verify(*_args: Any, **_kwargs: Any) -> VerifyResult:
    return VerifyResult(valid=True, run_status="valid", mismatches=())


def test_cli_no_go_references_valid_prereg_and_omits_adr(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cli = _load_cli()
    prereg = _write_prereg(tmp_path)
    _write_breakeven(tmp_path, a1_decision="NO_GO", branch="A2")
    _write_pivot(tmp_path, any_clears=False)
    monkeypatch.setattr(cli, "verify_preregistration", _valid_verify)

    rc = cli.main(["--repo-root", str(tmp_path)])
    assert rc == 0

    payload = json.loads(
        (tmp_path / "reports" / "strategy_v2_final_verdict.json").read_text()
    )
    assert payload["verdict"] == VERDICT_NO_GO
    # (1) references a VALID pre-registration sha matching the on-disk artifact
    assert payload["preregistration"]["valid"] is True
    assert payload["run_status"] == "valid"
    assert (
        payload["preregistration"]["file_sha256"]
        == hashlib.sha256(prereg.read_bytes()).hexdigest()
    )
    # (2) ADR-0002 absent without a clean held-out GO
    assert payload["adr_0002"]["promoted"] is False
    assert not (tmp_path / ADR_REL).exists()


def test_cli_clean_heldout_go_promotes_adr(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cli = _load_cli()
    _write_prereg(tmp_path)
    _write_breakeven(tmp_path, a1_decision="GO", branch="A1")
    _write_pivot(tmp_path, any_clears=False)
    (tmp_path / "reports" / "walk_forward_btc.json").write_text(
        json.dumps(
            {
                "run_status": "valid",
                "run_id": "stratv2-wf",
                "clean_heldout_go": True,
                "fold_collapse": False,
                "concentration_failure": False,
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(cli, "verify_preregistration", _valid_verify)

    rc = cli.main(["--repo-root", str(tmp_path)])
    assert rc == 0

    payload = json.loads(
        (tmp_path / "reports" / "strategy_v2_final_verdict.json").read_text()
    )
    assert payload["verdict"] == VERDICT_GO
    assert payload["adr_0002"]["promoted"] is True
    assert (tmp_path / ADR_REL).exists()


def test_cli_invalid_prereg_is_inconclusive_and_omits_adr(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cli = _load_cli()
    _write_prereg(tmp_path)
    _write_breakeven(tmp_path, a1_decision="NO_GO", branch="A2")
    _write_pivot(tmp_path, any_clears=False)

    def _invalid(*_args: Any, **_kwargs: Any) -> VerifyResult:
        return VerifyResult(
            valid=False, run_status="invalid", mismatches=("source hash drift: x",)
        )

    monkeypatch.setattr(cli, "verify_preregistration", _invalid)

    rc = cli.main(["--repo-root", str(tmp_path)])
    assert rc == 1

    payload = json.loads(
        (tmp_path / "reports" / "strategy_v2_final_verdict.json").read_text()
    )
    assert payload["run_status"] == "invalid"
    assert payload["verdict"] == VERDICT_INCONCLUSIVE
    assert payload["adr_0002"]["promoted"] is False
    assert not (tmp_path / ADR_REL).exists()


def test_not_applicable_walk_forward_does_not_register_failures() -> None:
    # When no walk-forward ran, the "not_applicable" sentinels must NOT count as
    # fold-collapse / concentration failures; the only ADR block reason is NO_CLEAN_HELDOUT_GO.
    from ajentix_quant.research.final_verdict import build_verdict_inputs

    inp = build_verdict_inputs(
        preregistration_valid=True,
        lineage_consistent=True,
        breakeven_summary={
            "a1_go_symbols": [],
            "a1_no_go_symbols": ["BTC/USDT:USDT", "ETH/USDT:USDT"],
            "a1_inconclusive_symbols": [],
        },
        walk_forward={
            "ran": False,
            "clean_heldout_go": False,
            "fold_collapse": "not_applicable",
            "concentration_failure": "not_applicable",
        },
        pivot_summary={"ran": True, "any_candidate_clears": False},
    )
    assert inp.fold_collapse is False
    assert inp.concentration_failure is False
    verdict, _ = decide_final_verdict(inp)
    promoted, block = should_promote_adr_0002(verdict, inp)
    assert promoted is False
    assert block == [ADR_REASON_NO_CLEAN_HELDOUT_GO]

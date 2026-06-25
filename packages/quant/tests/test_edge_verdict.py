from __future__ import annotations

import contextlib
import io
import json
from dataclasses import replace
from pathlib import Path

import pytest
from scripts import run_edge_verdict

from ajentix_quant.backtest.verdict import (
    EdgeVerdictReport,
    EdgeVerdictThresholds,
    Verdict,
    decide_verdict,
    net_apr_simple,
)
from ajentix_quant.data.cache import load_dataset

EDGE_FIXTURE_ROOT = Path("tests/fixtures/edge")
EDGE_SCENARIO_ID = "edge_demo_v1"


def _run_main(argv: list[str]) -> tuple[int, str]:
    stdout = io.StringIO()
    with contextlib.redirect_stdout(stdout):
        exit_code = run_edge_verdict.main(argv)
    return exit_code, stdout.getvalue()


def test_decide_verdict_go_only_with_venue_thresholds_and_no_collapse() -> None:
    verdict, reasons = decide_verdict(
        sharpe=1.6,
        mdd=0.04,
        net_apr=0.01,
        all_streams_venue=True,
        train_test_valid=True,
        test_periods=30,
        min_test_periods=30,
        collapse=False,
        thresholds=EdgeVerdictThresholds(),
    )

    assert verdict is Verdict.GO
    assert reasons == []


def test_decide_verdict_no_go_when_venue_thresholds_not_met() -> None:
    verdict, reasons = decide_verdict(
        sharpe=1.49,
        mdd=0.051,
        net_apr=-0.001,
        all_streams_venue=True,
        train_test_valid=True,
        test_periods=31,
        min_test_periods=30,
        collapse=False,
        thresholds=EdgeVerdictThresholds(),
    )

    assert verdict is Verdict.NO_GO
    assert any("sharpe" in reason for reason in reasons)
    assert any("mdd" in reason for reason in reasons)
    assert any("net_apr" in reason for reason in reasons)


def test_decide_verdict_no_go_on_collapse_even_when_thresholds_pass() -> None:
    verdict, reasons = decide_verdict(
        sharpe=2.0,
        mdd=0.02,
        net_apr=0.05,
        all_streams_venue=True,
        train_test_valid=True,
        test_periods=30,
        min_test_periods=30,
        collapse=True,
        thresholds=EdgeVerdictThresholds(),
    )

    assert verdict is Verdict.NO_GO
    assert reasons == ["test-vs-train performance collapse"]


@pytest.mark.parametrize(
    ("all_streams_venue", "train_test_valid", "test_periods", "expected"),
    [
        (False, True, 30, "real venue data required"),
        (True, False, 30, "train/test split required"),
        (True, True, 29, "insufficient test window"),
    ],
)
def test_decide_verdict_inconclusive_branches(
    all_streams_venue: bool,
    train_test_valid: bool,
    test_periods: int,
    expected: str,
) -> None:
    verdict, reasons = decide_verdict(
        sharpe=9.0,
        mdd=0.0,
        net_apr=1.0,
        all_streams_venue=all_streams_venue,
        train_test_valid=train_test_valid,
        test_periods=test_periods,
        min_test_periods=30,
        collapse=False,
        thresholds=EdgeVerdictThresholds(),
    )

    assert verdict is Verdict.INCONCLUSIVE
    assert any(expected in reason for reason in reasons)


def test_edge_demo_fixture_is_honestly_inconclusive_because_fixture_quality(tmp_path: Path) -> None:
    out_prefix = tmp_path / "edge_demo"
    exit_code, stdout = _run_main(
        [
            "--cache-root",
            str(EDGE_FIXTURE_ROOT),
            "--scenario-id",
            EDGE_SCENARIO_ID,
            "--out",
            str(out_prefix),
        ]
    )

    assert exit_code == 0
    assert "INCONCLUSIVE" in stdout
    payload = json.loads((tmp_path / "edge_demo.json").read_text(encoding="utf-8"))
    assert (tmp_path / "edge_demo.md").is_file()
    assert payload["verdict"] == Verdict.INCONCLUSIVE.value
    assert payload["verdict"] != Verdict.GO.value
    assert any(
        "real venue data required" in reason and "fixture" in reason
        for reason in payload["reasons"]
    )
    assert payload["source_quality"]["funding_history"] == "fixture"


def test_absent_cache_produces_inconclusive_report_exit_zero(tmp_path: Path) -> None:
    out_prefix = tmp_path / "absent"
    exit_code, stdout = _run_main(
        [
            "--cache-root",
            str(tmp_path / "missing-root"),
            "--scenario-id",
            "missing_real_v1",
            "--out",
            str(out_prefix),
        ]
    )

    assert exit_code == 0
    assert "INCONCLUSIVE" in stdout
    payload = json.loads((tmp_path / "absent.json").read_text(encoding="utf-8"))
    markdown = (tmp_path / "absent.md").read_text(encoding="utf-8")
    assert payload["verdict"] == Verdict.INCONCLUSIVE.value
    assert any("populate_bybit_cache" in reason for reason in payload["reasons"])
    assert payload["schema_version"] == "aq-cache-v1"
    assert "populate_bybit_cache" in markdown


def test_train_param_selection_does_not_read_test_window_rows() -> None:
    dataset = load_dataset(EDGE_FIXTURE_ROOT, EDGE_SCENARIO_ID)
    assert dataset.train_until_ms is not None
    baseline = run_edge_verdict.select_strategy_params(dataset, equity_usd=1_000.0)

    mutated_funding = {}
    for symbol, rows in dataset.funding.items():
        mutated_funding[symbol] = tuple(
            replace(
                row,
                rate=row.rate * 100.0 if row.timestamp > dataset.train_until_ms else row.rate,
            )
            for row in rows
        )
    mutated = replace(dataset, funding=mutated_funding)
    changed = run_edge_verdict.select_strategy_params(mutated, equity_usd=1_000.0)

    assert changed.params == baseline.params
    assert changed.param_freeze_hash == baseline.param_freeze_hash


def test_report_json_and_markdown_format() -> None:
    report = EdgeVerdictReport(
        scenario_id="unit",
        schema_version="aq-cache-v1",
        manifest_sha256="abc123",
        generator_version="unit-gen",
        verdict=Verdict.INCONCLUSIVE,
        reasons=["real venue data required: funding_history=fixture"],
        equity_usd=1_000.0,
        per_setup_notional_usd=250.0,
        train_until_ms=123,
        param_freeze_hash="freeze",
        source_quality={"funding_history": "fixture"},
        selected_params={"min_funding_rate_8h": 0.0001},
        train_metrics={"sharpe": 1.0, "sortino": 1.0, "mdd": 0.0, "net_apr": 0.1},
        test_metrics={"sharpe": 0.5, "sortino": 0.5, "mdd": 0.01, "net_apr": 0.02},
        event_counts={"train": {"entry": 1}, "test": {"funding": 2}},
        liquidated=False,
        sensitivity=[{"case": "min_funding_rate_8h_+25pct", "net_apr_delta": -0.01}],
        caveats=["GO is impossible without source_quality=VENUE data."],
    )

    payload = report.to_json()
    assert {
        "scenario_id",
        "schema_version",
        "manifest_sha256",
        "generator_version",
        "verdict",
        "reasons",
        "equity_usd",
        "per_setup_notional_usd",
        "train_until_ms",
        "param_freeze_hash",
        "source_quality",
        "selected_params",
        "train_metrics",
        "test_metrics",
        "event_counts",
        "liquidated",
        "sensitivity",
        "caveats",
    } <= set(payload)
    markdown = report.to_markdown()
    assert markdown.strip()
    assert "INCONCLUSIVE" in markdown
    assert "TEST metrics" in markdown
    assert "Caveats" in markdown


def test_net_apr_formula_is_simple_non_compounded_8h_annualization() -> None:
    assert net_apr_simple(
        final_equity=1_100.0,
        initial_equity=1_000.0,
        n_test_periods=10,
    ) == pytest.approx(0.10 * (365 * 3 / 10))

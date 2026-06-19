from __future__ import annotations

import contextlib
import io
import json
from dataclasses import replace
from pathlib import Path

import pytest
from scripts.run_edge_verdict import (
    build_edge_verdict_report,
    main,
    net_apr_simple,
    select_strategy_params,
)

from ajentix_quant.backtest.verdict import EdgeVerdictThresholds, Verdict, decide_verdict
from ajentix_quant.data.cache import load_dataset

EDGE_FIXTURE_ROOT = Path("tests/fixtures/edge")
EDGE_SCENARIO_ID = "edge_demo_v1"
EQUITY_USD = 1_000.0


def _run_main(argv: list[str]) -> tuple[int, str]:
    stdout = io.StringIO()
    with contextlib.redirect_stdout(stdout):
        exit_code = main(argv)
    return exit_code, stdout.getvalue()


def _json_at(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def _decide(**overrides: object) -> tuple[Verdict, list[str]]:
    args = {
        "sharpe": 10.0,
        "mdd": 0.001,
        "net_apr": 2.0,
        "all_streams_venue": True,
        "train_test_valid": True,
        "test_periods": 120,
        "min_test_periods": 30,
        "collapse": False,
        "thresholds": EdgeVerdictThresholds(),
    }
    args.update(overrides)
    return decide_verdict(**args)  # type: ignore[arg-type]


def test_fabricated_go_attack_non_venue_perfect_metrics_stays_inconclusive() -> None:
    verdict, reasons = _decide(all_streams_venue=False)

    assert verdict is Verdict.INCONCLUSIVE
    assert verdict is not Verdict.GO
    assert any("real venue data required" in reason for reason in reasons)


@pytest.mark.parametrize(
    "overrides",
    [
        {},
        {"collapse": True},
        {"train_test_valid": False},
        {"test_periods": 0},
        {"sharpe": 999.0, "mdd": 0.0, "net_apr": 999.0},
    ],
)
def test_non_venue_sweep_never_returns_go(overrides: dict[str, object]) -> None:
    verdict, reasons = _decide(all_streams_venue=False, **overrides)

    assert verdict is Verdict.INCONCLUSIVE
    assert verdict is not Verdict.GO
    assert reasons


def test_venue_perfect_metrics_can_return_go() -> None:
    verdict, reasons = _decide(all_streams_venue=True)

    assert verdict is Verdict.GO
    assert reasons == []


@pytest.mark.parametrize(
    ("label", "overrides", "expected_verdict", "reason_fragment"),
    [
        ("low-sharpe", {"sharpe": 1.499999}, Verdict.NO_GO, "sharpe"),
        ("mdd-breach", {"mdd": 0.050001}, Verdict.NO_GO, "mdd"),
        ("negative-apr", {"net_apr": -0.000001}, Verdict.NO_GO, "net_apr"),
        ("collapse", {"collapse": True}, Verdict.NO_GO, "collapse"),
        ("all-good", {}, Verdict.GO, ""),
    ],
)
def test_threshold_branches_with_venue_data(
    label: str,
    overrides: dict[str, object],
    expected_verdict: Verdict,
    reason_fragment: str,
) -> None:
    verdict, reasons = _decide(**overrides)

    assert verdict is expected_verdict, label
    if reason_fragment:
        assert any(reason_fragment in reason for reason in reasons), (label, reasons)
    else:
        assert reasons == []


@pytest.mark.parametrize(
    ("overrides", "reason_fragment"),
    [
        ({"train_test_valid": False}, "train/test split required"),
        ({"test_periods": 29}, "insufficient test window"),
    ],
)
def test_inconclusive_gates_precede_performance_decision(
    overrides: dict[str, object], reason_fragment: str
) -> None:
    verdict, reasons = _decide(**overrides)

    assert verdict is Verdict.INCONCLUSIVE
    assert verdict is not Verdict.GO
    assert any(reason_fragment in reason for reason in reasons)


def test_fixture_harness_cannot_emit_go_and_names_fixture_provenance(tmp_path: Path) -> None:
    out_prefix = tmp_path / "edge_demo_redteam"
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

    json_path = tmp_path / "edge_demo_redteam.json"
    md_path = tmp_path / "edge_demo_redteam.md"
    payload = _json_at(json_path)
    markdown = md_path.read_text(encoding="utf-8")
    reason_text = "\n".join(payload["reasons"])

    assert exit_code == 0
    assert "INCONCLUSIVE" in stdout
    assert payload["verdict"] == Verdict.INCONCLUSIVE.value
    assert payload["verdict"] != Verdict.GO.value
    assert "fixture" in reason_text
    assert "real venue data required" in reason_text
    assert payload["source_quality"]
    assert set(payload["source_quality"].values()) == {"fixture"}
    assert payload["scenario_id"] == EDGE_SCENARIO_ID
    assert payload["param_freeze_hash"]
    assert "INCONCLUSIVE" in markdown
    assert "fixture" in markdown


def test_absent_scenario_harness_is_inconclusive_exit_zero_with_reports(tmp_path: Path) -> None:
    out_dir = tmp_path / "out-dir"
    out_dir.mkdir()
    exit_code, stdout = _run_main(
        [
            "--cache-root",
            str(tmp_path / "absent-cache-root"),
            "--scenario-id",
            "absent_real_venue_v1",
            "--out",
            str(out_dir),
        ]
    )

    json_path = out_dir / "edge_verdict.json"
    md_path = out_dir / "edge_verdict.md"
    payload = _json_at(json_path)
    markdown = md_path.read_text(encoding="utf-8")
    reason_text = "\n".join(payload["reasons"])

    assert exit_code == 0
    assert "INCONCLUSIVE" in stdout
    assert payload["verdict"] == Verdict.INCONCLUSIVE.value
    assert payload["verdict"] != Verdict.GO.value
    assert payload["schema_version"] == "aq-cache-v1"
    assert payload["selected_params"]
    assert payload["source_quality"] == {}
    assert "populate_bybit_cache" in reason_text
    assert "populate_bybit_cache" in markdown
    assert markdown.startswith("# Stage-1 Edge Verdict")


def test_train_selection_ignores_test_funding_but_hash_tracks_train_rows() -> None:
    dataset = load_dataset(EDGE_FIXTURE_ROOT, EDGE_SCENARIO_ID)
    symbol = dataset.symbols[0]
    assert dataset.train_until_ms is not None

    baseline = select_strategy_params(dataset, equity_usd=EQUITY_USD, symbol=symbol)

    test_amplified_rows = tuple(
        replace(row, rate=row.rate * 100.0)
        if row.timestamp > dataset.train_until_ms
        else row
        for row in dataset.funding[symbol]
    )
    test_amplified = replace(
        dataset,
        funding={**dataset.funding, symbol: test_amplified_rows},
    )
    after_test_mutation = select_strategy_params(
        test_amplified, equity_usd=EQUITY_USD, symbol=symbol
    )

    assert after_test_mutation.params == baseline.params
    assert after_test_mutation.param_freeze_hash == baseline.param_freeze_hash

    first_train_idx = next(
        idx
        for idx, row in enumerate(dataset.funding[symbol])
        if row.timestamp <= dataset.train_until_ms
    )
    train_row = dataset.funding[symbol][first_train_idx]
    assert train_row.timestamp + 1 <= dataset.train_until_ms
    train_timestamp_mutated_rows = tuple(
        replace(row, timestamp=row.timestamp + 1) if idx == first_train_idx else row
        for idx, row in enumerate(dataset.funding[symbol])
    )
    train_timestamp_mutated = replace(
        dataset,
        funding={**dataset.funding, symbol: train_timestamp_mutated_rows},
    )
    after_train_mutation = select_strategy_params(
        train_timestamp_mutated, equity_usd=EQUITY_USD, symbol=symbol
    )

    assert after_train_mutation.param_freeze_hash != baseline.param_freeze_hash


def test_harness_report_json_is_deterministic_across_runs(tmp_path: Path) -> None:
    report_a = build_edge_verdict_report(
        EDGE_FIXTURE_ROOT,
        EDGE_SCENARIO_ID,
        equity_usd=EQUITY_USD,
        min_test_periods=30,
    ).to_json()
    report_b = build_edge_verdict_report(
        EDGE_FIXTURE_ROOT,
        EDGE_SCENARIO_ID,
        equity_usd=EQUITY_USD,
        min_test_periods=30,
    ).to_json()
    assert report_a == report_b

    for name in ("first", "second"):
        exit_code, _stdout = _run_main(
            [
                "--cache-root",
                str(EDGE_FIXTURE_ROOT),
                "--scenario-id",
                EDGE_SCENARIO_ID,
                "--out",
                str(tmp_path / name),
            ]
        )
        assert exit_code == 0

    assert _json_at(tmp_path / "first.json") == _json_at(tmp_path / "second.json")


def test_net_apr_hand_example_uses_simple_8h_annualization() -> None:
    assert net_apr_simple(
        final_equity=1_250.0,
        initial_equity=1_000.0,
        n_test_periods=73,
    ) == pytest.approx((1_250.0 / 1_000.0 - 1.0) * (1095 / 73))

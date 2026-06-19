from __future__ import annotations

import ast
import contextlib
import io
import math
from dataclasses import replace
from pathlib import Path

import pytest
from scripts import gen_stage1_structural_fixture as fixture_gen
from scripts import run_stage1_gate

from ajentix_quant.adapters.base import MarketType, PriceType, SourceQuality, StreamKey
from ajentix_quant.backtest import metrics
from ajentix_quant.data.cache import load_dataset, write_cache

PERF_FAILURE_NAMES = {
    "ann_return",
    "calmar",
    "max_drawdown",
    "mdd",
    "net_apr",
    "sharpe",
    "sortino",
}


def _names_in(node: ast.AST) -> set[str]:
    names: set[str] = set()
    for child in ast.walk(node):
        if isinstance(child, ast.Name):
            names.add(child.id)
        elif isinstance(child, ast.Attribute):
            names.add(child.attr)
    return names


def _contains_raise_or_assert(node: ast.AST) -> bool:
    return any(isinstance(child, ast.Raise | ast.Assert) for child in ast.walk(node))


def _write_structural_fixture_with_gap_multiplier(
    cache_root: Path,
    scenario_id: str,
    *,
    gap_multiplier: float,
) -> Path:
    funding, ohlcv, source_quality = fixture_gen.build_fixture_rows()
    mark_key = StreamKey(fixture_gen.SYMBOL, MarketType.LINEAR_PERP, PriceType.MARK)
    mark_rows = list(ohlcv[mark_key])
    gap_index = fixture_gen.GAP_INDEX
    previous_close = mark_rows[gap_index - 1].close
    mark_rows[gap_index] = replace(
        mark_rows[gap_index],
        high=round(previous_close * gap_multiplier, 2),
    )
    ohlcv = {**ohlcv, mark_key: tuple(mark_rows)}
    return write_cache(
        cache_root,
        scenario_id,
        venue=fixture_gen.VENUE,
        timeframe=fixture_gen.TIMEFRAME,
        funding=funding,
        ohlcv=ohlcv,
        source_quality=source_quality,
        param_freeze_hash=fixture_gen.PARAM_FREEZE_HASH,
    )


def _main_output(argv: list[str]) -> tuple[int, str]:
    stdout = io.StringIO()
    with contextlib.redirect_stdout(stdout):
        exit_code = run_stage1_gate.main(argv)
    return exit_code, stdout.getvalue()


def test_gate_source_has_no_performance_threshold_failure() -> None:
    source = Path(run_stage1_gate.__file__).read_text(encoding="utf-8")
    tree = ast.parse(source)

    perf_sensitive_checks: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Assert):
            names = _names_in(node.test)
            if names & PERF_FAILURE_NAMES:
                perf_sensitive_checks.append(ast.get_source_segment(source, node) or ast.dump(node))
        elif isinstance(node, ast.If) and _contains_raise_or_assert(node):
            names = _names_in(node.test)
            if names & PERF_FAILURE_NAMES:
                perf_sensitive_checks.append(ast.get_source_segment(source, node) or ast.dump(node))
        elif (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id == "_check"
        ):
            # _check(name, ok, detail) is the only structural-failure row constructor.
            ok_expr = node.args[1]
            names = _names_in(ok_expr)
            if names & PERF_FAILURE_NAMES:
                perf_sensitive_checks.append(ast.get_source_segment(source, node) or ast.dump(node))

    assert perf_sensitive_checks == []


def test_gate_pass_is_independent_of_bad_printed_performance_metrics(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(metrics, "sharpe", lambda returns, periods_per_year, rf=0.0: -999.0)
    monkeypatch.setattr(metrics, "sortino", lambda returns, periods_per_year, rf=0.0: -888.0)
    monkeypatch.setattr(metrics, "annualized_return", lambda returns, periods_per_year: -0.99)
    monkeypatch.setattr(metrics, "max_drawdown", lambda equity_curve: 0.99)
    monkeypatch.setattr(metrics, "calmar", lambda ann_return, max_drawdown: -123.0)

    gate = run_stage1_gate.run_gate()

    assert gate.exit_code == 0
    assert gate.failures == ()
    assert "sharpe=-999.000000000000" in gate.canonical_report
    assert "sortino=-888.000000000000" in gate.canonical_report
    assert "net_apr=-0.990000000000" in gate.canonical_report
    assert "max_drawdown=0.990000000000" in gate.canonical_report
    assert "calmar=-123.000000000000" in gate.canonical_report
    assert gate.canonical_report.endswith("\nPASS")


def test_gate_canonical_report_and_golden_string_are_deterministic_in_process() -> None:
    first = run_stage1_gate.run_gate()
    second = run_stage1_gate.run_gate()

    assert first.exit_code == second.exit_code == 0
    assert first.result is not None
    assert second.result is not None
    assert first.canonical_report == second.canonical_report
    assert run_stage1_gate.golden_string(first.result) == run_stage1_gate.golden_string(
        second.result
    )


@pytest.mark.parametrize(
    ("scenario_id", "include_negative_regime", "include_gap", "expected_failure"),
    [
        ("no_negative_regime", False, True, "negative funding"),
        ("no_gap_regime", True, False, ">=15% perp-mark gap present"),
    ],
)
def test_structural_gate_fails_when_required_regime_is_absent(
    tmp_path: Path,
    scenario_id: str,
    include_negative_regime: bool,
    include_gap: bool,
    expected_failure: str,
) -> None:
    fixture_gen.write_structural_fixture(
        tmp_path,
        scenario_id,
        include_negative_regime=include_negative_regime,
        include_gap=include_gap,
    )

    gate = run_stage1_gate.run_gate(tmp_path, scenario_id, enforce_golden=False)
    main_exit_code, main_stdout = _main_output(
        ["--cache-root", str(tmp_path), "--scenario-id", scenario_id, "--no-golden"]
    )

    assert gate.exit_code == 1
    assert any(expected_failure in failure for failure in gate.failures)
    assert main_exit_code == 1
    assert "FAIL" in main_stdout
    assert expected_failure in main_stdout


def test_structural_gate_fails_when_over_cap_gap_forces_liquidation(tmp_path: Path) -> None:
    scenario_id = "liquidating_gap"
    _write_structural_fixture_with_gap_multiplier(
        tmp_path,
        scenario_id,
        gap_multiplier=2.0,
    )

    gate = run_stage1_gate.run_gate(tmp_path, scenario_id, enforce_golden=False)
    main_exit_code, main_stdout = _main_output(
        ["--cache-root", str(tmp_path), "--scenario-id", scenario_id, "--no-golden"]
    )

    assert gate.result is not None
    assert gate.exit_code == 1
    assert gate.result.liquidated is True
    assert gate.result.n_liquidations >= 1
    assert any("no liquidation" in failure for failure in gate.failures)
    assert main_exit_code == 1
    assert "liquidated=True" in main_stdout
    assert "FAIL" in main_stdout


def test_metric_edge_cases_required_by_g007() -> None:
    assert metrics.calmar(0.12, 0.0) == math.inf
    assert metrics.calmar(-0.12, 0.0) == -math.inf
    assert metrics.calmar(0.0, 0.0) == 0.0
    assert metrics.win_rate([]) == 0.0
    assert metrics.win_rate([0.0, -0.01]) == 0.0
    assert metrics.win_rate([0.01, 0.02, 0.03]) == 1.0
    assert metrics.funding_capture(10.0, 0.0) == 0.0
    assert metrics.funding_capture(-2.0, 4.0) == -0.5
    assert metrics.max_abs_net_delta_frac([]) == 0.0


def test_committed_structural_fixture_loads_offline_and_contains_required_regimes() -> None:
    dataset = load_dataset(
        run_stage1_gate.FIXTURE_ROOT,
        run_stage1_gate.SCENARIO_ID,
    )

    assert set(dataset.source_quality.values()) == {SourceQuality.FIXTURE}
    assert all(row.rate < 0.0 for row in dataset.funding[run_stage1_gate.SYMBOL][12:14])
    assert any(row.rate < 0.0 for row in dataset.funding[run_stage1_gate.SYMBOL])

    mark_key = StreamKey(
        run_stage1_gate.SYMBOL,
        MarketType.LINEAR_PERP,
        PriceType.MARK,
    )
    mark_rows = sorted(dataset.ohlcv[mark_key], key=lambda candle: candle.timestamp_ms)
    gaps = [
        current.high / previous.close - 1.0
        for previous, current in zip(mark_rows, mark_rows[1:], strict=False)
        if previous.close > 0.0
    ]

    assert max(gaps) >= run_stage1_gate.GAP_DETECTION_FLOOR

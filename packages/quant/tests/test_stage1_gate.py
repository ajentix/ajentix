from __future__ import annotations

from scripts import gen_stage1_structural_fixture as fixture_gen
from scripts import run_stage1_gate


def test_structural_gate_passes_on_fixture() -> None:
    assert run_stage1_gate.main([]) == 0


def test_gate_is_deterministic() -> None:
    first = run_stage1_gate.run_gate()
    second = run_stage1_gate.run_gate()

    assert first.exit_code == 0
    assert second.exit_code == 0
    assert first.canonical_report == second.canonical_report


def test_gate_fails_when_negative_funding_regime_absent(tmp_path) -> None:
    fixture_gen.write_structural_fixture(
        tmp_path,
        "no_negative_regime",
        include_negative_regime=False,
    )

    gate = run_stage1_gate.run_gate(
        tmp_path,
        "no_negative_regime",
        enforce_golden=False,
    )

    assert gate.exit_code == 1
    assert any("negative funding" in failure for failure in gate.failures)


def test_gate_golden_master_event_count_and_final_equity() -> None:
    gate = run_stage1_gate.run_gate()

    assert gate.exit_code == 0
    assert gate.canonical_golden == run_stage1_gate.EXPECTED_GOLDEN
    assert gate.canonical_golden == (
        "events_total=43;events_by_kind=deleverage:1,entry:2,exit:1,funding:39;"
        "final_equity=1013.46188825"
    )

"""G001 red-team: the strategy-v2 pre-registration lineage must be ungameable.

Verifies that tampering ANY frozen field is detected, that a tampered artifact cannot be
smuggled past verification (which recomputes from the live repo), and that the previously
found gap (a load-bearing transitive module like metrics.py changing undetected) is CLOSED
now that the run identity freezes the whole src/ajentix_quant package + scripts.
"""

import copy
import shutil
from pathlib import Path

import pytest

from ajentix_quant.research import preregistration as prereg

REPO_ROOT = Path(__file__).resolve().parents[1]


def _committed_artifact() -> Path:
    arts = sorted((REPO_ROOT / "docs" / "preregistration").glob("stratv2-*.json"))
    assert arts, "no committed strategy-v2 pre-registration artifact"
    return arts[0]


def _build():
    return prereg.build_preregistration(REPO_ROOT)


def test_build_deterministic_and_fresh_verify_valid():
    a, b = _build(), _build()
    assert a == b
    assert prereg.verify_preregistration(a, REPO_ROOT).valid is True


@pytest.mark.parametrize(
    "tamper",
    [
        lambda a: a["source_hashes"].__setitem__(next(iter(a["source_hashes"])), "0" * 64),
        lambda a: a["settings_snapshot"].__setitem__("reserve_pct", 0.99),
        lambda a: a["plan"]["folds"][0].__setitem__("test_end", "2099-01-01T00:00:00Z"),
        lambda a: a["plan"]["grid"].__setitem__("candidates_per_fold_per_symbol", 9999),
        lambda a: a["plan"]["a1_bar"].__setitem__("min_qualifying_pct", 0.0),
        lambda a: a["plan"]["trial_budget"].__setitem__("total_heldout_cap", 100000),
        lambda a: a["cache_manifest_sha256"].__setitem__(
            next(iter(a["cache_manifest_sha256"])), "deadbeef"
        ),
        lambda a: a.__setitem__("schema_version", "stratv2-prereg-v999"),
        lambda a: a.__setitem__("run_id", "stratv2-000000000000"),
    ],
)
def test_each_frozen_surface_tamper_is_detected(tamper):
    art = copy.deepcopy(_build())
    tamper(art)
    result = prereg.verify_preregistration(art, REPO_ROOT)
    assert result.valid is False
    assert result.run_status == "invalid"
    assert result.mismatches


def test_internally_consistent_forgery_still_invalid():
    # An attacker weakens a frozen plan constant AND recomputes the artifact's OWN
    # content_hash + run_id to be self-consistent. verify must STILL reject it, because it
    # recomputes from the LIVE repo, not from the artifact's stored content.
    art = copy.deepcopy(_build())
    art["plan"]["a1_bar"]["min_qualifying_windows"] = 1
    skip = ("schema_version", "run_id", "content_hash")
    forged_content = {k: art[k] for k in art if k not in skip}
    forged_hash = prereg._canonical_hash(forged_content)  # attacker self-consistent hash
    art["content_hash"] = forged_hash
    art["run_id"] = f"stratv2-{forged_hash[:12]}"
    result = prereg.verify_preregistration(art, REPO_ROOT)
    assert result.valid is False  # live-repo recompute defeats the forgery


def test_committed_artifact_verifies_valid():
    art = prereg.load_preregistration(_committed_artifact())
    assert prereg.verify_preregistration(art, REPO_ROOT).valid is True


def test_load_errors(tmp_path):
    with pytest.raises(prereg.PreregistrationError):
        prereg.load_preregistration(tmp_path / "nope.json")
    bad = tmp_path / "bad.json"
    bad.write_text("not json{", encoding="utf-8")
    with pytest.raises(prereg.PreregistrationError):
        prereg.load_preregistration(bad)


def test_gap_closed_unfrozen_transitive_module_mutation_is_detected(tmp_path):
    # GAP-CLOSE PROOF: metrics.py is load-bearing for the verdict (engine + run_edge_verdict
    # import it). Freezing the whole package means mutating it MUST invalidate the run by
    # DEFAULT (no extra_source_files needed) — the previously found hole is closed.
    run_edge = (REPO_ROOT / "scripts/run_edge_verdict.py").read_text(encoding="utf-8")
    engine = (REPO_ROOT / "src/ajentix_quant/backtest/engine.py").read_text(encoding="utf-8")
    assert "metrics" in run_edge and "metrics" in engine  # it is genuinely load-bearing

    tmp_repo = tmp_path / "repo"
    shutil.copytree(REPO_ROOT / "src", tmp_repo / "src")
    shutil.copytree(REPO_ROOT / "scripts", tmp_repo / "scripts")

    baseline = prereg.build_preregistration(tmp_repo)
    assert prereg.verify_preregistration(baseline, tmp_repo).valid is True

    metrics_path = tmp_repo / "src/ajentix_quant/backtest/metrics.py"
    original = metrics_path.read_text(encoding="utf-8")
    mutated = original.replace(
        "return (mean - rf) / std * math.sqrt(periods_per_year)",
        "return 999.0  # forged decision metric",
    )
    assert mutated != original
    metrics_path.write_text(mutated, encoding="utf-8")

    result = prereg.verify_preregistration(baseline, tmp_repo)
    assert result.valid is False  # DEFAULT verify now catches the transitive-module change
    assert any("source hash drift" in m or "content_hash drift" in m for m in result.mismatches)


def test_settings_snapshot_includes_drawdown_kill_threshold():
    # max_drawdown_pct feeds RiskEngine drawdown_kill and must be in the frozen surface.
    art = _build()
    assert "max_drawdown_pct" in art["settings_snapshot"]

"""Immutable, hashed pre-registration governance for ETH defined-risk VRP research.

Phase 0 mirrors the strategy-v2 pre-registration contract without emitting a final
artifact. The run identity freezes source hashes, decision-relevant settings, ETH-only
scenario lineage, raw + normalized option-cache manifests, stress selector inputs, folds,
structure grid, Greek provenance, settlement/currency semantics, trial budget, and the GO
bar. Any drift recomputes to a different content hash and invalidates authorization.

Deterministic, read-only, stdlib-only. No network and no option-economics inspection.
"""

from __future__ import annotations

import copy
import hashlib
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

SCHEMA_VERSION = "vrp-prereg-v1"
MISSING_SHA = "MISSING"
DEFAULT_RAW_CACHE_ROOT = "data/raw/deribit_options"
DEFAULT_CACHE_ROOT = "data/cache/deribit_options"
AUTHORIZING_UNIVERSE = "ETH_credit_spreads_only"
SCENARIOS: dict[str, str] = {"ETH": "deribit_options_eth_vrp_v1"}
DEFAULT_SCENARIO_ID = SCENARIOS["ETH"]

PLAN_PRIMARY_EQUITY: float = 1000.0
PLAN_EQUITY_GRID: tuple[float, ...] = (500.0, 1000.0, 2000.0)
PLAN_RISK_LIMITS: dict[str, Any] = {
    "reserve_pct": 0.25,
    "per_structure_max_loss_pct": 0.25,
    "aggregate_max_defined_risk_pct": 0.40,
}

# Walk-forward fold family F1..F7 copied verbatim from strategy-v2.
PLAN_FOLDS: tuple[dict[str, str], ...] = (
    {
        "id": "F1",
        "train_start": "2024-09-01T00:00:00Z",
        "train_end": "2025-03-01T00:00:00Z",
        "test_start": "2025-03-01T00:00:00Z",
        "test_end": "2025-05-01T00:00:00Z",
    },
    {
        "id": "F2",
        "train_start": "2024-11-01T00:00:00Z",
        "train_end": "2025-05-01T00:00:00Z",
        "test_start": "2025-05-01T00:00:00Z",
        "test_end": "2025-07-01T00:00:00Z",
    },
    {
        "id": "F3",
        "train_start": "2025-01-01T00:00:00Z",
        "train_end": "2025-07-01T00:00:00Z",
        "test_start": "2025-07-01T00:00:00Z",
        "test_end": "2025-09-01T00:00:00Z",
    },
    {
        "id": "F4",
        "train_start": "2025-03-01T00:00:00Z",
        "train_end": "2025-09-01T00:00:00Z",
        "test_start": "2025-09-01T00:00:00Z",
        "test_end": "2025-11-01T00:00:00Z",
    },
    {
        "id": "F5",
        "train_start": "2025-05-01T00:00:00Z",
        "train_end": "2025-11-01T00:00:00Z",
        "test_start": "2025-11-01T00:00:00Z",
        "test_end": "2026-01-01T00:00:00Z",
    },
    {
        "id": "F6",
        "train_start": "2025-07-01T00:00:00Z",
        "train_end": "2026-01-01T00:00:00Z",
        "test_start": "2026-01-01T00:00:00Z",
        "test_end": "2026-03-01T00:00:00Z",
    },
    {
        "id": "F7",
        "train_start": "2025-09-01T00:00:00Z",
        "train_end": "2026-03-01T00:00:00Z",
        "test_start": "2026-03-01T00:00:00Z",
        "test_end": "2026-06-01T00:00:00Z",
    },
)
PLAN_WARMUP_START = "2024-08-01T00:00:00Z"
PLAN_COVERAGE_WINDOW: tuple[str, str] = (
    "2024-09-01T00:00:00Z",
    "2026-06-01T00:00:00Z",
)

PLAN_STRUCTURE_GRID: dict[str, Any] = {
    "search_space_version": "vrp-eth-credit-spread-grid-v1",
    "structure_types": ["put_credit_spread", "call_credit_spread"],
    "dte_targets": [21, 30, 45],
    "short_leg_abs_delta": [0.10, 0.16, 0.25],
    "width_usd": [100, 150, 200],
    "min_credit_to_width": [0.15, 0.20],
    "exit_rule": {
        "profit_take_frac": 0.50,
        "stop_loss_credit_mult": 2.0,
        "else": "hold_to_european_settlement",
    },
    "rolls": False,
    "candidates_per_fold": 108,
    "selection_cost_mode": "taker_roundtrip_plus_crossing",
    "selection_equity_usd": 1000.0,
}
PLAN_COST_PATH: dict[str, Any] = {
    "identity": "ajentix_quant.backtest.option_costs:evaluate_structure_costs",
    "maker_can_authorize": False,
}
PLAN_GREEK_PROVENANCE: dict[str, Any] = {
    "selection_source": "vendor_cached_hashed_preferred_else_local",
    "local_formula": "black_scholes",
    "day_count": "act/365",
    "risk_free_rate": 0.0,
    "dividend": 0.0,
    "timestamp_convention": "utc_snapshot",
    "local_greeks_role": "diagnostic_only",
    "deterministic_tie_breakers": True,
}
PLAN_SETTLEMENT: dict[str, Any] = {
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
PLAN_STRESS_RULE: dict[str, Any] = {
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
PLAN_TRIAL_BUDGET: dict[str, Any] = {
    "grid_versions": 1,
    "max_train_trials": 756,
    "max_heldout_evals": 7,
    "multiplicity_method": "hard_trial_budget_cap",
    "no_hidden_retries": True,
    "no_fold_deletion": True,
}
PLAN_GO_BAR: dict[str, Any] = {
    "source_quality_required": "venue_full_historical_chain",
    "min_sharpe": 0.8,
    "max_mdd_incl_stress": 0.25,
    "min_folds_nonneg": 4,
    "total_folds": 7,
    "min_folds_with_entries": 3,
    "min_total_entries": 10,
    "max_single_fold_pnl_share": 0.50,
    "max_single_cluster_pnl_share": 0.35,
    "sharpe_inflation_audit_threshold": 2.5,
    "non_authorizing": [
        "maker",
        "marks_only",
        "dvol",
        "proxy",
        "sample",
        "fixture",
        "naked",
        "btc",
        "iron_condor",
    ],
}


class PreregistrationError(Exception):
    """Raised when a VRP pre-registration artifact is structurally invalid."""


@dataclass(frozen=True)
class VerifyResult:
    valid: bool
    run_status: str  # "valid" | "invalid"
    mismatches: tuple[str, ...]

    def as_dict(self) -> dict[str, Any]:
        return {
            "valid": self.valid,
            "run_status": self.run_status,
            "mismatches": list(self.mismatches),
        }


def _sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _sha256_file(path: Path) -> str:
    return _sha256_text(path.read_text(encoding="utf-8"))


def _canonical(obj: Any) -> str:
    return json.dumps(obj, sort_keys=True, separators=(",", ":"))


def _canonical_hash(obj: Any) -> str:
    return _sha256_text(_canonical(obj))


def _resolve_path(repo_root: str | Path, path: str | Path) -> Path:
    p = Path(path)
    return p if p.is_absolute() else Path(repo_root) / p


def _display_path(repo_root: str | Path, path: str | Path | None) -> str | None:
    if path is None:
        return None
    p = _resolve_path(repo_root, path)
    root = Path(repo_root).resolve()
    try:
        return p.resolve().relative_to(root).as_posix()
    except ValueError:
        return p.as_posix()


def _iter_source_files(root: Path) -> list[Path]:
    """All decision-relevant source files: src/ajentix_quant/**/*.py + scripts/*.py."""
    files: list[Path] = []
    pkg = root / "src" / "ajentix_quant"
    if pkg.is_dir():
        files += [p for p in pkg.rglob("*.py") if "__pycache__" not in p.parts]
    scripts = root / "scripts"
    if scripts.is_dir():
        files += [p for p in scripts.glob("*.py") if "__pycache__" not in p.parts]
    return sorted(files)


def compute_source_hashes(repo_root: str | Path, extra: tuple[str, ...] = ()) -> dict[str, str]:
    root = Path(repo_root)
    out: dict[str, str] = {}
    for p in _iter_source_files(root):
        out[p.relative_to(root).as_posix()] = _sha256_file(p)
    for rel in extra:
        p = root / rel
        out[rel] = _sha256_file(p) if p.is_file() else MISSING_SHA
    return out


def raw_manifest_sha(
    repo_root: str | Path,
    raw_cache_root: str | Path,
    scenario_id: str,
) -> str:
    """Return sha256 of <raw_cache_root>/<scenario_id>/manifest.json, or MISSING."""
    p = _resolve_path(repo_root, raw_cache_root) / scenario_id / "manifest.json"
    return _sha256_file(p) if p.is_file() else MISSING_SHA


def normalized_manifest_sha(
    repo_root: str | Path,
    cache_root: str | Path,
    scenario_id: str,
) -> str:
    """Return sha256 of <cache_root>/<scenario_id>/manifest.json, or MISSING."""
    p = _resolve_path(repo_root, cache_root) / scenario_id / "manifest.json"
    return _sha256_file(p) if p.is_file() else MISSING_SHA


def stress_selector_input_sha(
    repo_root: str | Path,
    stress_selector_input_path: str | Path | None,
) -> str:
    """Return sha256 of the frozen stress selector-input manifest, or MISSING."""
    if stress_selector_input_path is None:
        return MISSING_SHA
    p = _resolve_path(repo_root, stress_selector_input_path)
    return _sha256_file(p) if p.is_file() else MISSING_SHA


def _settings_snapshot() -> dict[str, Any]:
    """Decision-relevant existing Settings fields for options cost/risk sizing."""
    from ..config import Settings

    s = Settings()
    fields = (
        "capital_usd_min",
        "capital_usd_max",
        "default_capital_usd",
        "reserve_pct",
        "max_position_pct",
        "max_drawdown_pct",
        "base_leverage",
        "max_leverage",
        "min_liq_distance_pct",
        "health_factor_floor",
        "gap_stress_pct",
        "vol_spike_annual",
        "perp_taker_fee_bps",
        "perp_maker_fee_bps",
        "spot_taker_fee_bps",
        "leverage_cost_apr",
        "slippage_base_bps",
        "slippage_impact_bps_per_pct_volume",
        "slippage_cap_bps",
    )
    return {field: getattr(s, field) for field in fields if hasattr(s, field)}


def _frozen_plan() -> dict[str, Any]:
    """The VRP plan-constant block hashed into the run identity."""
    return {
        "schema_version": SCHEMA_VERSION,
        "authorizing_universe": AUTHORIZING_UNIVERSE,
        "scenarios": dict(SCENARIOS),
        "primary_equity_usd": PLAN_PRIMARY_EQUITY,
        "equity_grid": list(PLAN_EQUITY_GRID),
        "risk_limits": copy.deepcopy(PLAN_RISK_LIMITS),
        "warmup_start": PLAN_WARMUP_START,
        "coverage_window": list(PLAN_COVERAGE_WINDOW),
        "folds": [dict(fold) for fold in PLAN_FOLDS],
        "structure_grid": copy.deepcopy(PLAN_STRUCTURE_GRID),
        "cost_path": copy.deepcopy(PLAN_COST_PATH),
        "greek_provenance": copy.deepcopy(PLAN_GREEK_PROVENANCE),
        "settlement": copy.deepcopy(PLAN_SETTLEMENT),
        "stress_rule": copy.deepcopy(PLAN_STRESS_RULE),
        "trial_budget": copy.deepcopy(PLAN_TRIAL_BUDGET),
        "go_bar": copy.deepcopy(PLAN_GO_BAR),
    }


def _normalize_stress_windows(
    stress_windows: tuple[dict[str, Any], ...] | list[dict[str, Any]] | None,
) -> list[dict[str, Any]]:
    if stress_windows is None:
        return []
    return [dict(window) for window in stress_windows]


def _validate_scenario_id(scenario_id: str) -> None:
    if scenario_id != DEFAULT_SCENARIO_ID:
        raise PreregistrationError(
            f"scenario_id {scenario_id!r} is not authorizing; "
            f"expected {DEFAULT_SCENARIO_ID!r}"
        )


def _frozen_content(
    repo_root: str | Path,
    raw_cache_root: str | Path,
    cache_root: str | Path,
    stress_windows: tuple[dict[str, Any], ...] | list[dict[str, Any]] | None,
    stress_selector_input_path: str | Path | None,
    extra_source_files: tuple[str, ...],
) -> dict[str, Any]:
    """All frozen fields that define the run identity, excluding run_id/content_hash."""
    source_hashes = compute_source_hashes(repo_root, extra=extra_source_files)
    raw_shas = {
        scenario: raw_manifest_sha(repo_root, raw_cache_root, scenario)
        for scenario in SCENARIOS.values()
    }
    normalized_shas = {
        scenario: normalized_manifest_sha(repo_root, cache_root, scenario)
        for scenario in SCENARIOS.values()
    }
    stress_path = _display_path(repo_root, stress_selector_input_path)
    return {
        "plan": _frozen_plan(),
        "settings_snapshot": _settings_snapshot(),
        "source_hashes": source_hashes,
        "raw_cache_root": str(raw_cache_root),
        "cache_root": str(cache_root),
        "raw_source_manifest_sha256": raw_shas,
        "normalized_cache_manifest_sha256": normalized_shas,
        "stress_windows": _normalize_stress_windows(stress_windows),
        "stress_selector_input_path": stress_path,
        "stress_selector_input_sha256": stress_selector_input_sha(
            repo_root, stress_selector_input_path
        ),
    }


def build_preregistration(
    repo_root: str | Path,
    *,
    raw_cache_root: str | Path = DEFAULT_RAW_CACHE_ROOT,
    cache_root: str | Path = DEFAULT_CACHE_ROOT,
    scenario_id: str = DEFAULT_SCENARIO_ID,
    stress_windows: tuple[dict[str, Any], ...] | list[dict[str, Any]] | None = None,
    stress_selector_input_path: str | Path | None = None,
    extra_source_files: tuple[str, ...] = (),
) -> dict[str, Any]:
    """Assemble the VRP pre-registration artifact; run_id derives from frozen content."""
    _validate_scenario_id(scenario_id)
    content = _frozen_content(
        repo_root,
        raw_cache_root,
        cache_root,
        stress_windows,
        stress_selector_input_path,
        extra_source_files,
    )
    content_hash = _canonical_hash(content)
    run_id = f"vrp-{content_hash[:12]}"
    return {
        "schema_version": SCHEMA_VERSION,
        "run_id": run_id,
        "content_hash": content_hash,
        **content,
    }


def load_preregistration(path: str | Path) -> dict[str, Any]:
    p = Path(path)
    if not p.is_file():
        raise PreregistrationError(f"missing VRP pre-registration artifact: {p}")
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise PreregistrationError(f"VRP pre-registration is not valid JSON: {exc}") from exc
    if not isinstance(data, dict):
        raise PreregistrationError("VRP pre-registration must be a JSON object")
    return data


def _append_plan_mismatches(
    artifact_plan: Any,
    current_plan: dict[str, Any],
    mismatches: list[str],
) -> None:
    if not isinstance(artifact_plan, dict):
        mismatches.append("plan-constant drift (plan block missing or not an object)")
        return

    surface_messages = (
        ("scenarios", "scenario/universe drift"),
        ("authorizing_universe", "authorizing-universe drift"),
        ("folds", "fold-boundary drift"),
        ("structure_grid", "structure-grid drift"),
        ("cost_path", "cost-path drift"),
        ("greek_provenance", "greek-provenance drift"),
        ("settlement", "settlement/currency drift"),
        ("stress_rule", "stress-rule drift"),
        ("trial_budget", "trial-budget drift"),
        ("go_bar", "GO-bar drift"),
        ("risk_limits", "risk-limit drift"),
    )
    for key, message in surface_messages:
        if artifact_plan.get(key) != current_plan[key]:
            mismatches.append(message)
    if artifact_plan != current_plan:
        mismatches.append("plan-constant drift (VRP frozen plan surfaces changed)")


def verify_preregistration(
    artifact: dict[str, Any],
    repo_root: str | Path,
    *,
    raw_cache_root: str | Path | None = None,
    cache_root: str | Path | None = None,
    scenario_id: str = DEFAULT_SCENARIO_ID,
    stress_windows: tuple[dict[str, Any], ...] | list[dict[str, Any]] | None = None,
    stress_selector_input_path: str | Path | None = None,
    extra_source_files: tuple[str, ...] = (),
) -> VerifyResult:
    """Recompute every frozen VRP hash and return run_status=invalid on ANY drift."""
    mismatches: list[str] = []
    if artifact.get("schema_version") != SCHEMA_VERSION:
        mismatches.append(
            f"schema_version {artifact.get('schema_version')!r} != {SCHEMA_VERSION!r}"
        )

    try:
        _validate_scenario_id(scenario_id)
    except PreregistrationError as exc:
        mismatches.append(str(exc))

    current_raw_root = str(
        raw_cache_root or artifact.get("raw_cache_root", DEFAULT_RAW_CACHE_ROOT)
    )
    current_cache_root = str(
        cache_root or artifact.get("cache_root", DEFAULT_CACHE_ROOT)
    )
    current_stress_path = stress_selector_input_path
    artifact_stress_path = artifact.get("stress_selector_input_path")
    if current_stress_path is None and isinstance(artifact_stress_path, str):
        current_stress_path = artifact_stress_path

    current = _frozen_content(
        repo_root,
        current_raw_root,
        current_cache_root,
        stress_windows,
        current_stress_path,
        extra_source_files,
    )

    frozen_src = artifact.get("source_hashes", {})
    if not isinstance(frozen_src, dict):
        mismatches.append("source_hashes missing or not an object")
        frozen_src = {}
    for rel, h in current["source_hashes"].items():
        if frozen_src.get(rel) != h:
            mismatches.append(f"source hash drift: {rel}")
    for rel in frozen_src:
        if rel not in current["source_hashes"]:
            mismatches.append(f"source file removed from frozen set: {rel}")

    if artifact.get("settings_snapshot") != current["settings_snapshot"]:
        mismatches.append("settings_snapshot drift")

    _append_plan_mismatches(artifact.get("plan"), current["plan"], mismatches)

    frozen_raw = artifact.get("raw_source_manifest_sha256", {})
    if not isinstance(frozen_raw, dict):
        mismatches.append("raw_source_manifest_sha256 missing or not an object")
        frozen_raw = {}
    for scenario, h in current["raw_source_manifest_sha256"].items():
        if frozen_raw.get(scenario) != h:
            mismatches.append(f"raw-source manifest drift: {scenario}")

    frozen_normalized = artifact.get("normalized_cache_manifest_sha256", {})
    if not isinstance(frozen_normalized, dict):
        mismatches.append("normalized_cache_manifest_sha256 missing or not an object")
        frozen_normalized = {}
    for scenario, h in current["normalized_cache_manifest_sha256"].items():
        if frozen_normalized.get(scenario) != h:
            mismatches.append(f"normalized-cache manifest drift: {scenario}")

    if artifact.get("stress_windows") != current["stress_windows"]:
        mismatches.append("stress-window drift")
    if artifact.get("stress_selector_input_sha256") != current["stress_selector_input_sha256"]:
        mismatches.append("stress-selector-input manifest drift")

    recomputed_hash = _canonical_hash(current)
    if artifact.get("content_hash") != recomputed_hash:
        mismatches.append("content_hash drift (VRP frozen content changed)")
    expected_run_id = f"vrp-{recomputed_hash[:12]}"
    if artifact.get("run_id") != expected_run_id:
        mismatches.append(
            f"run_id {artifact.get('run_id')!r} != recomputed {expected_run_id!r}"
        )

    valid = not mismatches
    return VerifyResult(
        valid=valid,
        run_status="valid" if valid else "invalid",
        mismatches=tuple(mismatches),
    )


def _manifest_root_from_path(
    repo_root: str | Path,
    manifest_path: str | Path | None,
    scenario_id: str,
    label: str,
) -> str:
    if manifest_path is None:
        raise PreregistrationError(
            f"{label} manifest path is required for VRP artifact emit"
        )
    p = _resolve_path(repo_root, manifest_path)
    if not p.is_file():
        raise PreregistrationError(f"{label} manifest path does not exist: {p}")
    expected_suffix = Path(scenario_id) / "manifest.json"
    if p.name != "manifest.json" or p.parent.name != scenario_id:
        raise PreregistrationError(
            f"{label} manifest must be <root>/{expected_suffix.as_posix()}: {p}"
        )
    return _display_path(repo_root, p.parent.parent) or p.parent.parent.as_posix()


def write_preregistration(
    repo_root: str | Path,
    *,
    raw_manifest_path: str | Path | None = None,
    normalized_manifest_path: str | Path | None = None,
    scenario_id: str = DEFAULT_SCENARIO_ID,
    stress_windows: tuple[dict[str, Any], ...] | list[dict[str, Any]] | None = None,
    stress_selector_input_path: str | Path | None = None,
    extra_source_files: tuple[str, ...] = (),
    out_dir: str = "docs/preregistration",
) -> Path:
    """Emit docs/preregistration/vrp-*.json only after required manifests exist."""
    _validate_scenario_id(scenario_id)
    raw_root = _manifest_root_from_path(
        repo_root, raw_manifest_path, scenario_id, "raw-source"
    )
    normalized_root = _manifest_root_from_path(
        repo_root, normalized_manifest_path, scenario_id, "normalized-cache"
    )
    artifact = build_preregistration(
        repo_root,
        raw_cache_root=raw_root,
        cache_root=normalized_root,
        scenario_id=scenario_id,
        stress_windows=stress_windows,
        stress_selector_input_path=stress_selector_input_path,
        extra_source_files=extra_source_files,
    )
    dest_dir = _resolve_path(repo_root, out_dir)
    dest_dir.mkdir(parents=True, exist_ok=True)
    dest = dest_dir / f"{artifact['run_id']}.json"
    dest.write_text(
        json.dumps(artifact, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    return dest

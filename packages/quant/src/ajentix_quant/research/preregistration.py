"""Immutable, hashed pre-registration governance for strategy-v2 R&D.

Anti-overfit is the whole point. Before ANY breakeven table, walk-forward fold, held-out
Edge Verdict, or pivot-feasibility output, one committed artifact freezes everything that
defines the run: scenario IDs, cache manifest sha256s, the source-quality requirement,
source-code hashes, the settings snapshot, symbols, equity grid, breakeven horizons, the
F1..F7 fold manifest, the param grid + search-space version, cost modes, the total trial
budget, the multiplicity method, the A1-vs-A2 decision bar, the A2 venue-feasibility bar,
and the aggregate GO/NO_GO/INCONCLUSIVE/pivot + branch rules.

``run_id`` is derived from a sha256 over ALL frozen fields, so changing any of them (code,
settings, folds, grid, caches, thresholds) yields a NEW run_id automatically. Downstream
runners ``verify_preregistration`` and refuse a GO (run_status=invalid) on any mismatch.
This makes a GO structurally hard to game and forbids silent test-window / grid retuning.

Deterministic, read-only, stdlib-only. No network, no engine changes.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

SCHEMA_VERSION = "stratv2-prereg-v1"

# --- frozen PLAN constants (tamper-evident: edits change the recomputed hashes) ----------
# The run identity freezes EVERY source file the engine/verdict/strategy/risk/cost path can
# transitively depend on: all of src/ajentix_quant/**/*.py plus all scripts/*.py. Globbing the
# whole package (rather than an explicit list) closes the gap where a load-bearing transitive
# module (e.g. metrics.py / account.py / events.py) could change while verification stayed valid.

SCENARIOS: dict[str, str] = {
    "BTC/USDT:USDT": "bybit_real_btc_v1",
    "ETH/USDT:USDT": "bybit_real_eth_v1",
}
PLAN_SYMBOLS: tuple[str, ...] = ("BTC/USDT:USDT", "ETH/USDT:USDT")
PLAN_EQUITY_GRID: tuple[float, ...] = (500.0, 1000.0, 2000.0)
PLAN_PRIMARY_EQUITY: float = 1000.0
PLAN_REPORT_HORIZONS: tuple[int, ...] = (1, 3, 7, 14)
PLAN_DECISION_HORIZONS: tuple[int, ...] = (21, 42)
# In-sample decision boundary (train_until 2025-09-01T00:00:00Z); held-out rows are after it.
PLAN_INSAMPLE_UNTIL_MS: int = 1_756_684_800_000

# Walk-forward fold family F1..F7 (UTC ISO boundaries).
PLAN_FOLDS: tuple[dict[str, str], ...] = (
    {"id": "F1", "train_start": "2024-09-01T00:00:00Z", "train_end": "2025-03-01T00:00:00Z",
     "test_start": "2025-03-01T00:00:00Z", "test_end": "2025-05-01T00:00:00Z"},
    {"id": "F2", "train_start": "2024-11-01T00:00:00Z", "train_end": "2025-05-01T00:00:00Z",
     "test_start": "2025-05-01T00:00:00Z", "test_end": "2025-07-01T00:00:00Z"},
    {"id": "F3", "train_start": "2025-01-01T00:00:00Z", "train_end": "2025-07-01T00:00:00Z",
     "test_start": "2025-07-01T00:00:00Z", "test_end": "2025-09-01T00:00:00Z"},
    {"id": "F4", "train_start": "2025-03-01T00:00:00Z", "train_end": "2025-09-01T00:00:00Z",
     "test_start": "2025-09-01T00:00:00Z", "test_end": "2025-11-01T00:00:00Z"},
    {"id": "F5", "train_start": "2025-05-01T00:00:00Z", "train_end": "2025-11-01T00:00:00Z",
     "test_start": "2025-11-01T00:00:00Z", "test_end": "2026-01-01T00:00:00Z"},
    {"id": "F6", "train_start": "2025-07-01T00:00:00Z", "train_end": "2026-01-01T00:00:00Z",
     "test_start": "2026-01-01T00:00:00Z", "test_end": "2026-03-01T00:00:00Z"},
    {"id": "F7", "train_start": "2025-09-01T00:00:00Z", "train_end": "2026-03-01T00:00:00Z",
     "test_start": "2026-03-01T00:00:00Z", "test_end": "2026-06-01T00:00:00Z"},
)

PLAN_GRID: dict[str, Any] = {
    "search_space_version": "strategy-v2-a1-grid-v1",
    "min_funding_rate_8h": [0.0, 0.000025, 0.00005, 0.0001],
    "min_hold_intervals": [21, 42],
    "rebalance_band": [0.02, 0.05],
    "leverage_policy": "risk_capped_dynamic",
    "selection_cost_mode": "taker_roundtrip_plus_slippage",
    "selection_equity_usd": 1000.0,
    "candidates_per_fold_per_symbol": 16,
}

PLAN_COST_MODES: dict[str, str] = {
    "primary": "taker_roundtrip_plus_slippage",
    "secondary": "conservative_maker_sensitivity_non_authorizing",
}

PLAN_TRIAL_BUDGET: dict[str, Any] = {
    "grid_versions": 1,
    "max_primary_train_trials": 224,
    "max_primary_heldout_evals": 14,
    "max_secondary_sensitivity_evals": 42,
    "total_heldout_cap": 56,
    "multiplicity_method": "hard_trial_budget_cap",
    "no_hidden_retries": True,
    "no_fold_deletion": True,
}

PLAN_A1_BAR: dict[str, Any] = {
    "decision_horizons": [21, 42],
    "min_insample_funding_rows": 900,
    "min_valid_windows_per_horizon": 800,
    "min_qualifying_pct": 0.10,
    "min_qualifying_windows": 80,
    "primary_equity_usd": 1000.0,
    "capital_robustness_equities": [500.0, 2000.0],
    "min_clusters": 6,
    "max_single_cluster_share": 0.35,
    "max_top3_cluster_share": 0.65,
    "safety_margin_bps": 1.0,
    "cost_mode": "taker_roundtrip_plus_slippage",
    "per_symbol": True,
    "maker_can_authorize": False,
}

PLAN_A2_BAR: dict[str, Any] = {
    "min_days_funding_history": 90,
    "depth_per_leg_usd": [250.0, 500.0],
    "max_slippage_bps_per_leg": 5.0,
    "min_qualifying_24h_pct": 0.10,
    "min_qualifying_24h_windows": 30,
    "safety_margin_bps": 1.0,
    "equity_usd": 1000.0,
    "min_clusters": 6,
    "max_single_week_share": 0.40,
    "requires": [
        "funding_history", "cadence", "fees", "depth_liquidity",
        "adl_liquidation_metadata", "borrow_basis_risk", "cex_comparison",
    ],
    "roadmap_apr_claims_authorize": False,
}

PLAN_AGGREGATE_RULES: dict[str, Any] = {
    "source_quality_required": "venue",
    "min_sharpe": 1.5,
    "max_mdd": 0.05,
    "min_net_apr": 0.0,
    "min_folds_nonneg_net_apr_pct": 0.75,
    "min_total_entries": 10,
    "min_folds_with_entries": 3,
    "any_fold_collapse_is_no_go": True,
    "max_single_fold_pnl_share": 0.50,
    "max_single_cluster_pnl_share": 0.35,
    "max_top3_cluster_pnl_share": 0.65,
    "any_inconclusive_fold_blocks_go": True,
}


class PreregistrationError(Exception):
    """Raised when a pre-registration artifact is structurally invalid."""


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


def _iter_source_files(root: Path) -> list[Path]:
    """All decision-relevant source files: src/ajentix_quant/**/*.py + scripts/*.py."""
    files: list[Path] = []
    pkg = root / "src" / "ajentix_quant"
    if pkg.is_dir():
        files += [p for p in pkg.rglob("*.py") if "__pycache__" not in p.parts]
    scripts = root / "scripts"
    if scripts.is_dir():
        files += [p for p in scripts.glob("*.py")]
    return sorted(files)


def compute_source_hashes(repo_root: str | Path, extra: tuple[str, ...] = ()) -> dict[str, str]:
    root = Path(repo_root)
    out: dict[str, str] = {}
    for p in _iter_source_files(root):
        out[p.relative_to(root).as_posix()] = _sha256_file(p)
    for rel in extra:
        p = root / rel
        out[rel] = _sha256_file(p) if p.is_file() else "MISSING"
    return out


def cache_manifest_sha(repo_root: str | Path, cache_root: str, scenario_id: str) -> str:
    p = Path(repo_root) / cache_root / scenario_id / "manifest.json"
    return _sha256_file(p) if p.is_file() else "MISSING"


def _settings_snapshot() -> dict[str, Any]:
    """Decision-relevant config + verdict thresholds, captured from live defaults."""
    from ..config import Settings

    s = Settings()
    fields = (
        "perp_taker_fee_bps", "perp_maker_fee_bps", "spot_taker_fee_bps", "leverage_cost_apr",
        "slippage_base_bps", "slippage_impact_bps_per_pct_volume", "slippage_cap_bps",
        "reserve_pct", "max_position_pct", "base_leverage", "max_leverage",
        "min_liq_distance_pct", "health_factor_floor", "gap_stress_pct", "vol_spike_annual",
        "funding_compression_8h", "funding_reversal_imminent_8h", "max_net_delta_frac",
        "adl_rank_threshold", "max_drawdown_pct", "default_capital_usd",
        "capital_usd_min", "capital_usd_max",
        "min_funding_rate_8h",
    )
    snap: dict[str, Any] = {k: getattr(s, k) for k in fields}
    # Edge Verdict thresholds are part of the frozen decision surface.
    from ..backtest.verdict import EdgeVerdictThresholds

    t = EdgeVerdictThresholds()
    snap["verdict_min_sharpe"] = t.min_sharpe
    snap["verdict_max_mdd"] = t.max_mdd
    snap["verdict_min_net_apr"] = t.min_net_apr
    return snap


def _frozen_plan() -> dict[str, Any]:
    """The plan-constant block that is hashed into the run identity."""
    return {
        "schema_version": SCHEMA_VERSION,
        "scenarios": SCENARIOS,
        "symbols": list(PLAN_SYMBOLS),
        "equity_grid": list(PLAN_EQUITY_GRID),
        "primary_equity_usd": PLAN_PRIMARY_EQUITY,
        "report_horizons": list(PLAN_REPORT_HORIZONS),
        "decision_horizons": list(PLAN_DECISION_HORIZONS),
        "insample_until_ms": PLAN_INSAMPLE_UNTIL_MS,
        "folds": list(PLAN_FOLDS),
        "grid": PLAN_GRID,
        "cost_modes": PLAN_COST_MODES,
        "trial_budget": PLAN_TRIAL_BUDGET,
        "a1_bar": PLAN_A1_BAR,
        "a2_bar": PLAN_A2_BAR,
        "aggregate_rules": PLAN_AGGREGATE_RULES,
        "source_quality_required": "venue",
    }


def _frozen_content(
    repo_root: str | Path,
    cache_root: str,
    extra_source_files: tuple[str, ...],
) -> dict[str, Any]:
    """All frozen fields that define the run identity (everything except run_id itself)."""
    source_hashes = compute_source_hashes(repo_root, extra=extra_source_files)
    cache_shas = {
        scenario: cache_manifest_sha(repo_root, cache_root, scenario)
        for scenario in SCENARIOS.values()
    }
    return {
        "plan": _frozen_plan(),
        "settings_snapshot": _settings_snapshot(),
        "source_hashes": source_hashes,
        "cache_root": cache_root,
        "cache_manifest_sha256": cache_shas,
    }


def build_preregistration(
    repo_root: str | Path,
    *,
    cache_root: str = "data/cache/bybit",
    extra_source_files: tuple[str, ...] = (),
) -> dict[str, Any]:
    """Assemble the pre-registration artifact (run_id derived from all frozen content)."""
    content = _frozen_content(repo_root, cache_root, extra_source_files)
    content_hash = _canonical_hash(content)
    run_id = f"stratv2-{content_hash[:12]}"
    return {
        "schema_version": SCHEMA_VERSION,
        "run_id": run_id,
        "content_hash": content_hash,
        **content,
    }


def load_preregistration(path: str | Path) -> dict[str, Any]:
    p = Path(path)
    if not p.is_file():
        raise PreregistrationError(f"missing pre-registration artifact: {p}")
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise PreregistrationError(f"pre-registration is not valid JSON: {exc}") from exc
    if not isinstance(data, dict):
        raise PreregistrationError("pre-registration must be a JSON object")
    return data


def verify_preregistration(
    artifact: dict[str, Any],
    repo_root: str | Path,
    *,
    extra_source_files: tuple[str, ...] = (),
) -> VerifyResult:
    """Recompute all frozen hashes from the current repo and compare to the artifact.

    Returns run_status=invalid (and the list of mismatches) on ANY drift, so a downstream
    runner can refuse a GO. This is the ungameable lineage check.
    """
    mismatches: list[str] = []
    if artifact.get("schema_version") != SCHEMA_VERSION:
        mismatches.append(
            f"schema_version {artifact.get('schema_version')!r} != {SCHEMA_VERSION}"
        )

    cache_root = str(artifact.get("cache_root", "data/cache/bybit"))
    current = _frozen_content(repo_root, cache_root, extra_source_files)

    # per-file source hashes
    frozen_src = artifact.get("source_hashes", {})
    for rel, h in current["source_hashes"].items():
        if frozen_src.get(rel) != h:
            mismatches.append(f"source hash drift: {rel}")
    for rel in frozen_src:
        if rel not in current["source_hashes"]:
            mismatches.append(f"source file removed from frozen set: {rel}")

    # settings snapshot
    if artifact.get("settings_snapshot") != current["settings_snapshot"]:
        mismatches.append("settings_snapshot drift")

    # plan constants (folds, grid, bars, budget, ...)
    if artifact.get("plan") != current["plan"]:
        mismatches.append("plan-constant drift (folds/grid/bars/budget/thresholds)")

    # cache manifest shas
    frozen_cache = artifact.get("cache_manifest_sha256", {})
    for scenario, h in current["cache_manifest_sha256"].items():
        if frozen_cache.get(scenario) != h:
            mismatches.append(f"cache manifest drift: {scenario}")

    # content_hash + run_id integrity (recompute from current content)
    recomputed_hash = _canonical_hash(current)
    if artifact.get("content_hash") != recomputed_hash:
        mismatches.append("content_hash drift (frozen content changed)")
    expected_run_id = f"stratv2-{recomputed_hash[:12]}"
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


def write_preregistration(
    repo_root: str | Path,
    *,
    cache_root: str = "data/cache/bybit",
    extra_source_files: tuple[str, ...] = (),
    out_dir: str = "docs/preregistration",
) -> Path:
    """Build + write the artifact to docs/preregistration/<run_id>.json; return its path."""
    artifact = build_preregistration(
        repo_root, cache_root=cache_root, extra_source_files=extra_source_files
    )
    dest_dir = Path(repo_root) / out_dir
    dest_dir.mkdir(parents=True, exist_ok=True)
    dest = dest_dir / f"{artifact['run_id']}.json"
    dest.write_text(json.dumps(artifact, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return dest

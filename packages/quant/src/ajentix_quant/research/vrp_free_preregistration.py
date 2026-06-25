"""Immutable, hashed governance for free-data-native ETH defined-risk VRP research.

Phase 0 freezes the reconstructed/free-data lineage before calibration or economics. The
schema is deliberately non-authorizing: reconstructed chains plus sampled spread calibration
can produce NO_GO, PROMISING_PENDING_REAL_SPREAD, or INCONCLUSIVE, but never a capital GO.

Deterministic, read-only, stdlib-only. No network and no option-economics inspection.
"""

from __future__ import annotations

import copy
import hashlib
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ..adapters.base import SourceQuality

SCHEMA_VERSION = "vrp-free-prereg-v1"
PRECALIBRATION_SCHEMA_VERSION = "vrp-free-precalibration-v1"
MISSING_SHA = "MISSING"

DEFAULT_RAW_SOURCE_ROOT = "data/raw/deribit_history_options"
DEFAULT_RECONSTRUCTED_CACHE_ROOT = "data/cache/vrp_free_reconstructed_options"
DEFAULT_TARDIS_SAMPLE_ROOT = "data/raw/tardis_free_deribit_options_chain"
DEFAULT_SPREAD_CALIBRATION_ROOT = "data/cache/vrp_free_spread_calibration"
DEFAULT_PRECALIBRATION_OUT_DIR = "docs/preregistration"

# Compatibility aliases for callers mirroring vrp_preregistration.py names.
DEFAULT_RAW_CACHE_ROOT = DEFAULT_RAW_SOURCE_ROOT
DEFAULT_CACHE_ROOT = DEFAULT_RECONSTRUCTED_CACHE_ROOT

AUTHORIZING_UNIVERSE = "ETH_credit_spreads_free_reconstructed_only"
SCENARIOS: dict[str, str] = {"ETH": "deribit_history_eth_vrp_free_v1"}
DEFAULT_SCENARIO_ID = SCENARIOS["ETH"]

PLAN_PRIMARY_EQUITY: float = 1000.0
PLAN_EQUITY_GRID: tuple[float, ...] = (500.0, 1000.0, 2000.0)
PLAN_RISK_LIMITS: dict[str, Any] = {
    "reserve_pct": 0.25,
    "per_structure_max_loss_pct": 0.25,
    "aggregate_max_defined_risk_pct": 0.40,
}

# Walk-forward fold family F1..F7 copied verbatim from the committed VRP pre-registration.
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

PLAN_RECONSTRUCTION_CONFIG: dict[str, Any] = {
    "method_version": "deribit-history-trade-iv-bs-reconstruction-v1",
    "cadence_hours": 8,
    "utc_hours": [0, 8, 16],
    "include_required_stress_timestamps": True,
    "include_expiry_settlement_timestamps": True,
    "max_trade_staleness_hours": 72,
    "no_future_trades": True,
    "pricing_model": "black_scholes_from_real_trade_iv",
    "input_trade_fields": [
        "timestamp",
        "instrument_name",
        "price",
        "mark_price",
        "iv",
        "index_price",
        "amount",
        "direction",
    ],
    "missing_required_coverage": "INCONCLUSIVE",
}

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

TARDIS_SAMPLE_MONTHS: tuple[str, ...] = (
    "2024-08-01",
    "2024-09-01",
    "2024-10-01",
    "2024-11-01",
    "2024-12-01",
    "2025-01-01",
    "2025-02-01",
    "2025-03-01",
    "2025-04-01",
    "2025-05-01",
    "2025-06-01",
    "2025-07-01",
    "2025-08-01",
    "2025-09-01",
    "2025-10-01",
    "2025-11-01",
    "2025-12-01",
    "2026-01-01",
    "2026-02-01",
    "2026-03-01",
    "2026-04-01",
    "2026-05-01",
    "2026-06-01",
)

PLAN_COST_BUDGET_BAR: dict[str, Any] = {
    "spread_quantile": "p75",
    "spread_safety_multiplier": 1.25,
    "median_spread_margin_multiplier": 1.50,
    "require_net_credit_to_width_after_p75_safety": True,
    "min_samples_per_bin": 30,
    "min_distinct_months_per_bin": 6,
    "min_valid_rows_per_required_month": 100,
    "min_valid_months_per_calendar_quarter": 2,
    "missing_required_month_behavior": "INCONCLUSIVE",
    "post_calibration_threshold_change_behavior": "INVALID",
    "fail_closed_when_bin_unavailable": True,
    "units": "round_trip_structure_spread_usd",
}
PLAN_BINNING: dict[str, Any] = {
    "dte_buckets_days": {
        "dte_21": [14, 27],
        "dte_30": [28, 38],
        "dte_45": [39, 60],
    },
    "absolute_log_moneyness_buckets": {
        "atm": [0.00, 0.03],
        "near": [0.03, 0.08],
        "wing": [0.08, 0.15],
        "far": [0.15, 0.30],
    },
    "option_types": ["put", "call"],
    "fallback_order": [
        "option_type+dte_bucket+moneyness_bucket+regime_label",
        "option_type+dte_bucket+moneyness_bucket",
        "option_type+dte_bucket",
        "option_type+moneyness_bucket",
        "fail_closed",
    ],
    "fallback_spread_selection": "max_available_quantile_not_lower_than_narrower_children",
}
PLAN_REGIME_LABELS: dict[str, str] = {
    "normal": "trailing_30d_rv_annualized <= 0.60 and abs_24h_return < 0.08",
    "high_vol": (
        "0.60 < trailing_30d_rv_annualized <= 1.00 "
        "or 0.08 <= abs_24h_return < 0.12"
    ),
    "tail": "trailing_30d_rv_annualized > 1.00 or abs_24h_return >= 0.12",
}
PLAN_UNIT_CONVERSIONS: dict[str, str] = {
    "deribit_option_price_unit": "ETH per 1 ETH contract",
    "spread_price_eth": "max(ask_price - bid_price, 0)",
    "round_trip_leg_crossing_usd": (
        "spread_price_eth * index_price_usd * contract_multiplier * quantity"
    ),
    "round_trip_structure_spread_usd": "sum(round_trip_leg_crossing_usd for both legs)",
    "vol_points": "iv_fraction * 100",
}
PLAN_FOLD_CAUSAL_CALIBRATION_RULE = (
    "fold selection and verdict gates use only calibration samples with sample_timestamp "
    "<= fold.train_end; all-sample calibration may be diagnostic only and cannot improve verdict"
)
PLAN_FAIL_CLOSED_RULES: dict[str, Any] = {
    "missing_precalibration_artifact": "INVALID",
    "missing_calibration_output": "INCONCLUSIVE",
    "missing_required_month": "INCONCLUSIVE",
    "insufficient_bin_samples": "INCONCLUSIVE",
    "post_calibration_config_change": "INVALID",
    "venue_masquerade": "INVALID_LINEAGE",
}
PLAN_PROMISING_CONFIRMATION_TRIGGER = (
    "PROMISING_PENDING_REAL_SPREAD authorizes only a continuous real-spread confirmation "
    "via paid Tardis trial, Deribit partnership free tier, or equivalent; it does not "
    "authorize capital"
)

PLAN_SOURCE_QUALITY_BRIDGE: dict[str, Any] = {
    "legacy_source_quality": SourceQuality.FIXTURE.name,
    "legacy_option_leg_source_quality": "SourceQuality.FIXTURE",
    "forbidden_reconstructed_option_leg_source_quality": "SourceQuality.VENUE",
    "forbid_venue": True,
    "free_source_quality": "reconstructed_from_real_trade_iv",
    "spread_source_quality": "calibrated_spread_sample",
    "authorizing": False,
    "capital_go_allowed": False,
    "non_authorizing_reason": "reconstructed_from_real_trade_iv",
    "free_verdict_allowed_outcomes": [
        "NO_GO",
        "PROMISING_PENDING_REAL_SPREAD",
        "INCONCLUSIVE",
    ],
}
PLAN_OUTCOME_RULES: dict[str, Any] = {
    "allowed_outcomes": ["NO_GO", "PROMISING_PENDING_REAL_SPREAD", "INCONCLUSIVE"],
    "no_capital_go_from_reconstructed_only": True,
    "capital_go_allowed": False,
    "go_payload_behavior": "INVALID_LINEAGE",
    "venue_masquerade_behavior": "INVALID_LINEAGE",
    "missing_free_label_behavior": "INVALID_LINEAGE",
    "missing_non_authorizing_reason_behavior": "INVALID_LINEAGE",
    "promising_pending_real_spread_is_not_capital_authorization": True,
}


class PreregistrationError(Exception):
    """Raised when a VRP-free governance artifact is structurally invalid."""


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


def manifest_sha(repo_root: str | Path, root_path: str | Path, scenario_id: str) -> str:
    p = _resolve_path(repo_root, root_path) / scenario_id / "manifest.json"
    return _sha256_file(p) if p.is_file() else MISSING_SHA


def raw_source_manifest_sha(
    repo_root: str | Path,
    raw_source_root: str | Path,
    scenario_id: str,
) -> str:
    return manifest_sha(repo_root, raw_source_root, scenario_id)


def reconstructed_cache_manifest_sha(
    repo_root: str | Path,
    reconstructed_cache_root: str | Path,
    scenario_id: str,
) -> str:
    return manifest_sha(repo_root, reconstructed_cache_root, scenario_id)


def tardis_sample_manifest_sha(
    repo_root: str | Path,
    tardis_sample_root: str | Path,
    scenario_id: str,
) -> str:
    return manifest_sha(repo_root, tardis_sample_root, scenario_id)


def spread_calibration_manifest_sha(
    repo_root: str | Path,
    spread_calibration_root: str | Path,
    scenario_id: str,
) -> str:
    return manifest_sha(repo_root, spread_calibration_root, scenario_id)


def stress_selector_input_sha(
    repo_root: str | Path,
    stress_selector_input_path: str | Path | None,
) -> str:
    if stress_selector_input_path is None:
        return MISSING_SHA
    p = _resolve_path(repo_root, stress_selector_input_path)
    return _sha256_file(p) if p.is_file() else MISSING_SHA


def precalibration_artifact_sha(
    repo_root: str | Path,
    precalibration_artifact_path: str | Path | None,
) -> str:
    if precalibration_artifact_path is None:
        return MISSING_SHA
    p = _resolve_path(repo_root, precalibration_artifact_path)
    return _sha256_file(p) if p.is_file() else MISSING_SHA


def _load_json_object(path: Path, label: str) -> dict[str, Any]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise PreregistrationError(f"{label} is not valid JSON: {exc}") from exc
    if not isinstance(data, dict):
        raise PreregistrationError(f"{label} must be a JSON object")
    return data


def _json_object_if_present(path: Path) -> dict[str, Any] | None:
    if not path.is_file():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return None
    return data if isinstance(data, dict) else None


def spread_calibration_precalibration_config_sha(
    repo_root: str | Path,
    spread_calibration_root: str | Path,
    scenario_id: str,
) -> str:
    p = _resolve_path(repo_root, spread_calibration_root) / scenario_id / "manifest.json"
    data = _json_object_if_present(p)
    if data is None:
        return MISSING_SHA
    value = data.get("precalibration_config_sha256")
    return value if isinstance(value, str) else MISSING_SHA


def _precalibration_config() -> dict[str, Any]:
    return {
        "schema_version": PRECALIBRATION_SCHEMA_VERSION,
        "scenario_id": DEFAULT_SCENARIO_ID,
        "sample_months": list(TARDIS_SAMPLE_MONTHS),
        "cost_budget_bar": copy.deepcopy(PLAN_COST_BUDGET_BAR),
        "binning": copy.deepcopy(PLAN_BINNING),
        "regime_labels": copy.deepcopy(PLAN_REGIME_LABELS),
        "unit_conversions": copy.deepcopy(PLAN_UNIT_CONVERSIONS),
        "fold_causal_calibration_rule": PLAN_FOLD_CAUSAL_CALIBRATION_RULE,
        "fail_closed_rules": copy.deepcopy(PLAN_FAIL_CLOSED_RULES),
        "promising_confirmation_trigger": PLAN_PROMISING_CONFIRMATION_TRIGGER,
    }


def precalibration_config() -> dict[str, Any]:
    """Return the frozen pre-calibration constants, without a self-referential hash."""
    return copy.deepcopy(_precalibration_config())


def precalibration_config_sha256() -> str:
    """Hash derived only from frozen pre-calibration constants."""
    return _canonical_hash(_precalibration_config())


def build_precalibration_artifact() -> dict[str, Any]:
    """Build the deterministic governance artifact emitted before calibration output exists."""
    config = _precalibration_config()
    config_sha = _canonical_hash(config)
    content = {
        "schema_version": PRECALIBRATION_SCHEMA_VERSION,
        "artifact_id": f"vrp-free-precalibration-{config_sha[:12]}",
        "scenario_id": DEFAULT_SCENARIO_ID,
        "precalibration_config": config,
        "precalibration_config_sha256": config_sha,
        "calibration_output_required": False,
        "emittable_before_calibration": True,
    }
    return {**content, "artifact_content_hash": _canonical_hash(content)}


def write_precalibration_artifact(
    repo_root: str | Path,
    *,
    out_dir: str | Path = DEFAULT_PRECALIBRATION_OUT_DIR,
) -> Path:
    """Emit docs/preregistration/vrp-free-precalibration-<hash>.json deterministically."""
    artifact = build_precalibration_artifact()
    dest_dir = _resolve_path(repo_root, out_dir)
    dest_dir.mkdir(parents=True, exist_ok=True)
    dest = dest_dir / f"{artifact['artifact_id']}.json"
    dest.write_text(json.dumps(artifact, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return dest


def load_precalibration_artifact(path: str | Path) -> dict[str, Any]:
    p = Path(path)
    if not p.is_file():
        raise PreregistrationError(f"missing VRP-free pre-calibration artifact: {p}")
    data = _load_json_object(p, "VRP-free pre-calibration artifact")
    if data.get("schema_version") != PRECALIBRATION_SCHEMA_VERSION:
        raise PreregistrationError(
            "VRP-free pre-calibration schema_version "
            f"{data.get('schema_version')!r} != {PRECALIBRATION_SCHEMA_VERSION!r}"
        )
    expected = precalibration_config_sha256()
    if data.get("precalibration_config_sha256") != expected:
        raise PreregistrationError(
            "VRP-free pre-calibration config hash drift: "
            f"{data.get('precalibration_config_sha256')!r} != {expected!r}"
        )
    if data.get("precalibration_config") != _precalibration_config():
        raise PreregistrationError("VRP-free pre-calibration constants drift")
    return data


def precalibration_artifact_config_sha(
    repo_root: str | Path,
    precalibration_artifact_path: str | Path | None,
) -> str:
    if precalibration_artifact_path is None:
        return MISSING_SHA
    p = _resolve_path(repo_root, precalibration_artifact_path)
    data = _json_object_if_present(p)
    if data is None:
        return MISSING_SHA
    value = data.get("precalibration_config_sha256")
    return value if isinstance(value, str) else MISSING_SHA


def _settings_snapshot() -> dict[str, Any]:
    """Decision-relevant existing Settings fields for options cost/risk sizing."""
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
    fallback = {
        "capital_usd_min": 500.0,
        "capital_usd_max": 2000.0,
        "default_capital_usd": 1000.0,
        "reserve_pct": 0.25,
        "max_position_pct": 0.25,
        "max_drawdown_pct": 0.05,
        "base_leverage": 2.0,
        "max_leverage": 5.0,
        "min_liq_distance_pct": 0.15,
        "health_factor_floor": 1.5,
        "gap_stress_pct": 0.20,
        "vol_spike_annual": 1.0,
        "perp_taker_fee_bps": 5.5,
        "perp_maker_fee_bps": 2.0,
        "spot_taker_fee_bps": 10.0,
        "leverage_cost_apr": 0.0,
        "slippage_base_bps": 1.0,
        "slippage_impact_bps_per_pct_volume": 5.0,
        "slippage_cap_bps": 50.0,
    }
    try:
        from ..config import Settings
    except ModuleNotFoundError:
        return fallback

    s = Settings()
    return {field: getattr(s, field) for field in fields if hasattr(s, field)}


def _frozen_plan() -> dict[str, Any]:
    """The VRP-free plan-constant block hashed into the run identity."""
    pre_config = _precalibration_config()
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
        "reconstruction_config": copy.deepcopy(PLAN_RECONSTRUCTION_CONFIG),
        "structure_grid": copy.deepcopy(PLAN_STRUCTURE_GRID),
        "cost_path": copy.deepcopy(PLAN_COST_PATH),
        "cost_budget_bar": copy.deepcopy(PLAN_COST_BUDGET_BAR),
        "precalibration_config": pre_config,
        "precalibration_config_sha256": _canonical_hash(pre_config),
        "source_quality_bridge": copy.deepcopy(PLAN_SOURCE_QUALITY_BRIDGE),
        "greek_provenance": copy.deepcopy(PLAN_GREEK_PROVENANCE),
        "settlement": copy.deepcopy(PLAN_SETTLEMENT),
        "stress_rule": copy.deepcopy(PLAN_STRESS_RULE),
        "trial_budget": copy.deepcopy(PLAN_TRIAL_BUDGET),
        "outcome_rules": copy.deepcopy(PLAN_OUTCOME_RULES),
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
            f"scenario_id {scenario_id!r} is not vrp-free authorizing; "
            f"expected {DEFAULT_SCENARIO_ID!r}"
        )


def _frozen_content(
    repo_root: str | Path,
    raw_source_root: str | Path,
    reconstructed_cache_root: str | Path,
    tardis_sample_root: str | Path,
    spread_calibration_root: str | Path,
    stress_windows: tuple[dict[str, Any], ...] | list[dict[str, Any]] | None,
    stress_selector_input_path: str | Path | None,
    precalibration_artifact_path: str | Path | None,
    extra_source_files: tuple[str, ...],
) -> dict[str, Any]:
    """All frozen fields that define the run identity, excluding run_id/content_hash."""
    source_hashes = compute_source_hashes(repo_root, extra=extra_source_files)
    raw_shas = {
        scenario: raw_source_manifest_sha(repo_root, raw_source_root, scenario)
        for scenario in SCENARIOS.values()
    }
    reconstructed_shas = {
        scenario: reconstructed_cache_manifest_sha(repo_root, reconstructed_cache_root, scenario)
        for scenario in SCENARIOS.values()
    }
    tardis_shas = {
        scenario: tardis_sample_manifest_sha(repo_root, tardis_sample_root, scenario)
        for scenario in SCENARIOS.values()
    }
    calibration_shas = {
        scenario: spread_calibration_manifest_sha(repo_root, spread_calibration_root, scenario)
        for scenario in SCENARIOS.values()
    }
    calibration_pre_shas = {
        scenario: spread_calibration_precalibration_config_sha(
            repo_root, spread_calibration_root, scenario
        )
        for scenario in SCENARIOS.values()
    }
    stress_path = _display_path(repo_root, stress_selector_input_path)
    precalibration_path = _display_path(repo_root, precalibration_artifact_path)
    return {
        "plan": _frozen_plan(),
        "settings_snapshot": _settings_snapshot(),
        "source_hashes": source_hashes,
        "raw_source_root": str(raw_source_root),
        "reconstructed_cache_root": str(reconstructed_cache_root),
        "tardis_sample_root": str(tardis_sample_root),
        "spread_calibration_root": str(spread_calibration_root),
        "raw_source_manifest_sha256": raw_shas,
        "reconstructed_cache_manifest_sha256": reconstructed_shas,
        "tardis_sample_manifest_sha256": tardis_shas,
        "spread_calibration_manifest_sha256": calibration_shas,
        "spread_calibration_precalibration_config_sha256": calibration_pre_shas,
        "precalibration_config_sha256": precalibration_config_sha256(),
        "precalibration_artifact_path": precalibration_path,
        "precalibration_artifact_sha256": precalibration_artifact_sha(
            repo_root, precalibration_artifact_path
        ),
        "precalibration_artifact_config_sha256": precalibration_artifact_config_sha(
            repo_root, precalibration_artifact_path
        ),
        "stress_windows": _normalize_stress_windows(stress_windows),
        "stress_selector_input_path": stress_path,
        "stress_selector_input_sha256": stress_selector_input_sha(
            repo_root, stress_selector_input_path
        ),
    }


def build_preregistration(
    repo_root: str | Path,
    *,
    raw_source_root: str | Path = DEFAULT_RAW_SOURCE_ROOT,
    reconstructed_cache_root: str | Path = DEFAULT_RECONSTRUCTED_CACHE_ROOT,
    tardis_sample_root: str | Path = DEFAULT_TARDIS_SAMPLE_ROOT,
    spread_calibration_root: str | Path = DEFAULT_SPREAD_CALIBRATION_ROOT,
    raw_cache_root: str | Path | None = None,
    cache_root: str | Path | None = None,
    scenario_id: str = DEFAULT_SCENARIO_ID,
    stress_windows: tuple[dict[str, Any], ...] | list[dict[str, Any]] | None = None,
    stress_selector_input_path: str | Path | None = None,
    precalibration_artifact_path: str | Path | None = None,
    extra_source_files: tuple[str, ...] = (),
) -> dict[str, Any]:
    """Assemble the VRP-free pre-registration; run_id derives from frozen content."""
    _validate_scenario_id(scenario_id)
    if raw_cache_root is not None:
        raw_source_root = raw_cache_root
    if cache_root is not None:
        reconstructed_cache_root = cache_root
    content = _frozen_content(
        repo_root,
        raw_source_root,
        reconstructed_cache_root,
        tardis_sample_root,
        spread_calibration_root,
        stress_windows,
        stress_selector_input_path,
        precalibration_artifact_path,
        extra_source_files,
    )
    content_hash = _canonical_hash(content)
    run_id = f"vrp-free-{content_hash[:12]}"
    return {
        "schema_version": SCHEMA_VERSION,
        "run_id": run_id,
        "content_hash": content_hash,
        **content,
    }


def load_preregistration(path: str | Path) -> dict[str, Any]:
    p = Path(path)
    if not p.is_file():
        raise PreregistrationError(f"missing VRP-free pre-registration artifact: {p}")
    data = _load_json_object(p, "VRP-free pre-registration")
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
        ("reconstruction_config", "reconstruction-config drift"),
        ("structure_grid", "structure-grid drift"),
        ("cost_path", "cost-path drift"),
        ("cost_budget_bar", "cost-budget-bar drift"),
        ("precalibration_config", "pre-calibration config drift"),
        ("precalibration_config_sha256", "pre-calibration config hash drift"),
        ("source_quality_bridge", "source-quality bridge drift"),
        ("greek_provenance", "greek-provenance drift"),
        ("settlement", "settlement/currency drift"),
        ("stress_rule", "stress-rule drift"),
        ("trial_budget", "trial-budget drift"),
        ("outcome_rules", "outcome-rule drift"),
        ("risk_limits", "risk-limit drift"),
    )
    for key, message in surface_messages:
        if artifact_plan.get(key) != current_plan[key]:
            mismatches.append(message)
    if artifact_plan != current_plan:
        mismatches.append("plan-constant drift (VRP-free frozen plan surfaces changed)")


def _append_dict_sha_mismatches(
    artifact: dict[str, Any],
    current: dict[str, Any],
    key: str,
    message: str,
    mismatches: list[str],
) -> None:
    frozen = artifact.get(key, {})
    if not isinstance(frozen, dict):
        mismatches.append(f"{key} missing or not an object")
        frozen = {}
    for scenario, h in current[key].items():
        if frozen.get(scenario) != h:
            mismatches.append(f"{message}: {scenario}")


def verify_preregistration(
    artifact: dict[str, Any],
    repo_root: str | Path,
    *,
    raw_source_root: str | Path | None = None,
    reconstructed_cache_root: str | Path | None = None,
    tardis_sample_root: str | Path | None = None,
    spread_calibration_root: str | Path | None = None,
    raw_cache_root: str | Path | None = None,
    cache_root: str | Path | None = None,
    scenario_id: str = DEFAULT_SCENARIO_ID,
    stress_windows: tuple[dict[str, Any], ...] | list[dict[str, Any]] | None = None,
    stress_selector_input_path: str | Path | None = None,
    precalibration_artifact_path: str | Path | None = None,
    extra_source_files: tuple[str, ...] = (),
) -> VerifyResult:
    """Recompute every frozen VRP-free hash and return invalid on ANY drift."""
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
        raw_source_root
        or raw_cache_root
        or artifact.get("raw_source_root", DEFAULT_RAW_SOURCE_ROOT)
    )
    current_reconstructed_root = str(
        reconstructed_cache_root
        or cache_root
        or artifact.get("reconstructed_cache_root", DEFAULT_RECONSTRUCTED_CACHE_ROOT)
    )
    current_tardis_root = str(
        tardis_sample_root or artifact.get("tardis_sample_root", DEFAULT_TARDIS_SAMPLE_ROOT)
    )
    current_calibration_root = str(
        spread_calibration_root
        or artifact.get("spread_calibration_root", DEFAULT_SPREAD_CALIBRATION_ROOT)
    )
    current_stress_path = stress_selector_input_path
    artifact_stress_path = artifact.get("stress_selector_input_path")
    if current_stress_path is None and isinstance(artifact_stress_path, str):
        current_stress_path = artifact_stress_path
    current_precalibration_path = precalibration_artifact_path
    artifact_precalibration_path = artifact.get("precalibration_artifact_path")
    if current_precalibration_path is None and isinstance(artifact_precalibration_path, str):
        current_precalibration_path = artifact_precalibration_path

    current = _frozen_content(
        repo_root,
        current_raw_root,
        current_reconstructed_root,
        current_tardis_root,
        current_calibration_root,
        stress_windows,
        current_stress_path,
        current_precalibration_path,
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

    _append_dict_sha_mismatches(
        artifact,
        current,
        "raw_source_manifest_sha256",
        "raw-source manifest drift",
        mismatches,
    )
    _append_dict_sha_mismatches(
        artifact,
        current,
        "reconstructed_cache_manifest_sha256",
        "reconstructed-cache manifest drift",
        mismatches,
    )
    _append_dict_sha_mismatches(
        artifact,
        current,
        "tardis_sample_manifest_sha256",
        "Tardis-sample manifest drift",
        mismatches,
    )
    _append_dict_sha_mismatches(
        artifact,
        current,
        "spread_calibration_manifest_sha256",
        "spread-calibration manifest drift",
        mismatches,
    )
    _append_dict_sha_mismatches(
        artifact,
        current,
        "spread_calibration_precalibration_config_sha256",
        "spread-calibration pre-calibration config drift",
        mismatches,
    )

    if artifact.get("precalibration_config_sha256") != current["precalibration_config_sha256"]:
        mismatches.append("pre-calibration config hash drift")
    if artifact.get("precalibration_artifact_path") != current["precalibration_artifact_path"]:
        mismatches.append("pre-calibration artifact path drift")
    if artifact.get("precalibration_artifact_sha256") != current["precalibration_artifact_sha256"]:
        mismatches.append("pre-calibration artifact drift")
    if (
        artifact.get("precalibration_artifact_config_sha256")
        != current["precalibration_artifact_config_sha256"]
    ):
        mismatches.append("pre-calibration artifact config hash drift")
    if current["precalibration_artifact_sha256"] != MISSING_SHA and (
        current["precalibration_artifact_config_sha256"]
        != current["precalibration_config_sha256"]
    ):
        mismatches.append("pre-calibration artifact does not match frozen config")

    for scenario, manifest_hash in current["spread_calibration_manifest_sha256"].items():
        if manifest_hash == MISSING_SHA:
            continue
        if (
            current["spread_calibration_precalibration_config_sha256"].get(scenario)
            != current["precalibration_config_sha256"]
        ):
            mismatches.append(
                "spread-calibration output was not produced from unchanged "
                f"pre-calibration config: {scenario}"
            )

    if artifact.get("stress_windows") != current["stress_windows"]:
        mismatches.append("stress-window drift")
    if artifact.get("stress_selector_input_path") != current["stress_selector_input_path"]:
        mismatches.append("stress-selector-input path drift")
    if artifact.get("stress_selector_input_sha256") != current["stress_selector_input_sha256"]:
        mismatches.append("stress-selector-input manifest drift")

    recomputed_hash = _canonical_hash(current)
    if artifact.get("content_hash") != recomputed_hash:
        mismatches.append("content_hash drift (VRP-free frozen content changed)")
    expected_run_id = f"vrp-free-{recomputed_hash[:12]}"
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
            f"{label} manifest path is required for VRP-free artifact emit"
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


def _required_file(repo_root: str | Path, path: str | Path | None, label: str) -> str:
    if path is None:
        raise PreregistrationError(f"{label} path is required for VRP-free artifact emit")
    p = _resolve_path(repo_root, path)
    if not p.is_file():
        raise PreregistrationError(f"{label} path does not exist: {p}")
    return _display_path(repo_root, p) or p.as_posix()


def _require_precalibration_artifact_matches(repo_root: str | Path, path: str | Path) -> None:
    p = _resolve_path(repo_root, path)
    load_precalibration_artifact(p)


def _require_calibration_manifest_matches_precalibration(
    repo_root: str | Path,
    spread_calibration_manifest_path: str | Path,
) -> None:
    p = _resolve_path(repo_root, spread_calibration_manifest_path)
    data = _load_json_object(p, "VRP-free spread calibration manifest")
    expected = precalibration_config_sha256()
    observed = data.get("precalibration_config_sha256")
    if observed != expected:
        raise PreregistrationError(
            "spread calibration manifest was not produced from unchanged "
            f"pre-calibration config: {observed!r} != {expected!r}"
        )


def write_preregistration(
    repo_root: str | Path,
    *,
    raw_manifest_path: str | Path | None = None,
    reconstructed_manifest_path: str | Path | None = None,
    tardis_sample_manifest_path: str | Path | None = None,
    spread_calibration_manifest_path: str | Path | None = None,
    precalibration_artifact_path: str | Path | None = None,
    scenario_id: str = DEFAULT_SCENARIO_ID,
    stress_windows: tuple[dict[str, Any], ...] | list[dict[str, Any]] | None = None,
    stress_selector_input_path: str | Path | None = None,
    extra_source_files: tuple[str, ...] = (),
    out_dir: str = DEFAULT_PRECALIBRATION_OUT_DIR,
) -> Path:
    """Emit docs/preregistration/vrp-free-*.json only after required manifests exist."""
    _validate_scenario_id(scenario_id)
    raw_root = _manifest_root_from_path(repo_root, raw_manifest_path, scenario_id, "raw-source")
    reconstructed_root = _manifest_root_from_path(
        repo_root, reconstructed_manifest_path, scenario_id, "reconstructed-cache"
    )
    tardis_root = _manifest_root_from_path(
        repo_root, tardis_sample_manifest_path, scenario_id, "Tardis-sample"
    )
    calibration_root = _manifest_root_from_path(
        repo_root, spread_calibration_manifest_path, scenario_id, "spread-calibration"
    )
    precalibration_path = _required_file(
        repo_root, precalibration_artifact_path, "pre-calibration artifact"
    )
    _required_file(repo_root, stress_selector_input_path, "stress selector input")
    _require_precalibration_artifact_matches(repo_root, precalibration_path)
    _require_calibration_manifest_matches_precalibration(
        repo_root, spread_calibration_manifest_path  # type: ignore[arg-type]
    )

    artifact = build_preregistration(
        repo_root,
        raw_source_root=raw_root,
        reconstructed_cache_root=reconstructed_root,
        tardis_sample_root=tardis_root,
        spread_calibration_root=calibration_root,
        scenario_id=scenario_id,
        stress_windows=stress_windows,
        stress_selector_input_path=stress_selector_input_path,
        precalibration_artifact_path=precalibration_path,
        extra_source_files=extra_source_files,
    )
    result = verify_preregistration(
        artifact,
        repo_root,
        scenario_id=scenario_id,
        stress_windows=stress_windows,
        stress_selector_input_path=stress_selector_input_path,
        precalibration_artifact_path=precalibration_path,
        extra_source_files=extra_source_files,
    )
    if not result.valid:
        raise PreregistrationError(
            "refusing to emit invalid VRP-free pre-registration: "
            + "; ".join(result.mismatches)
        )
    dest_dir = _resolve_path(repo_root, out_dir)
    dest_dir.mkdir(parents=True, exist_ok=True)
    dest = dest_dir / f"{artifact['run_id']}.json"
    dest.write_text(json.dumps(artifact, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return dest


def _contains_forbidden_go(value: Any) -> bool:
    if isinstance(value, dict):
        return any(_contains_forbidden_go(v) for v in value.values())
    if isinstance(value, (list, tuple, set)):
        return any(_contains_forbidden_go(v) for v in value)
    return isinstance(value, str) and value == "GO"


def _contains_venue_masquerade(payload: dict[str, Any]) -> bool:
    venue_values = {SourceQuality.VENUE.value, SourceQuality.VENUE.name, "SourceQuality.VENUE"}
    safe_bridge_keys = {
        "forbidden_reconstructed_option_leg_source_quality",
        "forbid_venue",
    }

    def walk(value: Any, parent_key: str = "") -> bool:
        if isinstance(value, dict):
            return any(walk(v, k) for k, v in value.items())
        if isinstance(value, (list, tuple, set)):
            return any(walk(v, parent_key) for v in value)
        return parent_key not in safe_bridge_keys and value in venue_values

    return walk(payload)


def free_lineage_mismatches(payload: dict[str, Any]) -> tuple[str, ...]:
    """Thin Phase-0 guard for reconstructed/free-data report lineage."""
    mismatches: list[str] = []
    allowed = set(PLAN_OUTCOME_RULES["allowed_outcomes"])
    verdict = payload.get("verdict", payload.get("outcome"))
    if verdict is not None and verdict not in allowed:
        mismatches.append(f"free verdict outcome {verdict!r} is not allowed")
    if _contains_forbidden_go(payload):
        mismatches.append("GO payload is invalid lineage for VRP-free")
    if _contains_venue_masquerade(payload):
        mismatches.append("VENUE masquerade is invalid lineage for reconstructed data")
    if payload.get("free_source_quality") != PLAN_SOURCE_QUALITY_BRIDGE["free_source_quality"]:
        mismatches.append("missing or invalid free_source_quality")
    if payload.get("spread_source_quality") != PLAN_SOURCE_QUALITY_BRIDGE["spread_source_quality"]:
        mismatches.append("missing or invalid spread_source_quality")
    if (
        payload.get("non_authorizing_reason")
        != PLAN_SOURCE_QUALITY_BRIDGE["non_authorizing_reason"]
    ):
        mismatches.append("missing or invalid non_authorizing_reason")
    if payload.get("authorizing") is not False:
        mismatches.append("authorizing must be false for VRP-free reconstructed evidence")
    if payload.get("capital_go_allowed") is not False:
        mismatches.append("capital_go_allowed must be false for VRP-free reconstructed evidence")
    if payload.get("uses_committed_authorizing_verdict") is True:
        mismatches.append("committed authorizing verdict reuse is invalid lineage")
    return tuple(mismatches)


def validate_free_lineage_payload(payload: dict[str, Any]) -> VerifyResult:
    mismatches = free_lineage_mismatches(payload)
    return VerifyResult(
        valid=not mismatches,
        run_status="valid" if not mismatches else "invalid",
        mismatches=mismatches,
    )


def max_free_verdict_for_valid_reconstructed_evidence(
    *,
    economics_pass: bool,
    inconclusive: bool = False,
) -> str:
    """Positive reconstructed evidence is capped at PROMISING_PENDING_REAL_SPREAD."""
    if inconclusive:
        return "INCONCLUSIVE"
    if economics_pass:
        return "PROMISING_PENDING_REAL_SPREAD"
    return "NO_GO"

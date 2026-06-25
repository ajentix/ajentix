"""Pure Stage-1 Edge Verdict report and decision types.

The verdict core is intentionally independent from cache loading and backtest execution so
unit tests can prove that a GO is impossible without real venue provenance.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from enum import StrEnum
from typing import Any

PERIODS_PER_YEAR_8H = 365 * 3


class Verdict(StrEnum):
    GO = "go"
    NO_GO = "no_go"
    INCONCLUSIVE = "inconclusive"


@dataclass(frozen=True)
class EdgeVerdictThresholds:
    min_sharpe: float = 1.5
    max_mdd: float = 0.05
    min_net_apr: float = 0.0


def net_apr_simple(
    *,
    final_equity: float,
    initial_equity: float,
    n_test_periods: int,
    periods_per_year: int = PERIODS_PER_YEAR_8H,
) -> float:
    """Simple non-compounded annualized net return over an 8h funding test window.

    Formula: ``(final_equity / initial_equity - 1) * (periods_per_year / n_test_periods)``.
    The backtest ledger's ``final_equity`` is already net of fees, funding, slippage, and
    leverage cost on the delta-neutral account equity at the configured small-cap equity.
    """

    if n_test_periods <= 0:
        raise ValueError("n_test_periods must be positive")
    if not math.isfinite(final_equity):
        raise ValueError("final_equity must be finite")
    if not math.isfinite(initial_equity) or initial_equity <= 0.0:
        raise ValueError("initial_equity must be finite and positive")
    return (final_equity / initial_equity - 1.0) * (periods_per_year / n_test_periods)


def decide_verdict(
    *,
    sharpe: float,
    mdd: float,
    net_apr: float,
    all_streams_venue: bool,
    train_test_valid: bool,
    test_periods: int,
    min_test_periods: int,
    collapse: bool,
    thresholds: EdgeVerdictThresholds,
) -> tuple[Verdict, list[str]]:
    """Return the honest GO/NO-GO/INCONCLUSIVE decision and canonical reasons."""

    inconclusive: list[str] = []
    if not all_streams_venue:
        inconclusive.append(
            "real venue data required: one or more required streams are not source_quality=venue"
        )
    if not train_test_valid:
        inconclusive.append("train/test split required")
    if test_periods < min_test_periods:
        inconclusive.append("insufficient test window")
    if inconclusive:
        return Verdict.INCONCLUSIVE, inconclusive

    if collapse:
        return Verdict.NO_GO, ["test-vs-train performance collapse"]

    failures: list[str] = []
    if not math.isfinite(sharpe):
        failures.append("sharpe is not finite")
    elif sharpe < thresholds.min_sharpe:
        failures.append(f"sharpe {sharpe:.12g} < {thresholds.min_sharpe:.12g}")

    if not math.isfinite(mdd):
        failures.append("mdd is not finite")
    elif mdd > thresholds.max_mdd:
        failures.append(f"mdd {mdd:.12g} > {thresholds.max_mdd:.12g}")

    if not math.isfinite(net_apr):
        failures.append("net_apr is not finite")
    elif net_apr < thresholds.min_net_apr:
        failures.append(f"net_apr {net_apr:.12g} < {thresholds.min_net_apr:.12g}")

    if failures:
        return Verdict.NO_GO, failures
    return Verdict.GO, []


@dataclass(frozen=True)
class EdgeVerdictReport:
    scenario_id: str
    schema_version: str
    manifest_sha256: str
    generator_version: str
    verdict: Verdict
    reasons: list[str]
    equity_usd: float
    per_setup_notional_usd: float
    train_until_ms: int | None
    param_freeze_hash: str | None
    source_quality: dict[str, str]
    selected_params: dict[str, float]
    train_metrics: dict[str, Any]
    test_metrics: dict[str, Any]
    event_counts: dict[str, Any]
    liquidated: bool
    sensitivity: list[dict[str, Any]]
    caveats: list[str]

    def to_json(self) -> dict[str, Any]:
        """Return a deterministic, JSON-safe report payload."""

        return {
            "scenario_id": self.scenario_id,
            "schema_version": self.schema_version,
            "manifest_sha256": self.manifest_sha256,
            "generator_version": self.generator_version,
            "verdict": self.verdict.value,
            "reasons": list(self.reasons),
            "equity_usd": _json_value(self.equity_usd),
            "per_setup_notional_usd": _json_value(self.per_setup_notional_usd),
            "train_until_ms": self.train_until_ms,
            "param_freeze_hash": self.param_freeze_hash,
            "source_quality": _json_mapping(self.source_quality),
            "selected_params": _json_mapping(self.selected_params),
            "train_metrics": _json_mapping(self.train_metrics),
            "test_metrics": _json_mapping(self.test_metrics),
            "event_counts": _json_mapping(self.event_counts),
            "liquidated": self.liquidated,
            "sensitivity": [_json_mapping(row) for row in self.sensitivity],
            "caveats": list(self.caveats),
        }

    def to_markdown(self) -> str:
        """Return a compact human-review report."""

        lines = [
            "# Stage-1 Edge Verdict",
            "",
            f"- scenario_id: `{self.scenario_id}`",
            f"- verdict: **{self.verdict.value.upper()}**",
            f"- schema_version: `{self.schema_version}`",
            f"- manifest_sha256: `{self.manifest_sha256}`",
            f"- generator_version: `{self.generator_version}`",
            f"- equity_usd: `{_format_value(self.equity_usd)}`",
            f"- per_setup_notional_usd: `{_format_value(self.per_setup_notional_usd)}`",
            f"- train_until_ms: `{self.train_until_ms}`",
            f"- param_freeze_hash: `{self.param_freeze_hash}`",
            "",
            "## Reasons",
            *(f"- {reason}" for reason in (self.reasons or ["none"])),
            "",
            "## Source quality",
            *(f"- {name}: `{quality}`" for name, quality in sorted(self.source_quality.items())),
            "",
            "## Frozen strategy params",
            *(
                f"- {name}: `{_format_value(value)}`"
                for name, value in sorted(self.selected_params.items())
            ),
            "",
            "## TRAIN metrics (printed; train is used only for param selection)",
            *(_metric_lines(self.train_metrics)),
            "",
            "## TEST metrics (printed; Edge Verdict is not a CI hard performance gate)",
            *(_metric_lines(self.test_metrics)),
            "",
            "## Event counts",
            *(
                f"- {name}: `{_format_value(value)}`"
                for name, value in sorted(self.event_counts.items())
            ),
            f"- liquidated: `{self.liquidated}`",
            "",
            "## Sensitivity",
            *(_sensitivity_lines(self.sensitivity)),
            "",
            "## Caveats",
            *(f"- {caveat}" for caveat in self.caveats),
        ]
        return "\n".join(lines).rstrip() + "\n"


def _json_mapping(mapping: dict[str, Any]) -> dict[str, Any]:
    return {str(key): _json_value(mapping[key]) for key in sorted(mapping)}


def _json_value(value: Any) -> Any:
    if isinstance(value, StrEnum):
        return value.value
    if isinstance(value, dict):
        return _json_mapping({str(k): v for k, v in value.items()})
    if isinstance(value, (list, tuple)):
        return [_json_value(item) for item in value]
    if isinstance(value, float):
        if math.isfinite(value):
            return value
        if math.isinf(value):
            return "inf" if value > 0 else "-inf"
        return "nan"
    return value


def _metric_lines(metrics: dict[str, Any]) -> list[str]:
    if not metrics:
        return ["- none"]
    return [f"- {name}: `{_format_value(metrics[name])}`" for name in sorted(metrics)]


def _sensitivity_lines(rows: list[dict[str, Any]]) -> list[str]:
    if not rows:
        return ["- none"]
    out: list[str] = []
    for row in rows:
        rendered = ", ".join(f"{key}={_format_value(row[key])}" for key in sorted(row))
        out.append(f"- {rendered}")
    return out


def _format_value(value: Any) -> str:
    value = _json_value(value)
    if isinstance(value, float):
        return f"{value:.12g}"
    return str(value)

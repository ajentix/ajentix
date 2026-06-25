"""Non-authorizing USD-consistent VRP skew measurement over the frozen walk-forward folds.

This resolves the single open question in ``docs/research/full_frozen_run_findings.md``: the frozen
strategy's ETH-credit-vs-USD-width unit bug made the official walk-forward select zero structures in
all seven folds, so the clean fold-level economics of the ETH OTM put-skew credit-spread edge were
never measured.

Two facts shape the design:

  1. On USD-projected snapshots (``ajentix_quant.options.usd_projection``) the identical search
     space, leg selection, and ``credit / width`` entry bar are finally dimensionally consistent and
     do select structures (the frozen path selects none purely from the unit bug).
  2. The committed breakeven / walk-forward *authorization* gate deliberately refuses to select or
     authorize on reconstructed (``fixture``) source quality (branch ``INCONCLUSIVE`` /
     ``NON_AUTHORIZING_FIXTURE``). That gate is correct: a capital GO is impossible from this
     data quality. But it means the authorization path cannot be reused to *measure* economics.

So this module measures the edge with an explicit, non-authorizing "enter-all" characterization:
for each held-out fold it backtests every structure the USD-eval strategy emits to European
settlement through the committed VRP engine (gross of effective spread, net of taker fees), then
applies a documented effective-spread haircut (from the full real-data run) for a net band, and
reports per-fold return-on-risk and a fold-level Sharpe. It never authorizes capital.
"""

from __future__ import annotations

import bisect
from collections.abc import Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from ajentix_quant.backtest.metrics import sharpe
from ajentix_quant.backtest.option_costs import evaluate_structure_costs
from ajentix_quant.backtest.vrp_engine import VrpBacktestStep, run_vrp_backtest
from ajentix_quant.data.options_cache import load_normalized_cache
from ajentix_quant.options.types import DefinedRiskStructure, OptionChainSnapshot
from ajentix_quant.options.usd_projection import USD_PROJECTION_SOURCE, project_snapshot_to_usd
from ajentix_quant.research.vrp_preregistration import PLAN_FOLDS, PLAN_PRIMARY_EQUITY
from ajentix_quant.strategies.vrp_defined_risk_usd_eval import (
    EVAL_NON_AUTHORIZING_LABEL,
    VrpDefinedRiskUsdEvalStrategy,
)

USD_EVAL_SCHEMA_VERSION = "aq-vrp-skew-usd-eval-v1"
REPORT_STEM = "vrp_skew_usd_eval"
DEFAULT_SCENARIO_ID = "deribit_history_eth_vrp_free_v1"

_MS_PER_DAY = 86_400_000
_DIAGNOSTIC_MAX_QUOTE_AGE_S = 10**9

# Documented effective round-trip spread per structure, from the full real-data run
# (docs/research/full_frozen_run_findings.md: p50 ~ $2.5-4, p75 ~ $4-7 across 64,223 samples).
# Upper end of each band is used for conservatism. This is a flat, transparent haircut, not a fresh
# per-structure calibration; it is non-authorizing and clearly labelled in the report.
EFFECTIVE_SPREAD_P50_USD = 4.0
EFFECTIVE_SPREAD_P75_USD = 7.0
_SHARPE_BAR = 1.5  # README validation-gate bar; reported, never authorizing.

# Measurement signal codes (all non-authorizing; a capital GO is never emitted here).
SIGNAL_NO_ENTRIES = "NO_ENTRIES_SELECTED"
SIGNAL_NET_NEGATIVE = "NET_NEGATIVE_AFTER_SPREAD"
SIGNAL_POSITIVE_SUBSCALE = "NET_POSITIVE_BUT_BELOW_SHARPE_BAR"
SIGNAL_POSITIVE_MEETS_BAR = "NET_POSITIVE_MEETS_SHARPE_BAR_NON_AUTHORIZING"

__all__ = [
    "DEFAULT_SCENARIO_ID",
    "EFFECTIVE_SPREAD_P50_USD",
    "EFFECTIVE_SPREAD_P75_USD",
    "REPORT_STEM",
    "USD_EVAL_SCHEMA_VERSION",
    "measure_fold",
    "measure_fold_economics",
    "measurement_signal",
    "periods_per_year",
    "project_usd_snapshots",
    "run_usd_eval",
    "summarize_measurement",
]


def _parse_iso_ms(value: str) -> int:
    normalized = value[:-1] + "+00:00" if value.endswith("Z") else value
    return int(datetime.fromisoformat(normalized).astimezone(UTC).timestamp() * 1000)


def _iso_ms(ts_ms: int) -> str:
    return datetime.fromtimestamp(ts_ms / 1000, tz=UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def project_usd_snapshots(
    snapshots: Sequence[OptionChainSnapshot],
) -> tuple[OptionChainSnapshot, ...]:
    """Project every snapshot to USD units, dropping any without a usable ETH/USD rate."""

    projected: list[OptionChainSnapshot] = []
    for snapshot in snapshots:
        usd = project_snapshot_to_usd(snapshot)
        if usd is not None:
            projected.append(usd)
    return tuple(projected)


def _test_structures(
    usd_snapshots: Sequence[OptionChainSnapshot],
    *,
    symbol: str,
    start_ms: int,
    end_ms: int,
    strategy: VrpDefinedRiskUsdEvalStrategy,
) -> list[tuple[OptionChainSnapshot, DefinedRiskStructure]]:
    out: list[tuple[OptionChainSnapshot, DefinedRiskStructure]] = []
    for snapshot in usd_snapshots:
        if snapshot.underlying != symbol.upper():
            continue
        if not (start_ms <= snapshot.snapshot_ts_ms < end_ms):
            continue
        out.extend((snapshot, s) for s in strategy.construct_structures(snapshot))
    return out


def _load_index_path(csv_path: str | Path) -> tuple[list[int], list[float]]:
    """Load the real ETH index path (timestamp_ms,underlying,index_price) for expiry settlement."""

    timestamps: list[int] = []
    prices: list[float] = []
    with open(csv_path, encoding="utf-8") as handle:
        next(handle, None)  # header
        for line in handle:
            parts = line.rstrip("\n").split(",")
            if len(parts) < 3:
                continue
            timestamps.append(int(parts[0]))
            prices.append(float(parts[2]))
    return timestamps, prices


def _settle_price(
    index_ts: Sequence[int], index_px: Sequence[float], expiry_ms: int
) -> float | None:
    """Nearest-prior index price at expiry, or None when expiry is outside index coverage."""

    if not index_ts or expiry_ms < index_ts[0] or expiry_ms > index_ts[-1]:
        return None
    i = bisect.bisect_right(index_ts, expiry_ms)
    return float(index_px[i - 1]) if i > 0 else None


def measure_fold(
    usd_snapshots: Sequence[OptionChainSnapshot],
    fold: dict[str, str],
    *,
    symbol: str,
    strategy: VrpDefinedRiskUsdEvalStrategy,
    equity_usd: float,
    index_ts: Sequence[int],
    index_px: Sequence[float],
) -> dict[str, Any]:
    """Enter-all settlement backtest of one fold's test-window structures (non-authorizing).

    Each structure is held to European settlement at the real ETH index price observed at its
    expiry. Structures whose expiry falls outside index coverage cannot be settled and are
    excluded; structures whose executable taker credit is <= 0 are also excluded. Both are reported.
    """

    test_start_ms = _parse_iso_ms(str(fold["test_start"]))
    test_end_ms = _parse_iso_ms(str(fold["test_end"]))
    rows = _test_structures(
        usd_snapshots, symbol=symbol, start_ms=test_start_ms, end_ms=test_end_ms, strategy=strategy
    )
    non_executable = 0
    unsettleable = 0
    kept: list[tuple[OptionChainSnapshot, DefinedRiskStructure]] = []
    steps: list[VrpBacktestStep] = []
    for snapshot, structure in rows:
        try:
            breakdown = evaluate_structure_costs(structure, cost_mode="taker")
        except ValueError:
            non_executable += 1
            continue
        if breakdown.net_credit_usd <= 0.0:
            non_executable += 1
            continue
        settle = _settle_price(index_ts, index_px, structure.expiry_ms)
        if settle is None:
            unsettleable += 1
            continue
        steps.append(
            VrpBacktestStep(
                entry_timestamp_ms=snapshot.snapshot_ts_ms,
                structure=structure,
                entry_snapshot=snapshot,
                settlement_price=settle,
                cost_mode="taker",
            )
        )
        kept.append((snapshot, structure))
    backtest = run_vrp_backtest(steps, initial_equity_usd=equity_usd)
    entries = backtest.n_entries
    # realized_pnl is the linear sum of per-structure settlement PnL (credit kept minus capped
    # settlement loss), gross of the effective spread, net of taker fees.
    gross_pnl = float(backtest.realized_pnl_usd)
    total_max_loss = sum(float(bd.max_loss_usd) for bd in backtest.cost_breakdowns)
    credit_to_width = [float(s.net_credit) / float(s.width) for _, s in kept if s.width > 0]
    mean_cw = sum(credit_to_width) / len(credit_to_width) if credit_to_width else 0.0
    return {
        "fold_id": str(fold["id"]),
        "test_start": _iso_ms(test_start_ms),
        "test_end": _iso_ms(test_end_ms),
        "candidate_structures": len(rows),
        "non_executable_excluded": non_executable,
        "unsettleable_excluded": unsettleable,
        "entries": entries,
        "gross_pnl_usd": round(gross_pnl, 4),
        "total_max_loss_usd": round(total_max_loss, 4),
        "mean_credit_to_width": round(mean_cw, 4),
        "max_loss_invariant_ok": backtest.max_loss_invariant_ok,
    }


def measure_fold_economics(
    usd_snapshots: Sequence[OptionChainSnapshot],
    *,
    symbol: str = "ETH",
    folds: Sequence[dict[str, str]] = PLAN_FOLDS,
    equity_usd: float = PLAN_PRIMARY_EQUITY,
    index_ts: Sequence[int] = (),
    index_px: Sequence[float] = (),
) -> list[dict[str, Any]]:
    """Run the enter-all measurement for every fold, settling at the real index path."""

    strategy = VrpDefinedRiskUsdEvalStrategy(max_quote_age_s=_DIAGNOSTIC_MAX_QUOTE_AGE_S)
    return [
        measure_fold(
            usd_snapshots,
            fold,
            symbol=symbol,
            strategy=strategy,
            equity_usd=equity_usd,
            index_ts=index_ts,
            index_px=index_px,
        )
        for fold in folds
    ]


def periods_per_year(folds: Sequence[dict[str, str]]) -> float:
    """Annualization factor from the mean test-window length across folds."""

    spans = []
    for fold in folds:
        start = _parse_iso_ms(str(fold["test_start"]))
        end = _parse_iso_ms(str(fold["test_end"]))
        if end > start:
            spans.append((end - start) / _MS_PER_DAY)
    if not spans:
        return 0.0
    mean_days = sum(spans) / len(spans)
    return 365.0 / mean_days if mean_days > 0 else 0.0


def _net_pnl(gross_pnl: float, entries: int, spread_usd: float) -> float:
    return gross_pnl - entries * spread_usd


def measurement_signal(
    *, total_net_p50_usd: float, fold_sharpe_net_p50: float, total_entries: int
) -> str:
    """Non-authorizing measurement signal from the net economics (never a capital GO)."""

    if total_entries == 0:
        return SIGNAL_NO_ENTRIES
    if total_net_p50_usd <= 0.0:
        return SIGNAL_NET_NEGATIVE
    if fold_sharpe_net_p50 < _SHARPE_BAR:
        return SIGNAL_POSITIVE_SUBSCALE
    return SIGNAL_POSITIVE_MEETS_BAR


def summarize_measurement(
    fold_rows: Sequence[dict[str, Any]],
    *,
    folds: Sequence[dict[str, str]],
    equity_usd: float,
) -> dict[str, Any]:
    """Aggregate per-fold enter-all economics + net band + fold-level Sharpe (pure, no I/O)."""

    ppy = periods_per_year(folds)
    per_fold: list[dict[str, Any]] = []
    ror_gross: list[float] = []
    ror_net_p50: list[float] = []
    for row in fold_rows:
        entries = int(row["entries"])
        gross = float(row["gross_pnl_usd"])
        max_loss = float(row["total_max_loss_usd"])
        net_p50 = _net_pnl(gross, entries, EFFECTIVE_SPREAD_P50_USD)
        net_p75 = _net_pnl(gross, entries, EFFECTIVE_SPREAD_P75_USD)
        # Return on risk: PnL over total capital at risk in that fold (scale-free across lot sizes).
        g_ror = gross / max_loss if max_loss > 0 else 0.0
        n_ror = net_p50 / max_loss if max_loss > 0 else 0.0
        ror_gross.append(g_ror)
        ror_net_p50.append(n_ror)
        per_fold.append(
            {
                "fold_id": row["fold_id"],
                "entries": entries,
                "gross_pnl_usd": round(gross, 4),
                "net_p50_pnl_usd": round(net_p50, 4),
                "net_p75_pnl_usd": round(net_p75, 4),
                "total_max_loss_usd": round(max_loss, 4),
                "mean_credit_to_width": row.get("mean_credit_to_width"),
                "return_on_risk_gross": round(g_ror, 6),
                "return_on_risk_net_p50": round(n_ror, 6),
            }
        )
    total_entries = sum(int(r["entries"]) for r in fold_rows)
    total_gross = sum(float(r["gross_pnl_usd"]) for r in fold_rows)
    total_net_p50 = sum(
        _net_pnl(float(r["gross_pnl_usd"]), int(r["entries"]), EFFECTIVE_SPREAD_P50_USD)
        for r in fold_rows
    )
    total_net_p75 = sum(
        _net_pnl(float(r["gross_pnl_usd"]), int(r["entries"]), EFFECTIVE_SPREAD_P75_USD)
        for r in fold_rows
    )
    sharpe_gross = sharpe(ror_gross, ppy) if len(ror_gross) >= 2 else 0.0
    sharpe_net = sharpe(ror_net_p50, ppy) if len(ror_net_p50) >= 2 else 0.0
    signal = measurement_signal(
        total_net_p50_usd=total_net_p50,
        fold_sharpe_net_p50=sharpe_net,
        total_entries=total_entries,
    )
    return {
        "per_fold": per_fold,
        "aggregate": {
            "fold_count": len(fold_rows),
            "folds_with_entries": sum(1 for r in fold_rows if int(r["entries"]) > 0),
            "total_entries": total_entries,
            "total_gross_pnl_usd": round(total_gross, 4),
            "total_net_p50_pnl_usd": round(total_net_p50, 4),
            "total_net_p75_pnl_usd": round(total_net_p75, 4),
            "periods_per_year": round(ppy, 4),
            "fold_sharpe_return_on_risk_gross": round(sharpe_gross, 4),
            "fold_sharpe_return_on_risk_net_p50": round(sharpe_net, 4),
            "sharpe_bar": _SHARPE_BAR,
            "effective_spread_p50_usd": EFFECTIVE_SPREAD_P50_USD,
            "effective_spread_p75_usd": EFFECTIVE_SPREAD_P75_USD,
        },
        "measurement_signal": signal,
        "weak_signal_caveat": (
            f"Fold-level Sharpe is over {len(fold_rows)} held-out folds (a small, weak sample), on "
            "an enter-all characterization (every emitted structure, one unit each) with a flat "
            "effective-spread haircut. It measures whether a clean edge exists, not a live "
            "authorization."
        ),
    }


def run_usd_eval(
    *,
    repo_root: str,
    raw_source_root: str,
    reconstructed_cache_root: str,
    scenario_id: str = DEFAULT_SCENARIO_ID,
    symbol: str = "ETH",
    folds: Sequence[dict[str, str]] = PLAN_FOLDS,
    equity_usd: float = PLAN_PRIMARY_EQUITY,
) -> dict[str, Any]:
    """Run the non-authorizing USD-consistent skew measurement from the local cache.

    ``raw_source_root`` only supplies ``index_path.csv`` (the real ETH index path) for expiry
    settlement; the 1.2GB ``trades.jsonl`` is not read.
    """

    snapshots = load_normalized_cache(reconstructed_cache_root, scenario_id)
    usd_snapshots = project_usd_snapshots(snapshots)
    index_ts, index_px = _load_index_path(Path(raw_source_root) / scenario_id / "index_path.csv")
    fold_rows = measure_fold_economics(
        usd_snapshots,
        symbol=symbol,
        folds=folds,
        equity_usd=equity_usd,
        index_ts=index_ts,
        index_px=index_px,
    )
    economics = summarize_measurement(fold_rows, folds=folds, equity_usd=equity_usd)
    coverage_start = min((s.snapshot_ts_ms for s in usd_snapshots), default=0)
    coverage_end = max((s.snapshot_ts_ms for s in usd_snapshots), default=0)

    return {
        "schema_version": USD_EVAL_SCHEMA_VERSION,
        "run_status": "valid",
        "scenario_id": scenario_id,
        "symbol": symbol.upper(),
        "authorizing": False,
        "capital_go_allowed": False,
        "non_authorizing_label": EVAL_NON_AUTHORIZING_LABEL,
        "purpose": (
            "Measure the fold-level economics of the ETH OTM put-skew credit spread that the "
            "frozen ETH/USD unit bug left unmeasured. Units are USD-consistent via projection "
            f"({USD_PROJECTION_SOURCE}). The committed authorization gate is non-authorizing on "
            "reconstructed/fixture source quality by design; this is a measurement, not a GO."
        ),
        "inputs": {
            "snapshot_count": len(snapshots),
            "usd_projected_snapshot_count": len(usd_snapshots),
            "coverage_start": _iso_ms(coverage_start) if coverage_start else None,
            "coverage_end": _iso_ms(coverage_end) if coverage_end else None,
            "fold_count": len(list(folds)),
            "equity_usd": equity_usd,
        },
        "economics_summary": economics,
        "method_note": (
            "Enter-all characterization: every structure the USD-eval strategy emits in each "
            "fold's held-out test window is entered (one unit) and held to European settlement "
            "through the committed VRP engine (taker fees included; reconstructed marks carry "
            "~zero crossing). A flat effective-spread haircut (p50/p75 from the full real-data "
            "run) gives the net band. "
            "No train-causal param selection is applied because the committed selection is "
            "authorization-gated off for reconstructed source quality; this measures the raw edge."
        ),
        "disclaimer": (
            "Non-authorizing research measurement on reconstructed Deribit-history data with a "
            "documented spread haircut. Not financial advice; no capital is authorized; crypto "
            "options carry total-loss risk."
        ),
    }

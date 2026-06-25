#!/usr/bin/env python3
"""Offline Stage-1 Edge Verdict report harness.

This is a human-reviewed performance report, not a CI hard performance gate. The script
always exits 0 when it can emit a well-formed report; the verdict field is the signal.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
from collections import Counter
from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace
from typing import Any

# Allow running from a checkout without installing (src layout).
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from ajentix_quant.adapters.base import (  # noqa: E402
    FundingRate,
    HistoricalCandle,
    MarketDataset,
    SourceQuality,
)
from ajentix_quant.backtest import metrics  # noqa: E402
from ajentix_quant.backtest.engine import BacktestResult, TwoLegFundingBacktest  # noqa: E402
from ajentix_quant.backtest.slippage import SlippageModel  # noqa: E402
from ajentix_quant.backtest.verdict import (  # noqa: E402
    PERIODS_PER_YEAR_8H,
    EdgeVerdictReport,
    EdgeVerdictThresholds,
    Verdict,
    decide_verdict,
    net_apr_simple,
)
from ajentix_quant.config import settings  # noqa: E402
from ajentix_quant.data.cache import (  # noqa: E402
    DEFAULT_REQUIRED_STREAMS,
    SCHEMA_VERSION,
    CacheValidationError,
    load_dataset,
)
from ajentix_quant.risk.engine import RiskEngine, RiskParams  # noqa: E402
from ajentix_quant.risk.margin import (  # noqa: E402
    VenueMarginModel,
    bybit_btc_eth_instruments,
    bybit_btc_eth_risk_limits,
)
from ajentix_quant.strategies import FundingHarvest  # noqa: E402
from ajentix_quant.strategies.sizing import SmallCapitalSizingPolicy  # noqa: E402

REPO_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_CACHE_ROOT = Path(settings.cache_dir)
PARAM_GRID = (0.00005, 0.0001, 0.00015, 0.0002, 0.0003)
HOLD_INTERVALS = 3
SAFETY_MARGIN_BPS = 1.0
REALIZED_VOL_ANNUAL = 0.5


@dataclass(frozen=True)
class ParamSelection:
    params: dict[str, float]
    param_freeze_hash: str
    train_result: BacktestResult
    train_metrics: dict[str, Any]


def clamp_equity(equity: float) -> float:
    return min(max(float(equity), settings.capital_usd_min), settings.capital_usd_max)


def select_strategy_params(
    dataset: MarketDataset,
    *,
    equity_usd: float,
    symbol: str | None = None,
) -> ParamSelection:
    """Select strategy params from TRAIN rows only and return a freeze hash.

    The dataset may include TEST rows; this function immediately splits on
    ``dataset.train_until_ms`` and never evaluates rows with ``timestamp > train_until_ms``.
    """

    train, _test = split_dataset(dataset)
    if train is None or train_periods(dataset, symbol=symbol) == 0:
        raise ValueError("train/test split required before selecting params")
    symbol = symbol or _single_symbol(train)

    best_threshold = PARAM_GRID[0]
    best_result: BacktestResult | None = None
    best_metrics: dict[str, Any] | None = None
    best_score: tuple[float, float] | None = None
    for threshold in PARAM_GRID:
        params = {"min_funding_rate_8h": threshold}
        result = _run_backtest(train, params=params, equity_usd=equity_usd, symbol=symbol)
        run_metrics = _metrics_from_result(
            result,
            dataset=train,
            equity_usd=equity_usd,
            symbol=symbol,
        )
        score = (float(run_metrics["net_apr"]), threshold)
        if best_score is None or score > best_score:
            best_threshold = threshold
            best_result = result
            best_metrics = run_metrics
            best_score = score

    assert best_result is not None
    assert best_metrics is not None
    params = {"min_funding_rate_8h": best_threshold}
    return ParamSelection(
        params=params,
        param_freeze_hash=_param_freeze_hash(train, params),
        train_result=best_result,
        train_metrics=best_metrics,
    )


def split_dataset(dataset: MarketDataset) -> tuple[MarketDataset | None, MarketDataset | None]:
    train_until_ms = dataset.train_until_ms
    if train_until_ms is None:
        return None, None

    def funding_rows(train: bool) -> dict[str, tuple[FundingRate, ...]]:
        out: dict[str, tuple[FundingRate, ...]] = {}
        for symbol, rows in dataset.funding.items():
            if train:
                out[symbol] = tuple(row for row in rows if row.timestamp <= train_until_ms)
            else:
                out[symbol] = tuple(row for row in rows if row.timestamp > train_until_ms)
        return out

    def ohlcv_rows(train: bool) -> dict[Any, tuple[HistoricalCandle, ...]]:
        out: dict[Any, tuple[HistoricalCandle, ...]] = {}
        for key, rows in dataset.ohlcv.items():
            if train:
                out[key] = tuple(row for row in rows if row.timestamp_ms <= train_until_ms)
            else:
                out[key] = tuple(row for row in rows if row.timestamp_ms > train_until_ms)
        return out

    base = {
        "venue": dataset.venue,
        "timeframe": dataset.timeframe,
        "symbols": dataset.symbols,
        "source_quality": dict(dataset.source_quality),
        "train_until_ms": train_until_ms,
        "param_freeze_hash": dataset.param_freeze_hash,
    }
    train_dataset = MarketDataset(
        scenario_id=f"{dataset.scenario_id}:train",
        funding=funding_rows(True),
        ohlcv=ohlcv_rows(True),
        **base,
    )
    test_dataset = MarketDataset(
        scenario_id=f"{dataset.scenario_id}:test",
        funding=funding_rows(False),
        ohlcv=ohlcv_rows(False),
        **base,
    )
    return train_dataset, test_dataset


def train_periods(dataset: MarketDataset, *, symbol: str | None = None) -> int:
    train, _test = split_dataset(dataset)
    if train is None:
        return 0
    symbol = symbol or _single_symbol(train)
    return len(train.funding.get(symbol, ()))


def test_periods(dataset: MarketDataset, *, symbol: str | None = None) -> int:
    _train, test = split_dataset(dataset)
    if test is None:
        return 0
    symbol = symbol or _single_symbol(test)
    return len(test.funding.get(symbol, ()))


def build_edge_verdict_report(
    cache_root: str | Path,
    scenario_id: str,
    *,
    equity_usd: float,
    min_test_periods: int,
) -> EdgeVerdictReport:
    cache_root = Path(cache_root)
    manifest, manifest_sha = _manifest_payload(cache_root, scenario_id)
    dataset = load_dataset(cache_root, scenario_id)
    symbol = _single_symbol(dataset)
    equity_usd = clamp_equity(equity_usd)
    per_setup_notional_usd = equity_usd * float(settings.max_position_pct)
    thresholds = EdgeVerdictThresholds()

    non_venue = _non_venue_required_streams(dataset)
    all_streams_venue = not non_venue
    train, test = split_dataset(dataset)
    train_count = train_periods(dataset, symbol=symbol)
    test_count = test_periods(dataset, symbol=symbol)
    train_test_valid = train is not None and test is not None and train_count > 0 and test_count > 0

    selected_params = {"min_funding_rate_8h": float(settings.min_funding_rate_8h)}
    param_freeze_hash = dataset.param_freeze_hash or _param_freeze_hash(dataset, selected_params)
    train_result: BacktestResult | None = None
    train_metrics = _empty_metrics()
    test_result: BacktestResult | None = None
    test_metrics = _empty_metrics()
    sensitivity: list[dict[str, Any]] = []
    event_counts: dict[str, Any] = {"train": {}, "test": {}}
    liquidated = False

    if train_test_valid:
        selection = select_strategy_params(dataset, equity_usd=equity_usd, symbol=symbol)
        selected_params = selection.params
        param_freeze_hash = selection.param_freeze_hash
        train_result = selection.train_result
        train_metrics = selection.train_metrics
        assert test is not None
        test_result = _run_backtest(
            test, params=selected_params, equity_usd=equity_usd, symbol=symbol
        )
        test_metrics = _metrics_from_result(
            test_result,
            dataset=test,
            equity_usd=equity_usd,
            symbol=symbol,
        )
        sensitivity = _sensitivity(
            baseline=test_metrics,
            test_dataset=test,
            params=selected_params,
            equity_usd=equity_usd,
            symbol=symbol,
        )
        event_counts = {
            "train": _event_counts(train_result),
            "test": _event_counts(test_result),
        }
        liquidated = train_result.liquidated or test_result.liquidated

    collapse = _collapse(train_metrics, test_metrics, thresholds)
    verdict, reasons = decide_verdict(
        sharpe=float(test_metrics["sharpe"]),
        mdd=float(test_metrics["mdd"]),
        net_apr=float(test_metrics["net_apr"]),
        all_streams_venue=all_streams_venue,
        train_test_valid=train_test_valid,
        test_periods=test_count,
        min_test_periods=min_test_periods,
        collapse=collapse,
        thresholds=thresholds,
    )
    if non_venue:
        specific = "real venue data required: " + ", ".join(non_venue)
        reasons = [
            specific if reason.startswith("real venue data required:") else reason
            for reason in reasons
        ]

    caveats = _base_caveats()
    if non_venue:
        caveats.append("GO is impossible without source_quality=VENUE for every required stream.")
    if verdict is Verdict.INCONCLUSIVE and not reasons:
        reasons = ["edge verdict inconclusive"]

    return EdgeVerdictReport(
        scenario_id=scenario_id,
        schema_version=str(manifest.get("schema_version", SCHEMA_VERSION)),
        manifest_sha256=manifest_sha,
        generator_version=str(manifest.get("generator_version", "unknown")),
        verdict=verdict,
        reasons=reasons,
        equity_usd=equity_usd,
        per_setup_notional_usd=per_setup_notional_usd,
        train_until_ms=dataset.train_until_ms,
        param_freeze_hash=param_freeze_hash,
        source_quality=_source_quality_dict(dataset),
        selected_params=selected_params,
        train_metrics=train_metrics,
        test_metrics=test_metrics,
        event_counts=event_counts,
        liquidated=liquidated,
        sensitivity=sensitivity,
        caveats=caveats,
    )


def absent_cache_report(
    cache_root: str | Path,
    scenario_id: str,
    *,
    equity_usd: float,
    reason_detail: str | None = None,
) -> EdgeVerdictReport:
    scenario_path = Path(cache_root) / scenario_id
    reason = (
        f"real Bybit venue cache absent at {scenario_path}; "
        "run scripts/populate_bybit_cache.py"
    )
    if reason_detail:
        reason = f"{reason} ({reason_detail})"
    selected_params = {"min_funding_rate_8h": float(settings.min_funding_rate_8h)}
    equity_usd = clamp_equity(equity_usd)
    return EdgeVerdictReport(
        scenario_id=scenario_id,
        schema_version=SCHEMA_VERSION,
        manifest_sha256="",
        generator_version="unknown",
        verdict=Verdict.INCONCLUSIVE,
        reasons=[reason],
        equity_usd=equity_usd,
        per_setup_notional_usd=equity_usd * float(settings.max_position_pct),
        train_until_ms=None,
        param_freeze_hash=None,
        source_quality={},
        selected_params=selected_params,
        train_metrics=_empty_metrics(),
        test_metrics=_empty_metrics(),
        event_counts={"train": {}, "test": {}},
        liquidated=False,
        sensitivity=[],
        caveats=[*_base_caveats(), "GO is impossible without a load-valid VENUE cache."],
    )


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--cache-root", default=str(DEFAULT_CACHE_ROOT))
    parser.add_argument("--scenario-id", default=settings.edge_verdict_scenario_id)
    parser.add_argument("--equity", type=float, default=settings.default_capital_usd)
    parser.add_argument("--out", default=None, help="report output prefix/path; writes JSON and MD")
    parser.add_argument("--min-test-periods", type=int, default=30)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    equity_usd = clamp_equity(args.equity)
    try:
        report = build_edge_verdict_report(
            args.cache_root,
            args.scenario_id,
            equity_usd=equity_usd,
            min_test_periods=args.min_test_periods,
        )
    except (CacheValidationError, FileNotFoundError, json.JSONDecodeError, OSError) as exc:
        report = absent_cache_report(
            args.cache_root,
            args.scenario_id,
            equity_usd=equity_usd,
            reason_detail=str(exc),
        )
    except Exception as exc:  # pragma: no cover - fail-closed report path for operator use.
        report = absent_cache_report(
            args.cache_root,
            args.scenario_id,
            equity_usd=equity_usd,
            reason_detail=f"edge verdict could not be computed: {exc}",
        )
    _emit(report, None if args.out is None else Path(args.out))
    return 0


def _emit(report: EdgeVerdictReport, out: Path | None) -> None:
    markdown = report.to_markdown()
    print(markdown, end="")
    if out is None:
        return
    json_path, md_path = _output_paths(out)
    json_path.parent.mkdir(parents=True, exist_ok=True)
    md_path.parent.mkdir(parents=True, exist_ok=True)
    json_path.write_text(
        json.dumps(report.to_json(), indent=2, sort_keys=True, allow_nan=False) + "\n",
        encoding="utf-8",
    )
    md_path.write_text(markdown, encoding="utf-8")


def _output_paths(out: Path) -> tuple[Path, Path]:
    if out.exists() and out.is_dir():
        return out / "edge_verdict.json", out / "edge_verdict.md"
    if out.suffix == ".json":
        return out, out.with_suffix(".md")
    if out.suffix == ".md":
        return out.with_suffix(".json"), out
    if out.suffix:
        return out.with_suffix(".json"), out.with_suffix(".md")
    return Path(str(out) + ".json"), Path(str(out) + ".md")


def _manifest_payload(cache_root: Path, scenario_id: str) -> tuple[dict[str, Any], str]:
    manifest_text = (cache_root / scenario_id / "manifest.json").read_text(encoding="utf-8")
    manifest = json.loads(manifest_text)
    return manifest, hashlib.sha256(manifest_text.encode("utf-8")).hexdigest()


def _single_symbol(dataset: MarketDataset) -> str:
    if len(dataset.symbols) != 1:
        raise ValueError("edge verdict currently requires a single-symbol scenario")
    return dataset.symbols[0]


def _run_backtest(
    dataset: MarketDataset,
    *,
    params: dict[str, float],
    equity_usd: float,
    symbol: str,
) -> BacktestResult:
    margin_model = _margin_model(symbol)
    runner = TwoLegFundingBacktest(
        strategy=FundingHarvest(
            min_funding_rate_8h=float(params["min_funding_rate_8h"]),
            funding_compression_8h=float(settings.funding_compression_8h),
            funding_reversal_imminent=float(settings.funding_reversal_imminent_8h),
            max_net_delta_frac=float(settings.max_net_delta_frac),
            basis_dislocation_bps=50.0,
        ),
        risk=_risk_engine(),
        margin_model=margin_model,
        slippage=SlippageModel(
            base_bps=float(settings.slippage_base_bps),
            impact_bps_per_pct_volume=float(settings.slippage_impact_bps_per_pct_volume),
            cap_bps=float(settings.slippage_cap_bps),
        ),
        sizing=SmallCapitalSizingPolicy(
            max_position_pct=float(settings.max_position_pct),
            reserve_pct=float(settings.reserve_pct),
            min_notional_usd=margin_model.instrument.min_notional,
        ),
        initial_equity_usd=equity_usd,
        settings=_settings_namespace(equity_usd),
    )
    return runner.run_market_dataset(
        dataset,
        symbol=symbol,
        initial_equity_usd=equity_usd,
        realized_vol_annual=REALIZED_VOL_ANNUAL,
        hold_intervals=HOLD_INTERVALS,
        safety_margin_bps=SAFETY_MARGIN_BPS,
        scenario_gap_pct=float(settings.gap_stress_pct),
    )


def _risk_engine() -> RiskEngine:
    return RiskEngine(
        RiskParams(
            base_leverage=float(settings.base_leverage),
            max_leverage=float(settings.max_leverage),
            min_liq_distance_pct=float(settings.min_liq_distance_pct),
            reserve_pct=float(settings.reserve_pct),
            max_drawdown_pct=float(settings.max_drawdown_pct),
            funding_reversal_exit_hours=int(settings.funding_reversal_exit_hours),
            max_position_pct=float(settings.max_position_pct),
            health_factor_floor=float(settings.health_factor_floor),
            vol_spike_annual=float(settings.vol_spike_annual),
            funding_compression_8h=float(settings.funding_compression_8h),
            funding_reversal_imminent_8h=float(settings.funding_reversal_imminent_8h),
            max_net_delta_frac=float(settings.max_net_delta_frac),
            gap_stress_pct=float(settings.gap_stress_pct),
            adl_rank_threshold=int(settings.adl_rank_threshold),
        )
    )


def _margin_model(symbol: str) -> VenueMarginModel:
    instruments = bybit_btc_eth_instruments()
    limits = bybit_btc_eth_risk_limits()
    if symbol not in instruments or symbol not in limits:
        raise ValueError(f"unsupported symbol for Bybit margin model: {symbol}")
    return VenueMarginModel(instruments[symbol], limits[symbol])


def _settings_namespace(equity_usd: float) -> SimpleNamespace:
    return SimpleNamespace(
        perp_taker_fee_bps=float(settings.perp_taker_fee_bps),
        spot_taker_fee_bps=float(settings.spot_taker_fee_bps),
        leverage_cost_apr=float(settings.leverage_cost_apr),
        reserve_pct=float(settings.reserve_pct),
        max_position_pct=float(settings.max_position_pct),
        max_net_delta_frac=float(settings.max_net_delta_frac),
        slippage_base_bps=float(settings.slippage_base_bps),
        slippage_impact_bps_per_pct_volume=float(settings.slippage_impact_bps_per_pct_volume),
        slippage_cap_bps=float(settings.slippage_cap_bps),
        default_capital_usd=float(equity_usd),
    )


def _metrics_from_result(
    result: BacktestResult,
    *,
    dataset: MarketDataset,
    equity_usd: float,
    symbol: str,
) -> dict[str, Any]:
    net_apr = net_apr_simple(
        final_equity=result.final_equity,
        initial_equity=equity_usd,
        n_test_periods=result.n_periods,
        periods_per_year=PERIODS_PER_YEAR_8H,
    )
    available = _available_positive_funding(dataset, symbol=symbol, equity_usd=equity_usd)
    captured = result.funding_received - result.funding_paid
    return {
        "sharpe": result.sharpe,
        "sortino": result.sortino,
        "mdd": result.max_drawdown,
        "net_apr": net_apr,
        "calmar": metrics.calmar(net_apr, result.max_drawdown),
        "funding_capture": metrics.funding_capture(captured, available),
        "max_abs_net_delta": result.max_abs_net_delta,
        "n_periods": result.n_periods,
        "final_equity": result.final_equity,
        "funding_received": result.funding_received,
        "funding_paid": result.funding_paid,
        "total_fees": result.total_fees,
        "total_slippage": result.total_slippage,
        "leverage_cost": result.leverage_cost,
    }


def _empty_metrics() -> dict[str, Any]:
    return {
        "sharpe": 0.0,
        "sortino": 0.0,
        "mdd": 0.0,
        "net_apr": 0.0,
        "calmar": 0.0,
        "funding_capture": 0.0,
        "max_abs_net_delta": 0.0,
        "n_periods": 0,
        "final_equity": 0.0,
        "funding_received": 0.0,
        "funding_paid": 0.0,
        "total_fees": 0.0,
        "total_slippage": 0.0,
        "leverage_cost": 0.0,
    }


def _available_positive_funding(dataset: MarketDataset, *, symbol: str, equity_usd: float) -> float:
    per_setup_notional = equity_usd * float(settings.max_position_pct)
    return sum(max(0.0, row.rate) * per_setup_notional for row in dataset.funding.get(symbol, ()))


def _sensitivity(
    *,
    baseline: dict[str, Any],
    test_dataset: MarketDataset,
    params: dict[str, float],
    equity_usd: float,
    symbol: str,
) -> list[dict[str, Any]]:
    base_threshold = float(params["min_funding_rate_8h"])
    rows: list[dict[str, Any]] = []
    for label, multiplier in (
        ("min_funding_rate_8h_-25pct", 0.75),
        ("min_funding_rate_8h_+25pct", 1.25),
    ):
        perturbed = {**params, "min_funding_rate_8h": max(0.0, base_threshold * multiplier)}
        result = _run_backtest(
            test_dataset, params=perturbed, equity_usd=equity_usd, symbol=symbol
        )
        perturbed_metrics = _metrics_from_result(
            result,
            dataset=test_dataset,
            equity_usd=equity_usd,
            symbol=symbol,
        )
        rows.append(
            {
                "case": label,
                "min_funding_rate_8h": perturbed["min_funding_rate_8h"],
                "sharpe_delta": float(perturbed_metrics["sharpe"]) - float(baseline["sharpe"]),
                "mdd_delta": float(perturbed_metrics["mdd"]) - float(baseline["mdd"]),
                "net_apr_delta": float(perturbed_metrics["net_apr"]) - float(baseline["net_apr"]),
            }
        )
    return rows


def _event_counts(result: BacktestResult) -> dict[str, int]:
    counts = Counter(event.kind.value for event in result.events)
    return {name: counts[name] for name in sorted(counts)}


def _param_freeze_hash(dataset: MarketDataset, params: dict[str, float]) -> str:
    timestamps = sorted(
        row.timestamp for series in dataset.funding.values() for row in series
    )
    payload = {
        "schema": 1,
        "selection": "train-only-fixed-grid-v1",
        "scenario_id": dataset.scenario_id.split(":", 1)[0],
        "train_until_ms": dataset.train_until_ms,
        "train_first_timestamp_ms": timestamps[0] if timestamps else None,
        "train_last_timestamp_ms": timestamps[-1] if timestamps else None,
        "train_periods": len(timestamps),
        "params": {key: params[key] for key in sorted(params)},
    }
    raw = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _non_venue_required_streams(dataset: MarketDataset) -> list[str]:
    out: list[str] = []
    for name in DEFAULT_REQUIRED_STREAMS:
        quality = dataset.source_quality.get(name)
        if quality is not SourceQuality.VENUE:
            out.append(f"{name.value}={(quality or SourceQuality.ABSENT).value}")
    return out


def _source_quality_dict(dataset: MarketDataset) -> dict[str, str]:
    return {name.value: quality.value for name, quality in dataset.source_quality.items()}


def _collapse(
    train_metrics: dict[str, Any],
    test_metrics: dict[str, Any],
    thresholds: EdgeVerdictThresholds,
) -> bool:
    train_sharpe = float(train_metrics.get("sharpe", 0.0))
    test_sharpe = float(test_metrics.get("sharpe", 0.0))
    train_mdd = float(train_metrics.get("mdd", 0.0))
    test_mdd = float(test_metrics.get("mdd", 0.0))
    sharpe_collapse = test_sharpe < 0.0 < train_sharpe
    drawdown_collapse = train_mdd <= thresholds.max_mdd < test_mdd
    return sharpe_collapse or drawdown_collapse


def _base_caveats() -> list[str]:
    return [
        "This is a recorded human-reviewed Edge Verdict report, not a CI hard performance gate.",
        "Net APR formula: (final_equity/initial_equity - 1) * (1095 / n_test_periods); "
        "simple non-compounded net return over the TEST window, after fees, funding, "
        "slippage, and leverage cost on the delta-neutral equity ledger at the "
        "configured small-cap equity.",
        "Strategy params are selected on TRAIN rows only (timestamp <= train_until_ms); "
        "the param_freeze_hash covers the frozen params and TRAIN window, then those params are "
        "reused on held-out TEST rows (timestamp > train_until_ms).",
        "Collapse rule: NO-GO when test_sharpe < 0 < train_sharpe, or TEST MDD breaches "
        "the threshold while TRAIN MDD did not.",
        "Committed scenario_ids are immutable; changing fixture data requires a new scenario_id "
        "(for example _v2) plus an ADR note.",
    ]


if __name__ == "__main__":
    raise SystemExit(main())

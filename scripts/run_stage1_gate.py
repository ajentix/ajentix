#!/usr/bin/env python3
"""Offline Stage-1 structural gate for the deterministic funding-harvest fixture."""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import sys
from collections import Counter
from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace

# Allow running from a checkout without installing (src layout).
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from ajentix_quant.adapters.base import (  # noqa: E402
    MarketType,
    PriceType,
    SourceQuality,
    StreamKey,
)
from ajentix_quant.backtest import metrics  # noqa: E402
from ajentix_quant.backtest.account import RATIO_QUANT, canonical_str  # noqa: E402
from ajentix_quant.backtest.engine import BacktestResult, TwoLegFundingBacktest  # noqa: E402
from ajentix_quant.backtest.slippage import SlippageModel  # noqa: E402
from ajentix_quant.data.cache import load_dataset  # noqa: E402
from ajentix_quant.risk.engine import RiskEngine, RiskParams  # noqa: E402
from ajentix_quant.risk.margin import (  # noqa: E402
    VenueMarginModel,
    bybit_btc_eth_instruments,
    bybit_btc_eth_risk_limits,
)
from ajentix_quant.strategies import FundingHarvest  # noqa: E402
from ajentix_quant.strategies.sizing import SmallCapitalSizingPolicy  # noqa: E402

REPO_ROOT = Path(__file__).resolve().parents[1]
FIXTURE_ROOT = REPO_ROOT / "tests" / "fixtures" / "stage1"
SCENARIO_ID = "structural_v1"
SYMBOL = "BTC/USDT:USDT"
INITIAL_EQUITY_USD = 1_000.0
RESERVE_PCT = 0.25
MAX_POSITION_PCT = 0.25
MAX_NET_DELTA_FRAC = 0.02
GAP_STRESS_PCT = 0.20
GAP_DETECTION_FLOOR = 0.15
HOLD_INTERVALS = 3
SAFETY_MARGIN_BPS = 1.0
REALIZED_VOL_ANNUAL = 0.5
EXPECTED_GOLDEN = (
    "events_total=43;events_by_kind=deleverage:1,entry:2,exit:1,funding:39;"
    "final_equity=1013.46188825"
)


@dataclass(frozen=True)
class GateRun:
    exit_code: int
    canonical_report: str
    canonical_golden: str
    failures: tuple[str, ...]
    result: BacktestResult | None


def _settings() -> SimpleNamespace:
    return SimpleNamespace(
        perp_taker_fee_bps=5.5,
        spot_taker_fee_bps=10.0,
        leverage_cost_apr=0.0,
        reserve_pct=RESERVE_PCT,
        max_position_pct=MAX_POSITION_PCT,
        max_net_delta_frac=MAX_NET_DELTA_FRAC,
        slippage_base_bps=1.0,
        slippage_impact_bps_per_pct_volume=5.0,
        slippage_cap_bps=50.0,
        default_capital_usd=INITIAL_EQUITY_USD,
    )


def _risk() -> RiskEngine:
    return RiskEngine(
        RiskParams(
            reserve_pct=RESERVE_PCT,
            max_position_pct=MAX_POSITION_PCT,
            max_net_delta_frac=MAX_NET_DELTA_FRAC,
            gap_stress_pct=GAP_STRESS_PCT,
            health_factor_floor=1.5,
            funding_reversal_exit_hours=24,
        )
    )


def _margin_model() -> VenueMarginModel:
    return VenueMarginModel(
        bybit_btc_eth_instruments()[SYMBOL],
        bybit_btc_eth_risk_limits()[SYMBOL],
    )


def _runner(risk: RiskEngine, margin_model: VenueMarginModel) -> TwoLegFundingBacktest:
    settings = _settings()
    return TwoLegFundingBacktest(
        strategy=FundingHarvest(
            min_funding_rate_8h=0.0001,
            funding_compression_8h=0.00005,
            funding_reversal_imminent=0.0,
            max_net_delta_frac=MAX_NET_DELTA_FRAC,
            basis_dislocation_bps=50.0,
        ),
        risk=risk,
        margin_model=margin_model,
        slippage=SlippageModel(
            settings.slippage_base_bps,
            settings.slippage_impact_bps_per_pct_volume,
            settings.slippage_cap_bps,
        ),
        sizing=SmallCapitalSizingPolicy(
            max_position_pct=MAX_POSITION_PCT,
            reserve_pct=RESERVE_PCT,
            min_notional_usd=margin_model.instrument.min_notional,
        ),
        settings=settings,
    )


def _manifest_payload(cache_root: Path, scenario_id: str) -> tuple[dict, str]:
    manifest_text = (cache_root / scenario_id / "manifest.json").read_text(encoding="utf-8")
    manifest = json.loads(manifest_text)
    return manifest, hashlib.sha256(manifest_text.encode("utf-8")).hexdigest()


def _negative_stats(rates: list[float]) -> tuple[int, int]:
    count = 0
    current = 0
    longest = 0
    for rate in rates:
        if rate < 0.0:
            count += 1
            current += 1
            longest = max(longest, current)
        else:
            current = 0
    return count, longest


def _gap_events(dataset) -> tuple[list[float], float]:
    marks = tuple(
        sorted(
            dataset.ohlcv[StreamKey(SYMBOL, MarketType.LINEAR_PERP, PriceType.MARK)],
            key=lambda candle: candle.timestamp_ms,
        )
    )
    gaps: list[float] = []
    for previous, current in zip(marks, marks[1:], strict=False):
        if previous.close <= 0.0:
            continue
        gap = current.high / previous.close - 1.0
        if gap >= GAP_DETECTION_FLOOR:
            gaps.append(gap)
    return gaps, max(gaps) if gaps else 0.0


def _run_once(dataset, *, scenario_gap_pct: float) -> BacktestResult:
    risk = _risk()
    margin_model = _margin_model()
    return _runner(risk, margin_model).run_market_dataset(
        dataset,
        symbol=SYMBOL,
        initial_equity_usd=INITIAL_EQUITY_USD,
        realized_vol_annual=REALIZED_VOL_ANNUAL,
        hold_intervals=HOLD_INTERVALS,
        safety_margin_bps=SAFETY_MARGIN_BPS,
        scenario_gap_pct=scenario_gap_pct,
    )


def _event_counts(result: BacktestResult) -> str:
    counts = Counter(event.kind.value for event in result.events)
    return ",".join(f"{name}:{counts[name]}" for name in sorted(counts))


def golden_string(result: BacktestResult) -> str:
    return (
        f"events_total={len(result.events)};events_by_kind={_event_counts(result)};"
        f"final_equity={canonical_str(result.final_equity)}"
    )


def _canonical_result(result: BacktestResult) -> str:
    return ";".join(
        (
            golden_string(result),
            f"n_entries={result.n_entries}",
            f"n_exits={result.n_exits}",
            f"n_forced_exits={result.n_forced_exits}",
            f"n_deleverages={result.n_deleverages}",
            f"n_liquidations={result.n_liquidations}",
            f"liquidated={result.liquidated}",
            f"max_abs_net_delta={canonical_str(result.max_abs_net_delta, RATIO_QUANT)}",
            f"max_leverage_used={result.max_leverage_used:.12f}",
            f"min_equity={canonical_str(min(result.equity_curve))}",
        )
    )


def _check(name: str, ok: bool, detail: str) -> tuple[str, str | None]:
    status = "PASS" if ok else "FAIL"
    return f"[{status}] {name}: {detail}", None if ok else f"{name}: {detail}"


def run_gate(
    cache_root: str | Path = FIXTURE_ROOT,
    scenario_id: str = SCENARIO_ID,
    *,
    enforce_golden: bool = True,
) -> GateRun:
    cache_root = Path(cache_root)
    manifest, manifest_sha = _manifest_payload(cache_root, scenario_id)
    dataset = load_dataset(cache_root, scenario_id)
    rates = [row.rate for row in dataset.funding[SYMBOL]]
    negative_count, longest_negative = _negative_stats(rates)
    gaps, max_gap = _gap_events(dataset)
    scenario_gap_pct = max_gap

    first = _run_once(dataset, scenario_gap_pct=scenario_gap_pct)
    second = _run_once(dataset, scenario_gap_pct=scenario_gap_pct)
    first_canonical = _canonical_result(first)
    second_canonical = _canonical_result(second)

    risk = _risk()
    margin_model = _margin_model()
    gap_cap = risk.gap_survival_leverage_cap(margin_model, equity=INITIAL_EQUITY_USD)
    reserve_floor = INITIAL_EQUITY_USD * RESERVE_PCT
    min_equity = min(first.equity_curve)
    all_fixture_quality = all(
        quality is SourceQuality.FIXTURE for quality in dataset.source_quality.values()
    )
    canonical_golden = golden_string(first)

    rows: list[str] = []
    failures: list[str] = []
    checks = [
        _check(
            "negative funding present",
            negative_count >= 1,
            f"negative_funding_count={negative_count}",
        ),
        _check(
            "sustained negative funding window",
            longest_negative >= 2,
            f"longest_consecutive_negative={longest_negative}",
        ),
        _check(
            ">=15% perp-mark gap present",
            len(gaps) >= 1,
            f"gap_events={len(gaps)}, max_gap={max_gap:.12f}",
        ),
        _check(
            "deterministic offline replay",
            first_canonical == second_canonical,
            "canonical_run_1 == canonical_run_2",
        ),
        _check(
            "fixture-only source quality",
            all_fixture_quality,
            "source_quality="
            + "{"
            + ", ".join(f"{k.value}: {v.value}" for k, v in dataset.source_quality.items())
            + "}",
        ),
        _check(
            "no liquidation under gap-survival cap",
            first.liquidated is False and first.n_liquidations == 0,
            f"liquidated={first.liquidated}, n_liquidations={first.n_liquidations}",
        ),
        _check(
            "leverage within cap",
            first.max_leverage_used <= gap_cap + 1e-12 and first.max_leverage_used <= 5.0,
            f"max_leverage_used={first.max_leverage_used:.12f}, gap_cap={gap_cap:.12f}",
        ),
        _check(
            "reserve floor respected",
            min_equity >= reserve_floor,
            f"min_equity={canonical_str(min_equity)}, reserve_floor={canonical_str(reserve_floor)}",
        ),
        _check(
            "net delta within configured band",
            first.max_abs_net_delta <= MAX_NET_DELTA_FRAC + 1e-12,
            (
                f"max_abs_net_delta={canonical_str(first.max_abs_net_delta, RATIO_QUANT)}, "
                f"band={canonical_str(MAX_NET_DELTA_FRAC, RATIO_QUANT)}"
            ),
        ),
    ]
    if enforce_golden:
        checks.append(
            _check(
                "event-count + final-equity golden master",
                canonical_golden == EXPECTED_GOLDEN,
                f"observed={canonical_golden}",
            )
        )
    for row, failure in checks:
        rows.append(row)
        if failure is not None:
            failures.append(failure)

    calmar_value = metrics.calmar(first.ann_return, first.max_drawdown)
    calmar_text = "inf" if math.isinf(calmar_value) else f"{calmar_value:.12f}"
    metrics_lines = [
        "Computed metrics (printed only; no performance thresholds are asserted):",
        f"  sharpe={first.sharpe:.12f}",
        f"  sortino={first.sortino:.12f}",
        f"  net_apr={first.ann_return:.12f}",
        f"  max_drawdown={first.max_drawdown:.12f}",
        f"  calmar={calmar_text}",
    ]
    breakdown_lines = [
        "Cost/event breakdown:",
        f"  total_fees={canonical_str(first.total_fees)}",
        f"  total_slippage={canonical_str(first.total_slippage)}",
        f"  funding_received={canonical_str(first.funding_received)}",
        f"  funding_paid={canonical_str(first.funding_paid)}",
        f"  leverage_cost={canonical_str(first.leverage_cost)}",
        f"  events_total={len(first.events)}",
        f"  events_by_kind={_event_counts(first)}",
    ]
    header = [
        "Stage-1 structural gate",
        f"scenario_id={dataset.scenario_id}",
        f"schema_version={manifest['schema_version']}",
        f"manifest_sha256={manifest_sha}",
        f"param_freeze_hash={manifest.get('param_freeze_hash')}",
        f"canonical_golden={canonical_golden}",
    ]
    status = "PASS" if not failures else "FAIL"
    report = "\n".join(
        [
            *header,
            "",
            "Structural invariants:",
            *rows,
            "",
            *metrics_lines,
            "",
            *breakdown_lines,
            "",
            status,
        ]
    )
    return GateRun(
        exit_code=0 if not failures else 1,
        canonical_report=report,
        canonical_golden=canonical_golden,
        failures=tuple(failures),
        result=first,
    )


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--cache-root", default=str(FIXTURE_ROOT))
    parser.add_argument("--scenario-id", default=SCENARIO_ID)
    parser.add_argument(
        "--no-golden",
        action="store_true",
        help="skip the committed golden-master check for alternate/tampered scenarios",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        gate = run_gate(
            args.cache_root,
            args.scenario_id,
            enforce_golden=not args.no_golden,
        )
    except Exception as exc:  # pragma: no cover - exercised by CLI failure mode.
        print(f"Stage-1 structural gate\n\n[FAIL] gate raised: {exc}")
        return 1
    print(gate.canonical_report)
    return gate.exit_code


if __name__ == "__main__":
    raise SystemExit(main())

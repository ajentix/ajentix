#!/usr/bin/env python3
"""Collect read-only strategy-v2 G003 A2 pivot venue-feasibility evidence.

Manual network tool: do not run in CI. The feasibility result is recorded in the
report payload and stdout; successful collection exits 0 even when no candidate
clears the bar.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import sys
from bisect import bisect_right
from collections.abc import Mapping
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib.error import URLError
from urllib.request import Request, urlopen

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from ajentix_quant.data.cache import load_dataset  # noqa: E402
from ajentix_quant.research.preregistration import (  # noqa: E402
    PLAN_A2_BAR,
    load_preregistration,
    verify_preregistration,
)
from ajentix_quant.research.venue_evidence import (  # noqa: E402
    AdlLiquidationMetadataEvidence,
    CandidateEvidence,
    DepthSlippageEstimate,
    FeeScheduleEvidence,
    FundingObservation,
    a2_cost_threshold_usd,
    compute_rolling_24h_opportunity_stats,
    evaluate_a2_candidate,
)

HL_FEE_URL = "https://hyperliquid.gitbook.io/hyperliquid-docs/trading/fees"
HL_FUNDING_URL = "https://hyperliquid.gitbook.io/hyperliquid-docs/trading/funding"
HL_API_PERPS_URL = "https://hyperliquid.gitbook.io/hyperliquid-docs/for-developers/api/info-endpoint/perpetuals"
HL_LIQUIDATION_URL = "https://hyperliquid.gitbook.io/hyperliquid-docs/trading/liquidations"
HL_ADL_URL = "https://hyperliquid.gitbook.io/hyperliquid-docs/trading/auto-deleveraging"
BYBIT_FEE_URL = "https://www.bybit.com/en/help-center/article/Perpetual-Futures-Contract-Fees-Explained"

HL_TAKER_FEE_BPS = 4.5
HL_MAKER_FEE_BPS = 1.5
BYBIT_TAKER_FEE_BPS = 5.5
BYBIT_MAKER_FEE_BPS = 2.0

HL_DIRECT_BASE_SYMBOLS = ("BTC/USDC:USDC", "ETH/USDC:USDC")
BYBIT_SPREAD_SYMBOLS = {
    "BTC": ("BTC/USDC:USDC", "BTC/USDT:USDT", "bybit_real_btc_v1"),
    "ETH": ("ETH/USDC:USDC", "ETH/USDT:USDT", "bybit_real_eth_v1"),
}

HOUR_MS = 60 * 60 * 1000
DAY_MS = 24 * HOUR_MS


def main(argv: list[str] | None = None) -> int:
    if os.environ.get("CI"):
        raise SystemExit(
            "collect_pivot_venue_evidence is a manual network tool and must not run in CI"
        )

    args = _parser().parse_args(argv)
    repo_root = Path(args.repo_root).resolve()
    prereg_path = _resolve_preregistration(repo_root, args.preregistration)
    artifact = load_preregistration(prereg_path)
    prereg_sha = _sha256_file(prereg_path)
    verify = verify_preregistration(artifact, repo_root)
    if not verify.valid:
        print(f"run_status={verify.run_status}")
        print(f"run_id={artifact.get('run_id')}")
        print(f"preregistration={prereg_path}")
        print(f"preregistration_sha256={prereg_sha}")
        print("decision=REFUSED_INVALID_PREREGISTRATION")
        for mismatch in verify.mismatches:
            print(f"mismatch={mismatch}", file=sys.stderr)
        return 1

    try:
        import ccxt  # noqa: PLC0415
    except ImportError as exc:  # pragma: no cover - exercised only in a missing-live-extra env
        raise SystemExit("ccxt is required for this manual network collector") from exc

    generated_at = _utc_now_iso()
    reports_dir = repo_root / args.reports_dir
    reports_dir.mkdir(parents=True, exist_ok=True)

    hl = ccxt.hyperliquid({"enableRateLimit": True})
    bybit = ccxt.bybit({"enableRateLimit": True, "options": {"defaultType": "swap"}})
    hl_markets = hl.load_markets()
    bybit.load_markets()
    hl_meta = _fetch_hl_meta()

    alt_symbols, alt_selection = _select_hl_alt_symbols(
        hl,
        hl_markets,
        base_symbols=HL_DIRECT_BASE_SYMBOLS,
        limit=max(0, int(args.alt_count)),
    )
    direct_symbols = tuple(dict.fromkeys((*HL_DIRECT_BASE_SYMBOLS, *alt_symbols)))

    now_ms = _now_ms()
    direct_since_ms = now_ms - int(float(args.lookback_days) * DAY_MS)
    required_sizes = tuple(float(size) for size in PLAN_A2_BAR["depth_per_leg_usd"])
    primary_order_size_usd = max(required_sizes)

    candidates: list[dict[str, Any]] = []
    for symbol in direct_symbols:
        report = _collect_hl_direct_candidate(
            hl=hl,
            hl_meta=hl_meta,
            symbol=symbol,
            since_ms=direct_since_ms,
            until_ms=now_ms,
            primary_order_size_usd=primary_order_size_usd,
            required_sizes=required_sizes,
            selection_reason=(
                "required minimum candidate" if symbol in HL_DIRECT_BASE_SYMBOLS else alt_selection
            ),
        )
        candidates.append(report)

    for base, (hl_symbol, bybit_symbol, scenario_id) in BYBIT_SPREAD_SYMBOLS.items():
        report = _collect_hl_bybit_spread_candidate(
            hl=hl,
            bybit=bybit,
            hl_meta=hl_meta,
            repo_root=repo_root,
            cache_root=str(artifact.get("cache_root", "data/cache/bybit")),
            base=base,
            hl_symbol=hl_symbol,
            bybit_symbol=bybit_symbol,
            scenario_id=scenario_id,
            lookback_days=float(args.lookback_days),
            primary_order_size_usd=primary_order_size_usd,
            required_sizes=required_sizes,
        )
        candidates.append(report)

    clearing = [candidate for candidate in candidates if candidate["clears"]]
    overall = {
        "any_candidate_clears": bool(clearing),
        "clearing_candidate_ids": [candidate["candidate_id"] for candidate in clearing],
        "conclusion": (
            "pivot candidate cleared -> Phase 3 ralplan required"
            if clearing
            else "no evidence-supported pivot candidate yet"
        ),
    }
    payload = {
        "schema_version": "pivot-venue-feasibility-v2",
        "run_status": verify.run_status,
        "run_id": artifact.get("run_id"),
        "content_hash": artifact.get("content_hash"),
        "generated_at": generated_at,
        "preregistration_path": prereg_path.relative_to(repo_root).as_posix(),
        "preregistration_sha256": prereg_sha,
        "a2_bar": PLAN_A2_BAR,
        "assumptions": _assumptions(primary_order_size_usd),
        "alt_selection": {
            "selected_symbols": list(alt_symbols),
            "rule": alt_selection,
        },
        "overall": overall,
        "candidates": candidates,
    }
    json_path = reports_dir / "pivot_venue_feasibility_v2.json"
    md_path = reports_dir / "pivot_venue_feasibility_v2.md"
    json_path.write_text(
        json.dumps(_json_safe(payload), indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    md_path.write_text(_markdown_report(payload), encoding="utf-8")

    print(f"run_status={verify.run_status}")
    print(f"run_id={artifact.get('run_id')}")
    print(f"preregistration_sha256={prereg_sha}")
    print(_summary_table(candidates))
    print(f"overall={overall['conclusion']}")
    print(f"wrote={json_path.relative_to(repo_root)}")
    print(f"wrote={md_path.relative_to(repo_root)}")
    return 0


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Collect read-only A2 pivot venue-feasibility evidence. Manual network tool."
    )
    parser.add_argument(
        "--repo-root",
        default=str(Path(__file__).resolve().parents[1]),
        help="Repository root (defaults to checkout containing this script).",
    )
    parser.add_argument(
        "--preregistration",
        default=None,
        help="Path to docs/preregistration/stratv2-*.json. Defaults to the single artifact.",
    )
    parser.add_argument("--reports-dir", default="reports")
    parser.add_argument(
        "--lookback-days",
        type=float,
        default=92.0,
        help="Funding lookback for live fetches; default adds a buffer above the 90d A2 bar.",
    )
    parser.add_argument(
        "--alt-count",
        type=int,
        default=3,
        help="Optional higher-recent-absolute-funding HL swaps to add by deterministic rule.",
    )
    return parser


def _collect_hl_direct_candidate(
    *,
    hl: Any,
    hl_meta: Any,
    symbol: str,
    since_ms: int,
    until_ms: int,
    primary_order_size_usd: float,
    required_sizes: tuple[float, ...],
    selection_reason: str,
) -> dict[str, Any]:
    fetched_at = _utc_now_iso()
    errors: list[str] = []
    funding_rows: tuple[FundingObservation, ...]
    try:
        funding_rows = _fetch_hl_funding_history(hl, symbol, since_ms=since_ms, until_ms=until_ms)
    except Exception as exc:  # noqa: BLE001 - report missing data rather than fabricating
        errors.append(f"funding_fetch_error={type(exc).__name__}: {exc}")
        funding_rows = ()

    try:
        depth = _depth_estimates_from_exchange(hl, symbol, required_sizes=required_sizes)
    except Exception as exc:  # noqa: BLE001
        errors.append(f"order_book_fetch_error={type(exc).__name__}: {exc}")
        depth = _missing_depth(required_sizes, source=f"hyperliquid:{symbol}")

    primary_depth = _estimate_for_size(depth, primary_order_size_usd)
    cost = a2_cost_threshold_usd(
        per_leg_notional_usd=primary_order_size_usd,
        first_leg_taker_fee_bps=HL_TAKER_FEE_BPS,
        second_leg_taker_fee_bps=HL_TAKER_FEE_BPS,
        first_leg_slippage_bps=primary_depth.max_slippage_bps,
        second_leg_slippage_bps=primary_depth.max_slippage_bps,
        equity_usd=float(PLAN_A2_BAR["equity_usd"]),
        safety_margin_bps=float(PLAN_A2_BAR["safety_margin_bps"]),
    )
    stats = compute_rolling_24h_opportunity_stats(
        funding_rows,
        cost_per_window_usd=cost["threshold_usd"],
        equity_usd=float(PLAN_A2_BAR["equity_usd"]),
        window_hours=24.0,
        # Conservative direct-harvest assumption: only positive funding is counted.
        use_absolute_carry=False,
    )
    metadata = _hl_adl_liquidation_metadata(hl_meta, _base_from_symbol(symbol))
    base = _base_from_symbol(symbol)
    evidence = CandidateEvidence(
        venue="hyperliquid",
        symbol=symbol,
        candidate_type="hl_direct_funding",
        funding_history=funding_rows,
        cadence_hours=1.0,
        fee_schedule=_hl_fee_schedule(),
        depth_estimates=depth,
        opportunity_stats=stats,
        adl_liquidation_metadata=metadata,
        # The collector records basis/borrow and CEX comparison evidence for BTC/ETH through
        # the paired spread candidates. Alts stay fail-closed until a comparison is added.
        borrow_basis_risk_present=True,
        cex_comparison_present=base in {"BTC", "ETH"},
        measured_evidence=True,
        notes=(
            "HL direct windows use measured hourly funding rows; positive funding sums only.",
            (
                "Cost threshold uses conservative two-leg taker-fee proxy with both legs "
                "charged at HL base taker fee."
            ),
        ),
    )
    clears, reasons = evaluate_a2_candidate(evidence, PLAN_A2_BAR)
    return _candidate_report(
        candidate_id=f"HL_DIRECT_{base}",
        evidence=evidence,
        clears=clears,
        reasons=reasons,
        selection_reason=selection_reason,
        cost=cost,
        data_source_provenance={
            "funding": {
                "source": "ccxt.hyperliquid.fetch_funding_rate_history",
                "symbol": symbol,
                "since": _iso_ms(since_ms),
                "until": _iso_ms(until_ms),
                "fetched_at": fetched_at,
                "cadence_assumption": "1h per Hyperliquid docs and ccxt funding rows",
                "citation": HL_FUNDING_URL,
            },
            "depth": {
                "source": "ccxt.hyperliquid.fetch_order_book",
                "symbol": symbol,
                "fetched_at": depth[0].fetched_at if depth else fetched_at,
            },
            "fees": _hl_fee_schedule().as_dict(),
            "metadata": metadata.as_dict(),
            "errors": errors,
        },
    )


def _collect_hl_bybit_spread_candidate(
    *,
    hl: Any,
    bybit: Any,
    hl_meta: Any,
    repo_root: Path,
    cache_root: str,
    base: str,
    hl_symbol: str,
    bybit_symbol: str,
    scenario_id: str,
    lookback_days: float,
    primary_order_size_usd: float,
    required_sizes: tuple[float, ...],
) -> dict[str, Any]:
    fetched_at = _utc_now_iso()
    errors: list[str] = []
    dataset = load_dataset(repo_root / cache_root, scenario_id)
    bybit_funding = tuple(dataset.funding.get(bybit_symbol, ()))
    if bybit_funding:
        until_ms = min(_now_ms(), max(row.timestamp for row in bybit_funding))
    else:
        until_ms = _now_ms()
        errors.append("bybit_cache_missing_funding_rows")
    since_ms = until_ms - int(lookback_days * DAY_MS)

    try:
        hl_rows = _fetch_hl_funding_history(hl, hl_symbol, since_ms=since_ms, until_ms=until_ms)
    except Exception as exc:  # noqa: BLE001
        errors.append(f"hl_funding_fetch_error={type(exc).__name__}: {exc}")
        hl_rows = ()

    spread_rows = _hl_bybit_spread_rows(
        hl_rows, bybit_funding, since_ms=since_ms, until_ms=until_ms
    )

    try:
        hl_depth = _depth_estimates_from_exchange(hl, hl_symbol, required_sizes=required_sizes)
    except Exception as exc:  # noqa: BLE001
        errors.append(f"hl_order_book_fetch_error={type(exc).__name__}: {exc}")
        hl_depth = _missing_depth(required_sizes, source=f"hyperliquid:{hl_symbol}")
    try:
        bybit_depth = _depth_estimates_from_exchange(
            bybit, bybit_symbol, required_sizes=required_sizes
        )
    except Exception as exc:  # noqa: BLE001
        errors.append(f"bybit_order_book_fetch_error={type(exc).__name__}: {exc}")
        bybit_depth = _missing_depth(required_sizes, source=f"bybit:{bybit_symbol}")
    combined_depth = _combine_depths(hl_depth, bybit_depth)

    hl_primary = _estimate_for_size(hl_depth, primary_order_size_usd)
    bybit_primary = _estimate_for_size(bybit_depth, primary_order_size_usd)
    cost = a2_cost_threshold_usd(
        per_leg_notional_usd=primary_order_size_usd,
        first_leg_taker_fee_bps=HL_TAKER_FEE_BPS,
        second_leg_taker_fee_bps=BYBIT_TAKER_FEE_BPS,
        first_leg_slippage_bps=hl_primary.max_slippage_bps,
        second_leg_slippage_bps=bybit_primary.max_slippage_bps,
        equity_usd=float(PLAN_A2_BAR["equity_usd"]),
        safety_margin_bps=float(PLAN_A2_BAR["safety_margin_bps"]),
    )
    stats = compute_rolling_24h_opportunity_stats(
        spread_rows,
        cost_per_window_usd=cost["threshold_usd"],
        equity_usd=float(PLAN_A2_BAR["equity_usd"]),
        window_hours=24.0,
        use_absolute_carry=False,
    )
    metadata = _hl_adl_liquidation_metadata(hl_meta, base)
    evidence = CandidateEvidence(
        venue="hyperliquid+bybit",
        symbol=f"{hl_symbol} <-> {bybit_symbol}",
        candidate_type="hl_bybit_annualized_funding_spread",
        funding_history=spread_rows,
        cadence_hours=1.0,
        fee_schedule=_spread_fee_schedule(),
        depth_estimates=combined_depth,
        opportunity_stats=stats,
        adl_liquidation_metadata=metadata,
        borrow_basis_risk_present=True,
        cex_comparison_present=True,
        measured_evidence=True,
        notes=(
            "Spread rows are abs(HL hourly funding - latest Bybit 8h funding / 8) per hour.",
            "Bybit funding source is the committed aq-cache-v1 real venue cache.",
        ),
    )
    clears, reasons = evaluate_a2_candidate(evidence, PLAN_A2_BAR)
    return _candidate_report(
        candidate_id=f"HL_BYBIT_SPREAD_{base}",
        evidence=evidence,
        clears=clears,
        reasons=reasons,
        selection_reason="required BTC/ETH CeDeFi HL<->Bybit spread comparison",
        cost=cost,
        data_source_provenance={
            "hl_funding": {
                "source": "ccxt.hyperliquid.fetch_funding_rate_history",
                "symbol": hl_symbol,
                "since": _iso_ms(since_ms),
                "until": _iso_ms(until_ms),
                "fetched_at": fetched_at,
                "citation": HL_FUNDING_URL,
            },
            "bybit_funding": {
                "source": "ajentix_quant.data.cache.load_dataset",
                "cache_root": cache_root,
                "scenario_id": scenario_id,
                "symbol": bybit_symbol,
                "rows": len(bybit_funding),
                "first_timestamp": _iso_ms(bybit_funding[0].timestamp) if bybit_funding else None,
                "last_timestamp": _iso_ms(bybit_funding[-1].timestamp) if bybit_funding else None,
            },
            "depth": {
                "hyperliquid": {
                    "source": "ccxt.hyperliquid.fetch_order_book",
                    "symbol": hl_symbol,
                    "fetched_at": hl_depth[0].fetched_at if hl_depth else fetched_at,
                },
                "bybit": {
                    "source": "ccxt.bybit.fetch_order_book",
                    "symbol": bybit_symbol,
                    "fetched_at": bybit_depth[0].fetched_at if bybit_depth else fetched_at,
                },
            },
            "fees": {
                "hyperliquid": _hl_fee_schedule().as_dict(),
                "bybit": _bybit_fee_schedule().as_dict(),
            },
            "metadata": metadata.as_dict(),
            "errors": errors,
        },
        extra={
            "venue_depth_details": {
                "hyperliquid": [estimate.as_dict() for estimate in hl_depth],
                "bybit": [estimate.as_dict() for estimate in bybit_depth],
            }
        },
    )


def _candidate_report(
    *,
    candidate_id: str,
    evidence: CandidateEvidence,
    clears: bool,
    reasons: list[str],
    selection_reason: str,
    cost: dict[str, float],
    data_source_provenance: dict[str, Any],
    extra: dict[str, Any] | None = None,
) -> dict[str, Any]:
    primary_depth = max(
        evidence.depth_estimates, key=lambda item: item.order_size_usd, default=None
    )
    out = {
        "candidate_id": candidate_id,
        "venue": evidence.venue,
        "symbol": evidence.symbol,
        "candidate_type": evidence.candidate_type,
        "selection_reason": selection_reason,
        "clears": bool(clears),
        "reason_codes": list(reasons),
        "qualifying_24h_window_pct": (
            evidence.opportunity_stats.qualifying_pct if evidence.opportunity_stats else 0.0
        ),
        "qualifying_24h_window_count": (
            evidence.opportunity_stats.qualifying_windows if evidence.opportunity_stats else 0
        ),
        "cluster_count": (
            evidence.opportunity_stats.cluster_count if evidence.opportunity_stats else 0
        ),
        "max_single_week_share": (
            evidence.opportunity_stats.max_single_week_share if evidence.opportunity_stats else 0.0
        ),
        "primary_order_size_usd": primary_depth.order_size_usd if primary_depth else None,
        "primary_slippage_bps_per_leg": (
            primary_depth.max_slippage_bps if primary_depth else math.inf
        ),
        "cost_threshold_usd": cost,
        "evidence": evidence.as_dict(include_windows=False),
        "data_source_provenance": data_source_provenance,
    }
    if extra:
        out.update(extra)
    return _json_safe(out)


def _fetch_hl_funding_history(
    exchange: Any,
    symbol: str,
    *,
    since_ms: int,
    until_ms: int,
) -> tuple[FundingObservation, ...]:
    cursor = int(since_ms)
    by_ts: dict[int, FundingObservation] = {}
    for _ in range(40):
        if cursor > until_ms:
            break
        page = exchange.fetch_funding_rate_history(symbol, since=cursor, limit=500)
        if not page:
            break
        max_ts = cursor
        for row in page:
            ts = _row_timestamp(row)
            max_ts = max(max_ts, ts)
            if ts < since_ms or ts > until_ms or ts in by_ts:
                continue
            rate = _row_funding_rate(row)
            by_ts[ts] = FundingObservation(
                timestamp_ms=ts,
                rate=rate,
                interval_hours=1.0,
                source="ccxt.hyperliquid.fetch_funding_rate_history",
            )
        next_cursor = max_ts + 1
        if next_cursor <= cursor:
            raise RuntimeError(f"HL funding pagination did not advance for {symbol}")
        cursor = next_cursor
    else:
        raise RuntimeError(f"HL funding pagination exceeded safety bound for {symbol}")
    return tuple(by_ts[ts] for ts in sorted(by_ts))


def _hl_bybit_spread_rows(
    hl_rows: tuple[FundingObservation, ...],
    bybit_rows: tuple[Any, ...],
    *,
    since_ms: int,
    until_ms: int,
) -> tuple[FundingObservation, ...]:
    bybit = [row for row in bybit_rows if since_ms <= int(row.timestamp) <= until_ms]
    timestamps = [int(row.timestamp) for row in bybit]
    out: list[FundingObservation] = []
    for hl_row in hl_rows:
        idx = bisect_right(timestamps, int(hl_row.timestamp_ms)) - 1
        if idx < 0:
            continue
        bybit_row = bybit[idx]
        # HL rows are hourly. Bybit cache rows are 8h, so bybit.rate / 8 is the hourly carry.
        hourly_spread = abs(float(hl_row.rate) - float(bybit_row.rate) / 8.0)
        out.append(
            FundingObservation(
                timestamp_ms=hl_row.timestamp_ms,
                rate=hourly_spread,
                interval_hours=1.0,
                source="derived:abs(hl_hourly_funding-bybit_8h_funding/8)",
            )
        )
    return tuple(out)


def _depth_estimates_from_exchange(
    exchange: Any,
    symbol: str,
    *,
    required_sizes: tuple[float, ...],
) -> tuple[DepthSlippageEstimate, ...]:
    fetched_at = _utc_now_iso()
    book = exchange.fetch_order_book(symbol, limit=100)
    bids = tuple((float(price), float(amount)) for price, amount in book.get("bids", ()))
    asks = tuple((float(price), float(amount)) for price, amount in book.get("asks", ()))
    bid_depth = _book_depth_usd(bids)
    ask_depth = _book_depth_usd(asks)
    source = f"{getattr(exchange, 'id', 'exchange')}:{symbol}:fetch_order_book"
    out = []
    for size in required_sizes:
        out.append(
            DepthSlippageEstimate(
                order_size_usd=float(size),
                bid_slippage_bps=_book_slippage_bps(bids, float(size), side="sell"),
                ask_slippage_bps=_book_slippage_bps(asks, float(size), side="buy"),
                bid_depth_usd=bid_depth,
                ask_depth_usd=ask_depth,
                source=source,
                fetched_at=fetched_at,
            )
        )
    return tuple(out)


def _book_depth_usd(levels: tuple[tuple[float, float], ...]) -> float:
    return float(sum(price * amount for price, amount in levels if price > 0.0 and amount > 0.0))


def _book_slippage_bps(
    levels: tuple[tuple[float, float], ...],
    order_size_usd: float,
    *,
    side: str,
) -> float:
    clean = [(p, a) for p, a in levels if p > 0.0 and a > 0.0]
    if not clean or order_size_usd <= 0.0:
        return math.inf
    best = clean[0][0]
    remaining = float(order_size_usd)
    quote_filled = 0.0
    base_filled = 0.0
    for price, amount in clean:
        level_quote = price * amount
        take_quote = min(remaining, level_quote)
        quote_filled += take_quote
        base_filled += take_quote / price
        remaining -= take_quote
        if remaining <= 1e-9:
            break
    if remaining > 1e-6 or base_filled <= 0.0:
        return math.inf
    avg_price = quote_filled / base_filled
    if side == "buy":
        return max(0.0, (avg_price / best - 1.0) * 10_000.0)
    if side == "sell":
        return max(0.0, (1.0 - avg_price / best) * 10_000.0)
    raise ValueError(f"unsupported side: {side}")


def _combine_depths(
    first: tuple[DepthSlippageEstimate, ...],
    second: tuple[DepthSlippageEstimate, ...],
) -> tuple[DepthSlippageEstimate, ...]:
    sizes = sorted(
        {
            *(round(x.order_size_usd, 8) for x in first),
            *(round(x.order_size_usd, 8) for x in second),
        }
    )
    out: list[DepthSlippageEstimate] = []
    fetched_at = _utc_now_iso()
    for size in sizes:
        a = _estimate_for_size(first, size)
        b = _estimate_for_size(second, size)
        out.append(
            DepthSlippageEstimate(
                order_size_usd=float(size),
                bid_slippage_bps=max(a.bid_slippage_bps, b.bid_slippage_bps),
                ask_slippage_bps=max(a.ask_slippage_bps, b.ask_slippage_bps),
                bid_depth_usd=min(a.bid_depth_usd, b.bid_depth_usd),
                ask_depth_usd=min(a.ask_depth_usd, b.ask_depth_usd),
                source=f"max_slippage/min_depth({a.source},{b.source})",
                fetched_at=fetched_at,
            )
        )
    return tuple(out)


def _missing_depth(
    required_sizes: tuple[float, ...], *, source: str
) -> tuple[DepthSlippageEstimate, ...]:
    fetched_at = _utc_now_iso()
    return tuple(
        DepthSlippageEstimate(
            order_size_usd=float(size),
            bid_slippage_bps=math.inf,
            ask_slippage_bps=math.inf,
            bid_depth_usd=0.0,
            ask_depth_usd=0.0,
            source=source,
            fetched_at=fetched_at,
        )
        for size in required_sizes
    )


def _estimate_for_size(
    estimates: tuple[DepthSlippageEstimate, ...],
    size: float,
) -> DepthSlippageEstimate:
    for estimate in estimates:
        if round(float(estimate.order_size_usd), 8) == round(float(size), 8):
            return estimate
    return _missing_depth((float(size),), source="missing")[0]


def _select_hl_alt_symbols(
    hl: Any,
    markets: dict[str, Any],
    *,
    base_symbols: tuple[str, ...],
    limit: int,
) -> tuple[tuple[str, ...], str]:
    if limit <= 0:
        return (), "alt selection disabled by --alt-count=0"
    eligible = [
        symbol
        for symbol, market in markets.items()
        if market.get("swap")
        and market.get("active", True)
        and (market.get("settle") == "USDC" or symbol.endswith(":USDC"))
        and symbol not in base_symbols
    ]
    try:
        raw_rates = hl.fetch_funding_rates(eligible)
    except Exception as exc:  # noqa: BLE001
        return (), (
            "deterministic rule was top-by-current-abs-funding among active USDC swaps, "
            f"but fetch_funding_rates failed ({type(exc).__name__}: {exc}); "
            "no optional alts selected"
        )
    scored: list[tuple[float, str]] = []
    for symbol in eligible:
        row = raw_rates.get(symbol) if isinstance(raw_rates, dict) else None
        if not row:
            continue
        rate = row.get("fundingRate") or (row.get("info") or {}).get("funding")
        try:
            scored.append((abs(float(rate)), symbol))
        except (TypeError, ValueError):
            continue
    scored.sort(key=lambda item: (-item[0], item[1]))
    selected = tuple(symbol for _, symbol in scored[:limit])
    return selected, (
        "top-by-current-absolute-funding among active Hyperliquid USDC-settled swaps "
        "from ccxt.hyperliquid.fetch_funding_rates, excluding BTC/ETH, tie-broken by symbol"
    )


def _fetch_hl_meta() -> Any:
    payload = json.dumps({"type": "metaAndAssetCtxs"}).encode("utf-8")
    request = Request(
        "https://api.hyperliquid.xyz/info",
        data=payload,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urlopen(request, timeout=30) as response:  # noqa: S310 - fixed public HTTPS URL
            return json.loads(response.read().decode("utf-8"))
    except (OSError, URLError, json.JSONDecodeError) as exc:
        return {"error": f"{type(exc).__name__}: {exc}"}


def _hl_adl_liquidation_metadata(meta: Any, base: str) -> AdlLiquidationMetadataEvidence:
    details: dict[str, Any] = {"base": base}
    margin_tiers_present = False
    if isinstance(meta, list) and len(meta) >= 2 and isinstance(meta[0], dict):
        universe = meta[0].get("universe") or []
        contexts = meta[1] if isinstance(meta[1], list) else []
        margin_tables = meta[0].get("marginTables") or []
        idx = next((i for i, item in enumerate(universe) if item.get("name") == base), None)
        if idx is not None:
            asset_meta = dict(universe[idx])
            ctx = (
                dict(contexts[idx])
                if idx < len(contexts) and isinstance(contexts[idx], dict)
                else {}
            )
            details.update(
                {
                    "asset_meta": asset_meta,
                    "asset_context_keys": sorted(ctx.keys()),
                    "markPx": ctx.get("markPx"),
                    "oraclePx": ctx.get("oraclePx"),
                    "openInterest": ctx.get("openInterest"),
                    "impactPxs": ctx.get("impactPxs"),
                    "margin_table_count": len(margin_tables),
                }
            )
            margin_tiers_present = bool(asset_meta.get("maxLeverage") and margin_tables)
    else:
        details["meta_fetch_error"] = (
            meta.get("error") if isinstance(meta, dict) else "unexpected meta payload"
        )
    return AdlLiquidationMetadataEvidence(
        adl_present=True,
        liquidation_present=True,
        margin_tiers_present=margin_tiers_present,
        source_urls=(HL_API_PERPS_URL, HL_LIQUIDATION_URL, HL_ADL_URL),
        details=details,
    )


def _hl_fee_schedule() -> FeeScheduleEvidence:
    return FeeScheduleEvidence(
        venue="hyperliquid",
        taker_fee_bps=HL_TAKER_FEE_BPS,
        maker_fee_bps=HL_MAKER_FEE_BPS,
        source_url=HL_FEE_URL,
        source_note="Official docs base perps tier: 0.045% taker, 0.015% maker.",
    )


def _bybit_fee_schedule() -> FeeScheduleEvidence:
    return FeeScheduleEvidence(
        venue="bybit",
        taker_fee_bps=BYBIT_TAKER_FEE_BPS,
        maker_fee_bps=BYBIT_MAKER_FEE_BPS,
        source_url=BYBIT_FEE_URL,
        source_note=(
            "Official Bybit futures docs non-VIP USDT/USDC perpetuals: "
            "0.055% taker, 0.02% maker."
        ),
    )


def _spread_fee_schedule() -> FeeScheduleEvidence:
    return FeeScheduleEvidence(
        venue="hyperliquid+bybit",
        taker_fee_bps=HL_TAKER_FEE_BPS + BYBIT_TAKER_FEE_BPS,
        maker_fee_bps=HL_MAKER_FEE_BPS + BYBIT_MAKER_FEE_BPS,
        source_url=f"{HL_FEE_URL} ; {BYBIT_FEE_URL}",
        source_note="Composite known taker schedules for the two spread legs.",
    )


def _assumptions(primary_order_size_usd: float) -> dict[str, Any]:
    return {
        "hyperliquid_funding_cadence": {
            "hours": 1.0,
            "citation": HL_FUNDING_URL,
            "note": (
                "Hyperliquid docs state funding is paid every hour; ccxt history rows are "
                "treated as per-hour fractional funding."
            ),
        },
        "hyperliquid_fee_schedule": _hl_fee_schedule().as_dict(),
        "bybit_fee_schedule": _bybit_fee_schedule().as_dict(),
        "depth": {
            "required_sizes_usd_per_leg": list(PLAN_A2_BAR["depth_per_leg_usd"]),
            "primary_order_size_usd": primary_order_size_usd,
            "slippage_limit_bps_per_leg": PLAN_A2_BAR["max_slippage_bps_per_leg"],
            "method": (
                "walk public order book to estimate average execution price slippage for "
                "$250/$500 marketable buy and sell notional"
            ),
        },
        "cost_threshold": {
            "method": (
                "G002 round_trip_cost_usd_with_fee_bps for explicit taker fees with "
                "measured order-book slippage added separately, plus PLAN_A2_BAR "
                "safety_margin_bps on $1000 equity"
            ),
            "conservative_direct_proxy": (
                "HL direct candidates use a two-leg fee proxy with both legs charged at "
                "Hyperliquid base taker fee; no maker rebates authorize."
            ),
        },
    }


def _summary_table(candidates: list[dict[str, Any]]) -> str:
    lines = [
        (
            "candidate | qual_pct | qual_windows | clusters | max_week | primary_slip_bps "
            "| clears | reasons"
        ),
        "--- | ---: | ---: | ---: | ---: | ---: | :---: | ---",
    ]
    for candidate in candidates:
        lines.append(
            f"{candidate['candidate_id']} | "
            f"{candidate['qualifying_24h_window_pct']:.2%} | "
            f"{candidate['qualifying_24h_window_count']} | "
            f"{candidate['cluster_count']} | "
            f"{candidate['max_single_week_share']:.2%} | "
            f"{_fmt(candidate['primary_slippage_bps_per_leg'])} | "
            f"{candidate['clears']} | "
            f"{','.join(candidate['reason_codes']) or '-'}"
        )
    return "\n".join(lines)


def _markdown_report(payload: dict[str, Any]) -> str:
    lines = [
        "# Pivot venue feasibility v2",
        "",
        f"- run_status: {payload['run_status']}",
        f"- run_id: {payload['run_id']}",
        f"- generated_at: {payload['generated_at']}",
        f"- preregistration_sha256: {payload['preregistration_sha256']}",
        f"- overall: {payload['overall']['conclusion']}",
        "",
        "## Assumptions and citations",
        "",
        f"- Hyperliquid funding cadence: hourly ({HL_FUNDING_URL}).",
        (
            f"- Hyperliquid fee: base perps taker {HL_TAKER_FEE_BPS:.1f} bps, "
            f"maker {HL_MAKER_FEE_BPS:.1f} bps ({HL_FEE_URL})."
        ),
        (
            f"- Bybit fee for spread legs: non-VIP perps taker "
            f"{BYBIT_TAKER_FEE_BPS:.1f} bps, maker {BYBIT_MAKER_FEE_BPS:.1f} bps "
            f"({BYBIT_FEE_URL})."
        ),
        (
            f"- HL metadata/liquidation/ADL docs: {HL_API_PERPS_URL}, "
            f"{HL_LIQUIDATION_URL}, {HL_ADL_URL}."
        ),
        "- Slippage: public order-book walk at $250/$500 per leg; primary reported size is $500.",
        (
            "- Direct-HL carry is conservative: only positive 24h funding sums are counted; "
            "roadmap APR claims do not authorize."
        ),
        "",
        "## Candidate results",
        "",
        _summary_table(payload["candidates"]),
        "",
    ]
    for candidate in payload["candidates"]:
        evidence = candidate["evidence"]
        stats = evidence.get("opportunity_stats") or {}
        lines.extend(
            [
                f"### {candidate['candidate_id']}",
                "",
                f"- symbol: {candidate['symbol']}",
                f"- candidate_type: {candidate['candidate_type']}",
                f"- selection_reason: {candidate['selection_reason']}",
                (
                    f"- funding rows/history days: {evidence['funding_history_rows']} / "
                    f"{_fmt(evidence['funding_history_days'])}"
                ),
                (
                    f"- qualifying windows: {candidate['qualifying_24h_window_count']} "
                    f"({candidate['qualifying_24h_window_pct']:.2%}) out of "
                    f"{stats.get('total_windows', 0)}"
                ),
                f"- clusters: {candidate['cluster_count']}",
                f"- max single-week positive-edge share: {candidate['max_single_week_share']:.2%}",
                (
                    "- primary slippage bps per leg: "
                    f"{_fmt(candidate['primary_slippage_bps_per_leg'])}"
                ),
                (
                    "- cost threshold USD: "
                    f"{_fmt((candidate.get('cost_threshold_usd') or {}).get('threshold_usd'))}"
                ),
                f"- clears: {candidate['clears']}",
                f"- reason_codes: {', '.join(candidate['reason_codes']) or '-'}",
                "",
            ]
        )
    if payload["overall"]["any_candidate_clears"]:
        lines.extend(
            [
                "## PHASE 3 RALPLAN REQUEST",
                "",
                (
                    "The following candidate(s) cleared the pre-registered A2 bar and require "
                    "a separate Phase 3 ralplan before any adapter/cache/backtest buildout:"
                ),
                "",
                *[
                    f"- {candidate_id}"
                    for candidate_id in payload["overall"]["clearing_candidate_ids"]
                ],
                "",
                (
                    "Phase 3 must scope the venue adapter, immutable cache format, "
                    "no-live-order replay model, liquidation/ADL modeling, "
                    "basis/borrow risk, and a held-out backtest plan. No adapter or "
                    "backtest was built in G003."
                ),
                "",
            ]
        )
    else:
        lines.extend(["## Overall conclusion", "", "no evidence-supported pivot candidate yet", ""])
    return "\n".join(lines)


def _resolve_preregistration(repo_root: Path, explicit: str | None) -> Path:
    if explicit:
        path = Path(explicit)
        return path if path.is_absolute() else repo_root / path
    prereg_dir = repo_root / "docs" / "preregistration"
    artifacts = sorted(prereg_dir.glob("stratv2-*.json")) if prereg_dir.is_dir() else []
    if len(artifacts) != 1:
        raise SystemExit(
            "expected exactly one docs/preregistration/stratv2-*.json artifact, "
            f"found {len(artifacts)}"
        )
    return artifacts[0]


def _base_from_symbol(symbol: str) -> str:
    return symbol.split("/", 1)[0]


def _row_timestamp(row: Mapping[str, Any]) -> int:
    return int(row.get("timestamp") or (row.get("info") or {}).get("time"))


def _row_funding_rate(row: Mapping[str, Any]) -> float:
    value = row.get("fundingRate")
    if value is None:
        value = (row.get("info") or {}).get("fundingRate")
    if value is None:
        value = (row.get("info") or {}).get("funding")
    return float(value)


def _sha256_file(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _now_ms() -> int:
    return int(datetime.now(UTC).timestamp() * 1000)


def _utc_now_iso() -> str:
    return datetime.now(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _iso_ms(timestamp_ms: int | None) -> str | None:
    if timestamp_ms is None:
        return None
    return (
        datetime.fromtimestamp(int(timestamp_ms) / 1000.0, UTC)
        .isoformat()
        .replace("+00:00", "Z")
    )


def _fmt(value: Any) -> str:
    try:
        f = float(value)
    except (TypeError, ValueError):
        return "-"
    if not math.isfinite(f):
        return "missing"
    return f"{f:.4f}"


def _json_safe(value: Any) -> Any:
    if isinstance(value, dict):
        return {str(k): _json_safe(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_safe(v) for v in value]
    if isinstance(value, float):
        return value if math.isfinite(value) else None
    return value


if __name__ == "__main__":
    raise SystemExit(main())

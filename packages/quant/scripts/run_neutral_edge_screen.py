#!/usr/bin/env python3
"""Cheap deterministic edge SCREEN for alt cross-venue funding-spread harvest.

Measures, over a fixed lookback, the delta-neutral collectible funding spread
(``|HL funding - Binance funding|``) for a frozen alt universe, plus per-name execution
slippage at an executable clip, then applies ``neutral_screen`` bars and the basket
de-concentration test. Writes ``reports/neutral_edge_screen.{json,md}``.

This is a SCREEN: a PROMISING_BUILD verdict only justifies a fully pre-registered Phase-3
build. It NEVER authorizes live capital. Network, read-only, refuses to run under CI.
"""

from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import os
import statistics
import sys
import time
from pathlib import Path
from typing import Any

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from ajentix_quant.backtest.costs import safety_margin_usd  # noqa: E402
from ajentix_quant.research.neutral_screen import (  # noqa: E402
    CROSS_VENUE_BAR,
    PORTFOLIO_BAR,
    REASON_SHORT_HISTORY,
    SCREEN_SCHEMA_VERSION,
    VERDICT_INCONCLUSIVE,
    CrossVenueEvidence,
    aggregate_portfolio_concentration,
    align_cross_venue_spread,
    cross_venue_spread_stats,
    direction_stability_pct,
    evaluate_cross_venue_candidate,
    mean_collectible_apr_pct,
    screen_history_sufficient,
    screen_verdict_multi_hold,
    split_spread_at,
    walk_forward_survival,
)
from ajentix_quant.research.venue_evidence import (  # noqa: E402
    FundingObservation,
    funding_history_days,
    round_trip_slippage_cost_usd_for_two_legs,
    taker_round_trip_cost_usd_for_two_legs,
)

DAY_MS = 86_400_000
HL_TAKER_FEE_BPS = 4.5
BINANCE_TAKER_FEE_BPS = 5.0
HL_FEE_URL = "https://hyperliquid.gitbook.io/hyperliquid-docs/trading/fees"
BINANCE_FEE_URL = "https://www.binance.com/en/fee/futureFee"

# Frozen universe: hedgeable alts present on Hyperliquid + Binance USD-M perps, spanning
# liquid mid-caps (diversifiers) and known high-funding names (edge sources). Frozen here
# so the screen cannot be cherry-picked post-hoc.
UNIVERSE: tuple[str, ...] = (
    "ADA", "CRV", "ICP", "XLM", "IOTA", "LINK", "AVAX", "DOGE",
    "SUI", "LTC", "OP", "ARB", "TRUMP", "ALT", "GAS", "HYPER", "BLUR", "WLD",
)
HOLD_WINDOWS_HOURS: tuple[float, ...] = (168.0, 504.0)  # 7d, 21d
PRIMARY_HOLD_HOURS = 168.0
WALK_FORWARD_HOLD_HOURS = 504.0  # 21d hold is the carry-amortizing horizon under test


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Alt cross-venue funding-spread edge screen.")
    parser.add_argument("--repo-root", default=str(Path(__file__).resolve().parents[1]))
    parser.add_argument("--reports-dir", default="reports")
    parser.add_argument("--lookback-days", type=float, default=365.0)
    parser.add_argument("--test-days", type=float, default=120.0)
    parser.add_argument("--per-name-notional", type=float, default=250.0)
    args = parser.parse_args(argv)

    if os.environ.get("CI"):
        print("refusing to run under CI: this screen performs live network reads", file=sys.stderr)
        return 2

    import ccxt  # noqa: PLC0415 - imported after the CI guard so CI never touches network

    repo_root = Path(args.repo_root).resolve()
    reports_dir = repo_root / args.reports_dir
    fetched_at = _utc_now_iso()
    now_ms = int(time.time() * 1000)
    since_ms = now_ms - int(args.lookback_days * DAY_MS)
    boundary_ms = now_ms - int(args.test_days * DAY_MS)

    hl = ccxt.hyperliquid()
    bn = ccxt.binanceusdm()

    hold_labels = [f"{int(h)}h" for h in HOLD_WINDOWS_HOURS]
    candidates: list[dict[str, Any]] = []
    per_name_stats_by_hold: dict[str, dict[str, Any]] = {lbl: {} for lbl in hold_labels}
    per_name_history: dict[str, float] = {}
    clears_by_hold: dict[str, list[str]] = {lbl: [] for lbl in hold_labels}
    train_clearing: list[str] = []
    test_clearing: list[str] = []
    test_stats_by_name: dict[str, Any] = {}

    for base in UNIVERSE:
        report = _screen_symbol(
            hl=hl,
            bn=bn,
            base=base,
            since_ms=since_ms,
            boundary_ms=boundary_ms,
            per_name_notional=float(args.per_name_notional),
            fetched_at=fetched_at,
        )
        candidates.append(report)
        if report.get("history_days") is not None:
            per_name_history[base] = float(report["history_days"])
        stats_map = report.pop("_stats_by_hold", {}) or {}
        for lbl, stats_obj in stats_map.items():
            if stats_obj is not None:
                per_name_stats_by_hold[lbl][base] = stats_obj
        for lbl, did_clear in report.get("clears_by_hold", {}).items():
            if did_clear:
                clears_by_hold[lbl].append(base)
        wf = report.pop("_walk_forward", None)
        if wf is not None:
            if wf["clears_train"]:
                train_clearing.append(base)
            if wf["clears_test"]:
                test_clearing.append(base)
            if wf["test_stats"] is not None:
                test_stats_by_name[base] = wf["test_stats"]

    portfolio_by_hold = {
        lbl: aggregate_portfolio_concentration(per_name_stats_by_hold[lbl])
        for lbl in hold_labels
    }
    history_sufficient = screen_history_sufficient(
        per_name_history,
        min_days=float(CROSS_VENUE_BAR["min_days_history"]),
        min_names=int(PORTFOLIO_BAR["min_qualifying_names"]),
    )
    multi = screen_verdict_multi_hold(
        clears_by_hold=clears_by_hold,
        portfolio_by_hold=portfolio_by_hold,
        hold_order=hold_labels,
        history_sufficient=history_sufficient,
    )
    in_sample_verdict = multi["verdict"]
    in_sample_reasons = multi["reasons"]
    chosen_hold = multi.get("minimum_passing_hold") or f"{int(PRIMARY_HOLD_HOURS)}h"
    portfolio = portfolio_by_hold[chosen_hold]
    clearing_names = clears_by_hold[chosen_hold]
    test_portfolio = aggregate_portfolio_concentration(
        {n: test_stats_by_name[n] for n in (set(train_clearing) & set(test_clearing))}
    )
    walk_forward = walk_forward_survival(
        train_clearing=train_clearing,
        test_clearing=test_clearing,
        test_portfolio=test_portfolio,
        min_surviving_names=int(PORTFOLIO_BAR["min_qualifying_names"]),
        max_test_week_share=float(PORTFOLIO_BAR["max_portfolio_single_week_share"]),
    )
    # The out-of-sample walk-forward is AUTHORITATIVE: a build is justified only if the
    # train-selected basket survives in the disjoint test window. In-sample is informational.
    if not history_sufficient:
        verdict = VERDICT_INCONCLUSIVE
        verdict_reasons = [REASON_SHORT_HISTORY]
    else:
        verdict = walk_forward["verdict"]
        verdict_reasons = walk_forward["reasons"]

    payload: dict[str, Any] = {
        "schema_version": SCREEN_SCHEMA_VERSION,
        "verdict": verdict,
        "verdict_reasons": verdict_reasons,
        "is_authorization": False,
        "authorization_note": (
            "SCREEN ONLY. A PROMISING_BUILD verdict justifies opening a fully "
            "pre-registered Phase-3 build; it does NOT authorize live capital."
        ),
        "test_days": float(args.test_days),
        "lookback_days": float(args.lookback_days),
        "walk_forward": {
            "authoritative": True,
            "verdict": walk_forward["verdict"],
            "reasons": walk_forward["reasons"],
            "survival_rate": round(walk_forward["survival_rate"], 4),
            "train_clearing_names": walk_forward["train_clearing_names"],
            "test_clearing_names": walk_forward["test_clearing_names"],
            "surviving_names": walk_forward["surviving_names"],
            "decayed_names": walk_forward["decayed_names"],
            "test_max_single_week_share": round(walk_forward["test_max_single_week_share"], 4),
        },
        "in_sample_full_window": {
            "verdict": in_sample_verdict,
            "reasons": in_sample_reasons,
            "minimum_passing_hold": multi.get("minimum_passing_hold"),
        },
        "per_name_notional_usd": float(args.per_name_notional),
        "primary_hold_window_hours": PRIMARY_HOLD_HOURS,
        "hold_windows_hours": list(HOLD_WINDOWS_HOURS),
        "universe": list(UNIVERSE),
        "cross_venue_bar": CROSS_VENUE_BAR,
        "portfolio_bar": PORTFOLIO_BAR,
        "fees": {
            "hyperliquid_taker_bps": HL_TAKER_FEE_BPS,
            "hyperliquid_fee_url": HL_FEE_URL,
            "binance_taker_bps": BINANCE_TAKER_FEE_BPS,
            "binance_fee_url": BINANCE_FEE_URL,
        },
        "minimum_passing_hold": multi.get("minimum_passing_hold"),
        "hold_horizon_dependent": multi.get("hold_horizon_dependent", False),
        "chosen_hold": chosen_hold,
        "per_hold_verdict": multi.get("per_hold", {}),
        "portfolio_by_hold": {
            lbl: {
                "max_single_week_share": portfolio_by_hold[lbl]["max_single_week_share"],
                "names_with_positive_edge": portfolio_by_hold[lbl]["names_with_positive_edge"],
                "clearing_names": clears_by_hold[lbl],
            }
            for lbl in hold_labels
        },
        "clearing_names": clearing_names,
        "portfolio": portfolio,
        "candidates": candidates,
        "generated_at": fetched_at,
    }
    payload["content_hash"] = _canonical_sha256(
        {k: v for k, v in payload.items() if k not in {"content_hash", "generated_at"}}
    )

    reports_dir.mkdir(parents=True, exist_ok=True)
    json_path = reports_dir / "neutral_edge_screen.json"
    md_path = reports_dir / "neutral_edge_screen.md"
    json_path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    md_path.write_text(_markdown(payload), encoding="utf-8")

    print(f"verdict(OOS-authoritative)={verdict}")
    print(f"verdict_reasons={','.join(verdict_reasons) or '-'}")
    print(f"in_sample_verdict={in_sample_verdict} (min_hold={multi.get('minimum_passing_hold')})")
    print(f"train_clearing={','.join(walk_forward['train_clearing_names']) or '-'}")
    print(f"test_clearing={','.join(walk_forward['test_clearing_names']) or '-'}")
    print(f"surviving_OOS={','.join(walk_forward['surviving_names']) or '-'}")
    print(f"survival_rate={walk_forward['survival_rate']:.2f}")
    print(f"test_max_single_week_share={walk_forward['test_max_single_week_share']:.3f}")
    print(f"wrote={json_path.relative_to(repo_root)}")
    print(f"wrote={md_path.relative_to(repo_root)}")
    return 0


def _screen_symbol(
    *,
    hl: Any,
    bn: Any,
    base: str,
    since_ms: int,
    boundary_ms: int,
    per_name_notional: float,
    fetched_at: str,
) -> dict[str, Any]:
    hl_symbol = f"{base}/USDC:USDC"
    bn_symbol = f"{base}/USDT:USDT"
    errors: list[str] = []

    hl_rows = _safe_funding(hl, hl_symbol, since_ms, errors, "hl")
    bn_rows = _safe_funding(bn, bn_symbol, since_ms, errors, "bn")
    if not hl_rows or not bn_rows:
        return {
            "base": base,
            "clears": False,
            "clears_by_hold": {},
            "reason_codes": ["MISSING_FUNDING_HISTORY"],
            "history_days": None,
            "errors": errors,
            "_stats_by_hold": {},
            "_walk_forward": None,
        }

    spread = align_cross_venue_spread(hl_rows, bn_rows)
    history_days = funding_history_days(spread)
    dir_stable = direction_stability_pct(spread)
    mean_apr = mean_collectible_apr_pct(spread)

    hl_slip = _safe_slippage(hl, hl_symbol, per_name_notional, errors, "hl")
    bn_slip = _safe_slippage(bn, bn_symbol, per_name_notional, errors, "bn")
    max_slip = max(hl_slip, bn_slip)

    fee_cost = taker_round_trip_cost_usd_for_two_legs(
        per_leg_notional_usd=per_name_notional,
        first_leg_taker_fee_bps=HL_TAKER_FEE_BPS,
        second_leg_taker_fee_bps=BINANCE_TAKER_FEE_BPS,
    )
    slip_cost = round_trip_slippage_cost_usd_for_two_legs(
        per_leg_notional_usd=per_name_notional,
        first_leg_slippage_bps=hl_slip,
        second_leg_slippage_bps=bn_slip,
    )
    margin = safety_margin_usd(
        notional=per_name_notional,
        safety_margin_bps=float(CROSS_VENUE_BAR["safety_margin_bps"]),
    )
    cost_per_window = fee_cost + slip_cost + margin

    stats_by_hold: dict[str, Any] = {}
    stats_objs: dict[str, Any] = {}
    clears_by_hold: dict[str, bool] = {}
    reasons_by_hold: dict[str, list[str]] = {}
    for hold in HOLD_WINDOWS_HOURS:
        label = f"{int(hold)}h"
        stats = cross_venue_spread_stats(
            spread,
            cost_per_window_usd=cost_per_window,
            per_name_notional_usd=per_name_notional,
            hold_window_hours=hold,
        )
        stats_by_hold[label] = {
            "total_windows": stats.total_windows,
            "qualifying_windows": stats.qualifying_windows,
            "qualifying_pct": (stats.qualifying_windows / stats.total_windows)
            if stats.total_windows
            else 0.0,
        }
        stats_objs[label] = stats
        evidence = CrossVenueEvidence(
            base=base,
            long_venue="hyperliquid",
            short_venue="binanceusdm",
            spread_rows=spread,
            stats=stats,
            direction_stability_pct=dir_stable,
            max_slippage_bps_per_leg=max_slip,
            history_days=history_days,
            mean_collectible_apr_pct=mean_apr,
        )
        clears, reasons = evaluate_cross_venue_candidate(evidence, CROSS_VENUE_BAR)
        clears_by_hold[label] = clears
        reasons_by_hold[label] = reasons

    # Out-of-sample walk-forward at the carry-amortizing 21d hold: does the bar still clear
    # in a disjoint TEST window? This is the authoritative anti-overfit check.
    train_spread, test_spread = split_spread_at(spread, boundary_ms)
    wf_clears: dict[str, bool] = {}
    wf_qual: dict[str, float] = {}
    wf_test_stats = None
    for seg_name, seg in (("train", train_spread), ("test", test_spread)):
        seg_days = funding_history_days(seg)
        seg_stats = cross_venue_spread_stats(
            seg,
            cost_per_window_usd=cost_per_window,
            per_name_notional_usd=per_name_notional,
            hold_window_hours=WALK_FORWARD_HOLD_HOURS,
        )
        seg_ev = CrossVenueEvidence(
            base=base,
            long_venue="hyperliquid",
            short_venue="binanceusdm",
            spread_rows=seg,
            stats=seg_stats,
            direction_stability_pct=direction_stability_pct(seg),
            max_slippage_bps_per_leg=max_slip,
            history_days=seg_days,
            mean_collectible_apr_pct=mean_collectible_apr_pct(seg),
        )
        seg_clears, _ = evaluate_cross_venue_candidate(seg_ev, CROSS_VENUE_BAR)
        wf_clears[seg_name] = seg_clears
        wf_qual[seg_name] = (
            seg_stats.qualifying_windows / seg_stats.total_windows
            if seg_stats.total_windows
            else 0.0
        )
        if seg_name == "test":
            wf_test_stats = seg_stats

    primary_label = f"{int(PRIMARY_HOLD_HOURS)}h"
    return {
        "base": base,
        "long_venue": "hyperliquid",
        "short_venue": "binanceusdm",
        "clears": clears_by_hold.get(primary_label, False),
        "clears_by_hold": clears_by_hold,
        "reason_codes": reasons_by_hold.get(primary_label, []),
        "reason_codes_by_hold": reasons_by_hold,
        "history_days": round(history_days, 1),
        "direction_stability_pct": round(dir_stable, 4),
        "mean_collectible_apr_pct": round(mean_apr, 2),
        "max_slippage_bps_per_leg": round(max_slip, 4),
        "cost_per_window_usd": round(cost_per_window, 5),
        "stats_by_hold": stats_by_hold,
        "data_source": {
            "hl_funding": "ccxt.hyperliquid.fetch_funding_rate_history",
            "binance_funding": "ccxt.binanceusdm.fetch_funding_rate_history",
            "hl_rows": len(hl_rows),
            "bn_rows": len(bn_rows),
            "fetched_at": fetched_at,
            "errors": errors,
        },
        "walk_forward": {
            "clears_train": wf_clears.get("train", False),
            "clears_test": wf_clears.get("test", False),
            "train_qual_pct": round(wf_qual.get("train", 0.0), 4),
            "test_qual_pct": round(wf_qual.get("test", 0.0), 4),
        },
        "_stats_by_hold": stats_objs,
        "_walk_forward": {
            "clears_train": wf_clears.get("train", False),
            "clears_test": wf_clears.get("test", False),
            "test_stats": wf_test_stats,
        },
    }


def _safe_funding(
    exchange: Any, symbol: str, since_ms: int, errors: list[str], tag: str
) -> tuple[FundingObservation, ...]:
    try:
        return _fetch_funding_history(exchange, symbol, since_ms)
    except Exception as exc:  # noqa: BLE001 - report missing data, never fabricate
        errors.append(f"{tag}_funding_error={type(exc).__name__}: {exc}")
        return ()


def _fetch_funding_history(
    exchange: Any, symbol: str, since_ms: int
) -> tuple[FundingObservation, ...]:
    collected: dict[int, FundingObservation] = {}
    cursor = since_ms
    for _ in range(10):
        batch = exchange.fetch_funding_rate_history(symbol, since=cursor, limit=1000)
        if not batch:
            break
        for row in batch:
            ts = int(row["timestamp"])
            rate = row.get("fundingRate")
            if rate is None:
                continue
            collected[ts] = FundingObservation(
                timestamp_ms=ts,
                rate=float(rate),
                interval_hours=_infer_interval_hours(batch),
                source=f"{exchange.id}:{symbol}",
            )
        nxt = int(batch[-1]["timestamp"]) + 1
        if nxt <= cursor:
            break
        cursor = nxt
        if len(batch) < 500:
            break
    return tuple(sorted(collected.values(), key=lambda r: r.timestamp_ms))


def _infer_interval_hours(batch: list[dict[str, Any]]) -> float:
    if len(batch) < 2:
        return 8.0
    diffs = [
        (int(batch[i + 1]["timestamp"]) - int(batch[i]["timestamp"])) / 3_600_000.0
        for i in range(min(10, len(batch) - 1))
    ]
    diffs = [d for d in diffs if d > 0]
    if not diffs:
        return 8.0
    return round(statistics.median(diffs), 4)


def _safe_slippage(
    exchange: Any, symbol: str, notional_usd: float, errors: list[str], tag: str
) -> float:
    try:
        return _measure_slippage_bps(exchange, symbol, notional_usd)
    except Exception as exc:  # noqa: BLE001
        errors.append(f"{tag}_slippage_error={type(exc).__name__}: {exc}")
        return float("inf")


def _measure_slippage_bps(exchange: Any, symbol: str, notional_usd: float) -> float:
    book = exchange.fetch_order_book(symbol, limit=50)
    bids = book.get("bids") or []
    asks = book.get("asks") or []
    if not bids or not asks:
        return float("inf")
    best_bid = float(bids[0][0])
    best_ask = float(asks[0][0])
    mid = (best_bid + best_ask) / 2.0
    if mid <= 0:
        return float("inf")
    buy_px = _walk_book(asks, notional_usd)
    sell_px = _walk_book(bids, notional_usd)
    if buy_px is None or sell_px is None:
        return float("inf")
    buy_slip = (buy_px - mid) / mid * 10_000.0
    sell_slip = (mid - sell_px) / mid * 10_000.0
    return max(buy_slip, sell_slip)


def _walk_book(levels: list[list[float]], notional_usd: float) -> float | None:
    filled_usd = 0.0
    cost = 0.0
    qty = 0.0
    for level in levels:
        px = float(level[0])
        size = float(level[1])
        level_usd = px * size
        take_usd = min(level_usd, notional_usd - filled_usd)
        take_qty = take_usd / px if px > 0 else 0.0
        cost += take_qty * px
        qty += take_qty
        filled_usd += take_usd
        if filled_usd >= notional_usd - 1e-9:
            break
    if filled_usd < notional_usd - 1e-9 or qty <= 0:
        return None
    return cost / qty


def _markdown(payload: dict[str, Any]) -> str:
    lines = [
        "# Alt Cross-Venue Funding-Spread Edge Screen",
        "",
        f"- **Verdict (OUT-OF-SAMPLE, authoritative):** {payload['verdict']}",
        f"- **Verdict reasons:** {', '.join(payload['verdict_reasons']) or '-'}",
        f"- **SCREEN ONLY** — {payload['authorization_note']}",
        "",
        "## Out-of-sample walk-forward (the build gate)",
        f"- train/test split: last {payload['test_days']}d = TEST, prior = TRAIN",
        f"- train-clearing: {', '.join(payload['walk_forward']['train_clearing_names']) or '-'}",
        f"- test-clearing: {', '.join(payload['walk_forward']['test_clearing_names']) or '-'}",
        "- **surviving (clear in BOTH): "
        + (", ".join(payload["walk_forward"]["surviving_names"]) or "-")
        + "**",
        f"- survival rate: {payload['walk_forward']['survival_rate']:.2f} | "
        f"test weekly concentration: "
        f"{payload['walk_forward']['test_max_single_week_share']:.3f}",
        f"- decayed (train-only, died OOS): "
        f"{', '.join(payload['walk_forward']['decayed_names']) or '-'}",
        f"- in-sample full-window verdict: {payload['in_sample_full_window']['verdict']}"
        f" (min hold {payload['in_sample_full_window']['minimum_passing_hold']})",
        f"- lookback: {payload['lookback_days']}d | per-name notional: "
        f"${payload['per_name_notional_usd']} | primary hold: "
        f"{int(payload['primary_hold_window_hours'])}h",
        f"- minimum passing hold: {payload.get('minimum_passing_hold') or 'none'}"
        f" (hold-horizon-dependent: {payload.get('hold_horizon_dependent')})",
        f"- clearing names @ chosen hold {payload.get('chosen_hold')}: "
        f"{', '.join(payload['clearing_names']) or '-'}",
        "- per-hold clearing: "
        + "; ".join(
            f"{lbl}: {len(v['clearing_names'])} names (wk share {v['max_single_week_share']:.2f})"
            for lbl, v in payload.get("portfolio_by_hold", {}).items()
        ),
        f"- names with positive edge @ chosen hold: "
        f"{payload['portfolio']['names_with_positive_edge']} / {len(payload['universe'])}",
        f"- content hash: {payload['content_hash']}",
        "",
        "| base | clears | mean APR% | dir-stable% | slip bps/leg | 7d qual% | reasons |",
        "| --- | --- | --- | --- | --- | --- | --- |",
    ]
    for c in payload["candidates"]:
        q7 = c.get("stats_by_hold", {}).get("168h", {}).get("qualifying_pct")
        lines.append(
            f"| {c['base']} | {c.get('clears')} | "
            f"{_fmt(c.get('mean_collectible_apr_pct'))} | "
            f"{_fmt(c.get('direction_stability_pct'))} | "
            f"{_fmt(c.get('max_slippage_bps_per_leg'))} | {_fmt(q7)} | "
            f"{','.join(c.get('reason_codes', [])) or '-'} |"
        )
    lines += ["", f"_Generated at {payload['generated_at']}._", ""]
    return "\n".join(lines)


def _fmt(value: Any) -> str:
    if isinstance(value, bool):
        return str(value)
    if isinstance(value, (int, float)):
        if value != value or value in (float("inf"), float("-inf")):
            return str(value)
        return f"{value:.4f}"
    return str(value)


def _utc_now_iso() -> str:
    return dt.datetime.now(dt.UTC).isoformat()


def _canonical_sha256(obj: Any) -> str:
    return hashlib.sha256(
        json.dumps(obj, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()


if __name__ == "__main__":
    raise SystemExit(main())

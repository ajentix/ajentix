# ajentix-quant

> Deterministic, market-neutral quant money-making engine.
> **Runtime LLM = 0** (trading is pure deterministic code). AI agents only **build, maintain, research, and supervise** the system.

Money-first. DeFi/venue is an instrument, not an ideology — the engine optimizes risk-adjusted return and is venue-abstracted so CEX / DeFi / CeDeFi venues plug in behind one adapter interface.

Full requirements/vision spec: [`docs/SPEC.md`](docs/SPEC.md) (crystallized from a 25-round deep interview + 13 research passes).

## Core thesis (why this edge exists)

The crown-jewel quant edge in crypto is **market-neutral, not directional**. Cash-and-carry / funding-basis harvest historically clears a Sharpe ~4.84 vs ~0.8 for directional trading, because positive funding reflects a **structural friction** (leveraged longs perpetually overpay), not a fleeting inefficiency.

**v1 strategy:** delta-neutral funding harvest — long spot + short perpetual on the same asset, market-neutral, collecting positive funding. **v1 venue:** Bybit (via `ccxt`) — strong retail funding skew, unified margin, deep liquidity, free historical data.

## Status — Phase 0 (walking skeleton)

This repo is a **walking skeleton**: the core path runs end-to-end on bundled sample data, but strategies/backtest are intentionally minimal. Live trading is **not** implemented.

```
src/ajentix_quant/
  config.py               # pydantic-settings (AQ_* env)
  adapters/               # venue plumbing (uniform) — microstructure stays first-class
    base.py               # VenueAdapter ABC, FundingRate, Candle
    bybit.py              # ccxt-based read-only adapter (lazy import)
  strategies/             # pluggable, deterministic
    base.py               # Strategy ABC, Signal
    funding_harvest.py    # delta-neutral funding harvest signal
  risk/
    engine.py             # dynamic regime-aware leverage, liq-distance, kill-switch, ADL (skeleton)
  backtest/
    metrics.py            # sharpe, sortino, ann_return, max_drawdown
    engine.py             # runnable funding-harvest backtest
  execution/
    paper.py              # paper / dry-run executor (NO live orders)
  data/
    sample.py             # deterministic offline sample funding series
scripts/run_backtest.py   # run the sample backtest
tests/                    # core-path tests
```

## Quickstart

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"          # core + dev; add ".[live]" for ccxt, ".[data]" for pandas/polars
pytest                            # core-path tests
python scripts/run_backtest.py    # runs the funding-harvest backtest on sample data
```

## Risk model (deterministic)

- **Dynamic regime-aware leverage** ("lever up calm, down volatile"): base 2–3x, cap 5x only in low-vol + high-positive-funding regimes, auto-deleverage on vol spikes / funding compression.
- **Portfolio/cross margin (hedge netting)** for capital efficiency + ADL deprioritization.
- **Liquidation-distance floor ≥ 15%**, emergency reserve 20–30%, **funding-reversal forced exit**, ADL-rank monitoring.
- **API keys: trade-only, NEVER withdrawal.**
- Backtests must include negative-funding windows and a flash-crash-style gap stress.

## Validation gate (3 stages)

1. **Backtest** — net of fees + both-way funding + slippage + leverage cost, over a window incl. a negative-funding regime and a gap stress: Sharpe ≥ 1.5, MDD ≤ 5%, net APR ≥ 0.
2. **Paper / tiny live (2–4 weeks)** — stays market-neutral, MDD ≤ 5%, kill-switch/liquidation/ADL never fire.
3. **Small live scale-up** — staged capital increase after (1)+(2) pass.

## Roadmap

- **Phase 0 (this repo):** walking skeleton.
- **Phase 1:** Bybit historical pipeline + full funding-harvest + dynamic leverage + full backtest metrics.
- **Phase 2:** paper + tiny live + live risk engine + monitoring.
- **Phase 3:** stat-arb satellite + Hyperliquid adapter (long-tail funding) + Pendle PT scanner + **options tail-hedge (leverage unlock)**.
- **Phase 4:** CeDeFi cross-venue (HL↔CEX funding spread) + portfolio allocation + capital scale-up.

## Disclaimer

Research/engineering project. Crypto trading carries substantial risk including total loss. "Delta-neutral" is **not** risk-free (funding-regime, basis-compression, liquidation, ADL, exchange-counterparty, gap risk). Nothing here is financial advice.

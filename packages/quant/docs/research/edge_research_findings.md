# Retail Crypto Edge Research — Findings & Scoreboard

**Objective:** find a retail-scale ($500–2000) crypto trading edge that *survives out-of-sample*.
**Discipline:** strict train→test split (≈65/35) on real data, realistic costs, OOS-only verdicts.
A "survivor" requires TEST performance >0 with a sign consistent with TRAIN (no regime sign-flip)
and a sane trade count. In-sample results never count.

## Headline conclusion

After **19 mechanisms** tested OOS, exactly **one structural class survives**: the
**volatility risk premium (VRP) / defined-risk short-vol**. Everything else — every attempt to
harvest a "free" price/funding inefficiency — is beautiful in-sample and **dead out-of-sample**.

The reframe this produces:

> In an efficient market, automatable risk-free inefficiencies are competed down to cost and
> do not persist. What survives OOS is **getting paid to bear risk** (a risk *premium*), not
> finding a free lunch. The VRP persists precisely because someone must bear crash risk.

## Scoreboard

| # | Hypothesis | Class | OOS verdict | Evidence |
| --- | --- | --- | --- | --- |
| 1 | Funding harvest (Bybit/HL majors) | neutral carry | NO_GO | carry < ~16.5bps round-trip cost |
| 2 | Cross-venue alt funding spread (HL vs Binance) | neutral arb | NO_GO | 9 train-clearing names → **0 survive OOS** |
| 3 | Cointegration pair mining (136 pairs) | neutral reversion | NO_GO | train Sharpe 1.3–3.4 → **OOS median 0, mean −2.08** |
| 4 | Pre-specified major pairs (BTC-ETH etc.) | neutral reversion | NO_GO | BTC/ETH train 2.38 → **OOS ~0**; trend pairs −500…−1073% |
| 5 | Liquidation-wick reversion | directional behavioral | INCONCLUSIVE | train +1.75%/73% win but **30 train / 0 test events** (rare, unverifiable) |
| 6 | Funding time-of-day seasonality | gating | NO | funding flat across 8h slots |
| 7 | Basis carry (majors, short perp + long spot) | neutral carry | NO | BTC funding APR train 5.3% → **test 0.1%** (decayed) |
| 8 | Cross-sectional funding factor (long low / short high) | neutral long-short | NO_GO | TRAIN/TEST Sharpe both negative; turnover-killed |
| 9 | Time-series momentum (majors, hourly) | directional | NO | mostly negative both halves |
| 10 | Cross-sectional momentum | directional long-short | FLIP | train neg ↔ test pos (regime sign-flip) |
| 11 | Upbit↔Bithumb spot spread (Korea) | arb | NO | spread 0.0–0.1% < fees; locally arbed |
| 12 | USDT/KRW (kimchi) premium reversion | segmented market | NO_GO | train +0.45%/5d (76% win) → **test +0.08% (44% win)**; premium ~0% now |
| 13 | Cross-sectional short-term reversal | directional long-short | FLIP | train pos ↔ test neg |
| 14 | BTC lead-lag (alts follow) | directional | NO | −68…−490% (cost-destroyed) both halves |
| 15 | ETH/BTC ratio reversion (structural) | neutral reversion | NO_GO | train 2.4–3.5 → **test 0 (0 trades)** |
| 16 | Session / time-of-day return effect | directional | NO | OOS good-hours negative |
| 17 | Funding-extreme directional fade | directional behavioral | NO | noisy coin-flip (win 44–51%) |
| 18 | Open-interest signals | microstructure | INFEASIBLE | only ~20d OI history available (free) |
| 19 | **Volatility risk premium (VRP) / short-vol** | **risk premium** | **OOS-SURVIVOR** | IV>RV persistent: BTC win 82%→73%, ETH 77%→76% (train→test) |

## VRP — the survivor, with honest caveats

**Why it survives (and the others don't):** VRP is a *risk premium*, not an inefficiency.
Implied vol systematically exceeds realized vol (BTC IV 43.7% vs RV 29.6% ≈ +14 vol-pts; ETH
IV 67% vs RV 48% ≈ +19 vp) because option sellers must be paid for bearing crash risk. This
does not get arbed away.

**Feasibility at $500–2000 (measured on Deribit):**
- ETH options: 1 contract = 1 ETH (~$1700 notional), `minAmount=1` → accessible. Defined-risk
  spreads (credit spread / iron condor) cap max loss to the strike width in dollars.
- BTC options: `minAmount=0.1` BTC (~$6k) → borderline too large for a small account.
- ATM ETH option IV bid/ask spread ≈ **2 vol points** (≈1 vp to cross) — tight. Round-trip
  execution cost ~2–4 vp, which is **dwarfed by the 14–19 vp premium**. Execution is not the killer.

**The catch — this is insurance-selling, not a free lunch:**
- **Naked short variance is net-NEGATIVE OOS** (BTC test Sharpe −1.24): P&L is convex in realized
  vol (∝ RV²), so a single crash day (worst proxy day ≈ −920 "vega%") swamps months of premium.
  This is "picking up pennies in front of a steamroller," and was directly confirmed.
- **Tail-capped (defined-risk) short-vol flips OOS-positive** in the proxy backtest, but the
  reported Sharpe magnitudes (10–17) are **model-inflated** (noisy 1-day realized-vol estimate;
  unmodeled historical option prices). A realistic VRP book runs Sharpe ~0.8–1.5 with occasional
  double-digit drawdowns.

**Verdict:** VRP/defined-risk short-vol is the first and only OOS-persistent, retail-feasible,
net-positive-after-cost edge found. It is a genuine edge **conditional on accepting tail risk**.

## Recommended next phase (not done here)

A proper VRP feasibility build, with full pre-registration discipline, must use **actual historical
Deribit option-chain prices** (not just the DVOL index proxy), model real bid/ask, min ticket
sizes, defined-risk structures (credit spreads / iron condors), $500–2000 position sizing, and the
drawdown profile **including the worst historical vol-expansion days**. Only then is the
risk-adjusted return trustworthy enough to authorize capital.

## What is conclusively exhausted

The free, systematic, price-and-funding **inefficiency** space (neutral arb, carry, cointegration,
momentum/reversal, factor, session, segmented-market spreads). 18 independent attempts, all
in-sample-attractive and OOS-dead/flip/infeasible. The remaining untested frontiers (deep on-chain,
L2 order-flow, long options-chain history, structured event calendars) all sit behind a **data/
infra/ops barrier — which is itself the source of any edge there**, consistent with the headline
conclusion.

# USD-consistent VRP skew measurement — findings

Status: **NO_GO confirmed (net-negative), and now measured rather than blocked.**

This closes the single open item from
[`full_frozen_run_findings.md`](full_frozen_run_findings.md): the frozen VRP strategy's
ETH-credit-vs-USD-width unit bug made the official walk-forward select **zero** structures in all
seven folds, so the OTM put-skew credit-spread edge's clean fold-level economics were *never
measured* ("Not concluded"). They are now measured on USD-consistent units, and the edge is
**net-negative over the full F1–F7 window**: the unit bug was a real bug, but fixing it does **not**
rescue the edge. No capital is authorized; this is a measurement, not a GO.

## What was built (frozen code untouched)

- `src/ajentix_quant/options/usd_projection.py` — projects a reconstructed snapshot's ETH-denominated
  premia back to USD (premium and labels move together, by the exact per-snapshot `ETH_USD` rate the
  reconstruction used). Per-contract (`min_lot=1`) so the measurement reports scale-free economics.
- `src/ajentix_quant/strategies/vrp_defined_risk_usd_eval.py` — a non-authorizing sibling of the
  frozen strategy. It reuses the frozen leg-selection helpers and the **identical** search space and
  `credit/width` entry bar (0.15/0.20), differing only in requiring the USD settlement profile, so on
  USD-projected legs the gate is finally dimensionally consistent.
- `src/ajentix_quant/research/vrp_usd_eval.py` + `scripts/run_vrp_skew_usd_eval.py` — the measurement
  harness and CLI.

The frozen strategy (`vrp_defined_risk.py`) and the pre-registration are **not modified**.

## Why a separate measurement harness

The committed breakeven / walk-forward path is an *authorization* gate. On reconstructed (`fixture`)
source quality it returns `branch_decision=INCONCLUSIVE` / `NON_AUTHORIZING_FIXTURE` and selects **no**
param keys by design — confirming the prior conclusion that a capital GO is structurally impossible
from this data quality. That gate therefore cannot be reused to *measure* economics, so the harness
characterizes the raw edge directly and stays explicitly non-authorizing.

## Method

For each held-out test fold, every structure the USD-eval strategy emits (full grid, every snapshot)
is entered one contract and held to **European settlement at the real ETH index price observed at the
structure's expiry** (`index_path.csv`), priced through the committed VRP engine (taker fees included;
reconstructed marks carry ~zero crossing). Structures whose taker-executable credit is ≤ 0 or whose
expiry is outside index coverage are excluded and counted. A flat effective-spread haircut from the
full real-data run (p50 $4 / p75 $7 per structure, upper end of the measured $2.5–4 / $4–7 bands)
gives the net band. "Return on risk" = PnL / total capital at risk (scale-free).

This is an **enter-all** characterization (every signal, one unit) — it answers "is there a clean edge
at all", not "what would an optimally sized, train-selected book return". Fold Sharpe over 7 windows
is a small, weak sample.

## Results (real Deribit history, 2024-09 → 2026-06, 21,350 chains)

Signal: **`NET_NEGATIVE_AFTER_SPREAD`**. 215,289 entries across the 7 folds.

| fold | entries | gross $ | net p50 $ | ror gross | ror net p50 |
| --- | ---: | ---: | ---: | ---: | ---: |
| F1 | 32,364 | +1,441,902 | +1,312,446 | +0.289 | +0.263 |
| F2 | 38,220 | +1,439,489 | +1,286,609 | +0.320 | +0.286 |
| F3 | 41,346 | −513,856 | −679,240 | −0.106 | −0.140 |
| F4 | 38,889 | −2,331,651 | −2,487,207 | −0.503 | −0.536 |
| F5 | 29,844 | −857,963 | −977,339 | −0.245 | −0.279 |
| F6 | 18,462 | −390,276 | −464,124 | −0.214 | −0.254 |
| F7 | 16,164 | +235,837 | +171,181 | +0.156 | +0.113 |

Aggregate: gross **−$976,519**, net p50 **−$1,837,675**, net p75 −$2,483,542. Fold Sharpe
(return-on-risk) gross **−0.33**, net p50 **−0.60** (bar 1.5).

## Interpretation

- The edge collects premium in calm folds (F1, F2, F7 positive) but **gives it all back and more in
  the drawdown folds** (F3–F6, with F4 at −0.50 return-on-risk). The OTM put-skew premium is thin
  (~10–17% of width ungated; ~0.21–0.32 mean credit/width once the 0.15 bar selects) and **fat-tailed**:
  capped-loss spreads still lose multiples of the credit when ETH sells off.
- Net of even a modest per-structure spread it is decisively negative, and gross is already negative
  over the full window. The earlier diagnostic call ("marginal-to-negative after costs") is now a
  measured, fold-level result, not an extrapolation.
- The frozen unit bug was genuine (it zeroed selection), but **correcting it does not produce an
  authorizing edge**. The terminal `NO_GO` stands on economics, not on the bug.

## Caveats

- Enter-all, one-contract, no train-causal position sizing or book construction; a train-selected
  subset could differ, but the full-population sign and the drawdown-fold losses are unambiguous.
- Flat effective-spread haircut (documented constants), not a fresh per-structure calibration.
- 7 folds is a weak Sharpe sample. Non-authorizing throughout; reconstructed + effective-spread
  source quality precludes a capital GO regardless.

## Reproduce

```bash
python scripts/run_vrp_skew_usd_eval.py
# reads data/cache/full_recon (+ data/cache/full_combined/.../index_path.csv); writes
# reports/vrp_skew_usd_eval.{json,md}. Network-free; the 1.2GB trades.jsonl is not read.
```

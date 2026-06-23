# Full frozen-window real-data run — findings

Status: **NO_GO (non-authorizing, capital_go_allowed=false)** — terminal honest result of running
the entire free-data-native VRP pipeline end-to-end on real Deribit-history data over the full
frozen window. 100% real observed data; no fabrication.

## What was run (all real, network-free after collection)

| Stage | Result |
|---|---|
| Collection (5 chunks, merged) | **5,086,803** deduped real ETH option trades; warmup from 2024-08-01, coverage **2024-09-01 → 2026-06-01**; `cache_fabricated=false` |
| Reconstruction (frozen 8h grid) | **1,915** snapshots, 21,350 chains, 889,779 legs; `cache_fabricated=false` |
| Walk-forward | real frozen folds **F1–F7** (6-month train / 2–3-month test each) |
| Effective-spread cost basis | **64,223** real trade-vs-mark samples across 21 months |

Tooling built this session to make the full scale feasible:
`scripts/merge_vrp_free_history_caches.py` (chunked-collection merge), bisect-windowed
reconstruction lookups (hours → ~9.7 min), and `--use-frozen-folds` on the skew-verdict runner.

## Official verdict: NO_GO — and the decisive root cause

Across **all 7 folds the official walk-forward selected ZERO structures** (`entries=0`,
`selected_structure_count=0`, `NON_TRAIN_CLEARING_SELECTION` + `INSUFFICIENT_HELDOUT_ENTRIES`).
The economics were therefore never reached — not because the edge was tested and failed, but
because **no structure cleared the entry gate**.

Root cause is a **unit incompatibility in the frozen, immutable strategy**
(`src/ajentix_quant/strategies/vrp_defined_risk.py`, first committed in `d0d48b3`):

- `width = abs(short.strike - long.strike)` is in **USD** (Deribit ETH strikes are USD).
- `net_credit = short.bid_price - long.ask_price` is in **ETH** — the frozen plan sets
  `PLAN_SETTLEMENT.premium_currency = "ETH"`, and the reconstruction emits ETH-denominated
  model prices (`greeks.value / index_price`) to match Deribit's native quote convention.
- The entry gate `net_credit / width < min_credit_to_width` (bar = 0.15–0.20) therefore
  compares **ETH credit against USD width with no conversion**: e.g. 0.0085 ETH / 200 USD ≈
  0.00004 ≪ 0.15 → every structure is rejected.

The strategy does USD-native credit/width math while requiring an ETH premium currency and
exposing **no conversion hook** in the gate. This latent bug was never exercised by the first
VRP build, which terminated at `INCONCLUSIVE_DATA_BLOCKER` before reaching real data. The full
real run is what surfaces it. The strategy and the pre-registration are frozen and were not
modified; the pipeline **correctly fails closed** rather than silently emitting a number.

## Honest economic signal (diagnostic, non-gating)

The non-gating diagnostics + direct measurement still characterize the edge on real data:

- **Skew credit vs width (USD-converted):** the OTM put-credit-spread net credit is roughly
  **10–17% of width** in USD terms (computed from diagnostic candidates × real entry index
  price) — **borderline against the frozen 0.15 entry bar.** Some tenors (e.g. ~54 DTE,
  100-wide) clear 0.15; most 19–26 DTE structures land ~0.10–0.12.
- **Costs are absorbable:** effective round-trip structure spread (trade-vs-mark proxy)
  **p50 ≈ $2.5–4 / p75 ≈ $4–7** per structure across 21 months (64,223 samples). Small vs the
  ~$10–23 credit collected on $100–200 spreads at $1,000 capital.
- **Stress is `not_applicable`:** the free trade-derived index path is event-timestamped and is
  not the regular hourly grid the frozen exact-underlying stress requires; running it would need
  separately-gated hourly resampling (a synthesis step this pipeline does not perform). This is a
  structural basis limitation, not a data-quantity shortfall.

This is consistent with the earlier finding: **ATM variance premium is dead; the real premium is
the OTM put-skew, which is fat-tailed and requires a hard defined-risk cap.** The skew credit is
thin (≈10–17% of width) and borderline against a 15% entry bar.

## What is and is not concluded

- **Concluded (real, full-scale):** the pipeline runs end-to-end on 5.09M real trades; costs are
  absorbable; the skew credit is thin/borderline vs the frozen entry bar; ATM variance is dead.
- **Not concluded:** a clean fold-level walk-forward Sharpe/PnL for the skew edge. It is blocked
  by the frozen strategy's ETH/USD unit incompatibility, which cannot be fixed without either
  modifying frozen code (out of bounds) or emitting a USD-priced-but-ETH-labelled leg
  (misrepresentation). Measuring it cleanly requires a separately-designed, explicitly
  non-authorizing **USD-consistent evaluation variant** (e.g. a reconstruction mode that carries
  the real BS USD model value as the leg credit basis, paired with the USD width), gated on its
  own merits. Capital GO remains structurally impossible (reconstructed/effective-spread
  source-quality).

## Reproduce

```bash
R_RAW=data/cache/full_combined ; R_REC=data/cache/full_recon
# collection driver (5 chunks) + merge
bash data/cache/full_chunks/run_chunks.sh
python scripts/merge_vrp_free_history_caches.py \
  --chunk-root data/cache/full_chunks/c0 --chunk-root data/cache/full_chunks/c1 \
  --chunk-root data/cache/full_chunks/c2 --chunk-root data/cache/full_chunks/c3 \
  --chunk-root data/cache/full_chunks/c4 --out-root "$R_RAW" --coverage-from 2024-09-01T00:00:00Z
python scripts/reconstruct_vrp_free_chain.py --raw-source-root "$R_RAW" --reconstructed-cache-root "$R_REC"
python scripts/run_vrp_free_skew_verdict.py --raw-source-root "$R_RAW" \
  --reconstructed-cache-root "$R_REC" --use-frozen-folds
```

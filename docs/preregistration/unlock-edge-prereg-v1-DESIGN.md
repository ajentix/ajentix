# Token-Unlock Supply-Shock Edge — Pre-Registration (DESIGN, v1)

> Frozen design, pending gate (architect + critic). On approval -> immutable hashed module
> src/ajentix_quant/research/unlock_preregistration.py + content-hashed artifact, like the VRP prereg.
> Post-result edits to thresholds/grid/windows are INVALID (confirmation bias).

## 0. Why this candidate (honest prior)

Every prior inefficiency died OOS (18 attempts: momentum/arb/carry/reversal/factor). The one
surviving class is risk premia — getting paid to bear risk. A large scheduled unlock forces
insiders/VCs to sell a step-change in supply into finite liquidity; being short into it (demanding
the discount that clears the supply) is a candidate liquidity/supply-shock premium, not a free lunch.

Honest prior: GUARDED. Unlocks are public and scheduled, so the efficient-market view says they are
priced in (DefiLlama publishes a "7d post-unlock impact" column; observed impacts are mixed and
high-variance). This pre-registration settles it OOS with the same kill-fast discipline that killed
the other 18. If it flips or is <=0 net OOS, it is DEAD — no resurrection by tweaking tau or window.

## 1. Hypothesis (falsifiable)

- H1: around a large scheduled unlock, the token earns NEGATIVE beta-adjusted (abnormal) return over
  a defined event window, net of realistic retail costs, PERSISTENTLY out-of-sample.
- H0 (null): mean net abnormal return around large unlocks ~= 0 (priced-in / efficient).
- Tradeable form: beta-neutral short of the token, hedge leg = BTC, held over the event window.

Abnormal return r_abn = r_token - beta * r_btc. beta is estimated CAUSALLY from the trailing daily
returns [T-61, T-1] (OLS slope, clipped [0,3]). Isolates token-specific supply effect from market
beta; keeps the book market-neutral. All offsets are trade days on the token's perp.

## 2. Data sources (frozen, free, reproducible)

- Unlock schedules: DefiLlama emissions-adapters GitHub repo, pinned at a recorded commit hash.
  manualCliff/manualLinear/manualStep primitives re-implemented in Python and unit-tested against
  the repo's own expected fixtures.
- Prices (daily OHLCV): free ccxt (Binance, fallback Bybit) USDT-perp + BTCUSDT-perp; request params
  + as-of date recorded; cached + manifest-hashed like the VRP raw cache.
- Funding rates: free ccxt funding history per perp; cached + hashed.
- Float / circulating supply: derived deterministically from the pinned emissions schedule.

No paid API. Collection is the only network step; all analysis is offline, deterministic,
stdlib+numpy only, cache_fabricated=false asserted (no synthetic events/prices).

## 3. Event definition (frozen)

An unlock event for token k at date T qualifies iff ALL hold:
1. schedule unlocks >= tau of circulating float within [T, T+2] days (captures cliffs + large
   step/linear bursts; avoids double-counting trickle linear vesting).
2. unlocked USD value at T >= $250k (filters dust unlocks that move nothing).
3. a USDT-perp for k existed and traded >= 90 days before T (beta estimable; tradeable).
4. T inside the study window (sec 6) and >= 7 days from another qualifying event for the same token
   (de-overlapped; earlier/larger wins).

Cliff unlocks are preferred and flagged (fixed at TGE, rarely revised -> minimal look-ahead).
Linear/step bursts are included but tagged revision_risk=true and reported separately.

## 4. Strategy (frozen)

- Entry: short k-perp at the daily close of day T+a; simultaneously long BTC-perp sized to
  beta*notional (beta-neutral). One unit of risk per event.
- Exit: flat at the daily close of day T+a+h.
- Sizing: per-event defined risk = per_event_risk_pct of equity; hard stop at stop_pct adverse move
  on the SHORT leg so a single squeeze cannot exceed the defined risk (the VRP lesson: cap the tail).
- Primary equity $1000; equity grid {500,1000,2000} for sensitivity (reporting only).

## 5. Parameter grid (frozen, bounded trial budget = 8)

- tau (unlock % of float): {0.025, 0.05}
- entry offset a (days vs unlock): {-1, 0}
- hold horizon h (days): {5, 10}
- hedge: {beta_neutral_btc}   (directional/no-hedge is reported as a diagnostic only, NON-gating)
=> 2 x 2 x 2 x 1 = 8 combinations. TRIAL BUDGET = 8, frozen. No combo added post-hoc.
A single primary combo is pre-designated for the headline verdict: tau=0.05, a=0, h=10, beta_neutral.
The other 7 are robustness; the verdict cannot be cherry-picked from the best combo.

## 6. Study window & folds (frozen, chronological OOS)

- Study window: events with T in [2023-01-01, 2026-06-01).
- Split by EVENT date, chronological, no shuffling:
  - TRAIN: events T < 2025-03-01
  - TEST (held out): 2025-03-01 <= T < 2026-06-01
- Fold-causal: beta, pooling stats, and grid selection use ONLY data with timestamp < fold.train_end.
  TEST events never seen during selection. All-data stats are diagnostic only, never improve verdict.
- Min events: >= 40 TRAIN and >= 25 TEST qualifying events for the headline combo, else
  INCONCLUSIVE_INSUFFICIENT_EVENTS (mirrors VRP insufficient-coverage fail-closed).

## 7. Cost model (frozen, realistic retail @ $1000)

Per event, both legs (short k-perp + long BTC-perp), entry+exit:
- taker fee 0.05% per leg per side.
- slippage 0.10% per leg per side baseline; 0.20% if token perp daily volume < $20M.
- funding: realized over hold, summed from real ccxt funding, signed (not assumed favorable).
- A trade whose modeled round-trip cost exceeds its gross abnormal return is a real loss, not
  excluded. No cost waved to flatter the edge (funding-harvest lesson).

## 8. Verdict bars (frozen)

Headline combo (tau=0.05, a=0, h=10, beta_neutral), TEST set, net of all costs:
- PROMISING (non-authorizing): TEST mean net abnormal return per event > 0 AND TRAIN/TEST same sign
  AND hit-rate > 52% AND Newey-West t-stat >= 2.0 on per-event net returns AND >= 25 TEST events.
- NO_GO: TEST mean <= 0, OR TRAIN/TEST sign flip, OR t-stat < 2.0. (Expected if unlocks are priced
  in. NO_GO is honest and final.)
- INCONCLUSIVE_INSUFFICIENT_EVENTS: event-count minimums not met.
- Capital GO is NOT reachable from this artifact: PROMISING only authorizes a tiny live forward-
  confirmation stage (3-stage gate), never direct capital.

Robustness (does NOT override headline): of the 8 combos, >= 6 must share the headline TEST sign,
else downgrade to NO_GO (a result in only 1-2 combos is overfit noise).

## 9. Anti-overfit governance (frozen, mirrors VRP)

- Trial budget = 8 combos, hard-capped. No post-hoc combo.
- Post-result change to tau / window / cost / bar = INVALID artifact.
- Fold-causal selection only; TEST is write-once.
- Survivorship disclosed: universe = tokens surviving in the pinned emissions repo with a live perp;
  delisted/dead tokens absent -> survivor bias, reported as caveat, not hidden.
- Schedule-revision look-ahead: cliffs preferred; revision_risk events reported separately; a
  cliffs-only sensitivity run must not flip the headline sign/verdict, else downgrade.
- No fabrication: every event + price + funding is a real observed datum; missing data fails closed.

## 10. Crystallization plan (on gate approval)

1. src/ajentix_quant/research/unlock_preregistration.py: frozen PLAN_* constants + content hash.
2. scripts/create_unlock_preregistration.py: emit immutable hashed artifact to docs/preregistration/.
3. Then (separately gated): emissions-adapter parser, ccxt collectors, event builder, walk-forward
   evaluator, final verdict mapper. Execution does NOT begin until this design is approved.

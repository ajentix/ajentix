# Token-Unlock Supply-Shock Edge — Pre-Registration (DESIGN, v2)

> Supersedes v1 (unlock-edge-prereg-v1-DESIGN.md). v1 was gated: architect BLOCK + critic REQUEST-CHANGES.
> v2 pins every operational knob to a concrete value and fixes the 4 HIGH trust blockers. Pending re-gate.
> On re-gate CLEAR -> crystallize to src/ajentix_quant/research/unlock_preregistration.py + hashed artifact.
> Post-result edits to ANY constant below = INVALID (confirmation bias). All times UTC.

## Gate fixes applied (v1 -> v2)
- HIGH-1 point-in-time: per-event schedule truth from emissions-adapters GIT HISTORY (commit dated < T), not a single current pin.
- HIGH-2 survivorship: free data cannot reconstruct delisted perps -> max outcome hard-capped PROMISING_SURVIVOR_UNIVERSE_ONLY (non-authorizing).
- HIGH-3 cost/stop: daily high/low path-aware stop + stressed slippage + frozen sizing/stop values.
- HIGH-4 inference: stationary block-bootstrap lower bound + independent month-cluster minimum; Newey-West demoted to diagnostic.
- sign-convention bug fixed (verdict gates on short-leg NET pnl, not raw abnormal return).
- all 22 critic under-specified knobs pinned below.

## 1. Hypothesis & sign convention (frozen)

- H1: short-leg NET return around a large scheduled unlock is POSITIVE OOS, net of all costs
  (equivalently: the token's beta-adjusted abnormal return is negative and large enough to beat costs).
- H0: short-leg net return ~= 0 after costs (priced-in / efficient).
- Formulas (frozen):
  - r = simple daily close-to-close return.
  - token_abnormal = r_token - beta * r_btc   (beta per sec 5).
  - short_gross = -token_abnormal             (we short token, long beta*notional BTC).
  - short_net = short_gross - cost_return      (cost_return per sec 7, in return units on token notional).
  - VERDICT GATES ON short_net. token_abnormal<0 is reported as a diagnostic only.

## 2. Data sources (frozen, free, point-in-time)

- Unlock schedules: github.com/DefiLlama/emissions-adapters. Universe enumeration uses HEAD pinned at
  a recorded commit SHA <UNIVERSE_SHA> (filled at crystallization). PER-EVENT schedule truth for token k
  at unlock date T uses the adapter file protocols/<k>.ts content from the LATEST commit whose committer
  date <= T - 1 day (schedule must have been public before entry). If no pre-T commit contains the
  (date, amount) of the event, the event is EXCLUDED from the headline and tagged not_point_in_time
  (diagnostic only). Implementation: local clone; per-event `git rev-list -1 --before=<T-1d> master` on
  the file; re-implemented manualCliff/manualLinear/manualStep, unit-tested vs the repo's own fixtures.
- Prices/funding/volume: ccxt daily OHLCV '1d' (UTC 00:00 boundary) + funding history + quote volume.
  Venue: Binance USDT-perp primary; if token has no Binance USDT-perp, Bybit USDT-perp. BTC hedge ALWAYS
  on the token's venue. All cached + manifest-hashed (sha256) like the VRP raw cache; request params +
  as-of date recorded. No paid API. cache_fabricated=false asserted.
- Float: circulating float at T-1 = sum over all schedule sections of cumulative unlocked tokens at T-1,
  per the point-in-time adapter. Denominator for tau uses float at T-1 close.

## 3. Event definition (frozen, fully pinned)

For token k, scan days d ascending (UTC). incremental_unlock(d) = unlocked(d) - unlocked(d-1) tokens
from the point-in-time adapter. A candidate forms at T = first day of a rolling [d, d+2] (3 UTC days,
inclusive) where: window_tokens = sum incremental_unlock over [T,T+2]; window_pct = window_tokens /
float(T-1); window_usd = window_tokens * close_price(T-1, token venue). Qualifies iff ALL:
1. window_pct >= tau (sec 5 grid).
2. window_usd >= 250000 USD.
3. token USDT-perp existed & traded >= 90 distinct daily bars before T on its venue.
4. all required bars present (sec 6) and the event is point-in-time (sec 2).
De-overlap: sort qualifying candidates by T asc; reject one within 7 calendar days of an already-accepted
event for the SAME token. Tie at equal T: larger window_pct wins; then larger window_usd; then
lexicographically smaller market symbol. classification: cliff if >=80% of window_tokens land on a single
day, else step/linear (tagged revision_risk per source type).

## 4. Strategy & sizing (frozen, executable)

- Entry: at daily close of day T+a, SHORT token-perp with notional = 0.20 * equity; simultaneously LONG
  BTC-perp with notional = clip(beta,0,3) * (0.20 * equity). One event = one unit.
- Exit: daily close of day T+a+h, OR earlier stop.
- Hard stop (path-aware, daily): for the SHORT, adverse = price UP. If on any day in (entry, exit] the
  token daily HIGH >= entry_price * (1 + stop_pct), stop fills that day at entry_price*(1+stop_pct) plus
  0.30% stressed slippage; BTC hedge unwound same day at that day's close. stop_pct = 0.25.
  => max loss per event ~= 0.20*0.25 = 5% of equity on the short leg (the defined risk), before hedge.
- Funding accrues on both legs until stop/exit. Primary equity $1000; grid {500,1000,2000} reporting only.
- No leverage beyond what 0.20*equity notional per leg implies; aggregate concurrent defined risk is
  reported but the headline is per-event (events are 7d+ de-overlapped per token; cross-token concurrency
  is a reported diagnostic, not a sizing constraint in the headline).

## 5. Beta & parameter grid (frozen, trial budget = 8)

- beta = OLS slope (with intercept) of r_token on r_btc over the daily returns strictly BEFORE the entry
  close: window [entry_day-61, entry_day-1] (so a=-1 uses returns before the a=-1 entry; a=0 before T).
  Require >= 40 valid return obs and positive prices else EXCLUDE event. Slope clipped to [0,3] post-fit.
- Grid: tau in {0.025, 0.05}; a in {-1, 0}; h in {5, 10}; hedge {beta_neutral_btc}. = 8 combos, hard cap.
- HEADLINE combo (pre-designated, the ONLY verdict driver): tau=0.05, a=0, h=10, beta_neutral_btc.
- The other 7 combos are robustness ONLY. NO grid/threshold is selected from data (remove "selection";
  there is no train-time tuning — the headline is fixed a priori). All event-builder constants above are
  frozen, not tunable.

## 6. Study window, folds, inference (frozen)

- Study window: events with T in [2023-01-01, 2026-06-01).
- Chronological split by event date: TRAIN T < 2025-03-01; TEST 2025-03-01 <= T < 2026-06-01. No shuffle.
- Everything is causal by construction (fixed headline, causal beta, point-in-time schedule). TRAIN is used
  only to check sign consistency; TEST is the held-out verdict set and is write-once.
- Primary inference (TEST, headline combo, short_net per event):
  - stationary block bootstrap, block unit = ISO calendar MONTH of the event, B = 10000 resamples,
    one-sided 95% lower confidence bound on the mean short_net.
  - independent clusters = distinct ISO months containing >=1 TEST event; require >= 8 such clusters.
  - Newey-West t-stat is computed and REPORTED but is DIAGNOSTIC ONLY (not a gate).
- Minimum events: >= 40 TRAIN and >= 25 TEST headline events AND >= 8 TEST month-clusters, else
  INCONCLUSIVE_INSUFFICIENT_EVENTS.

## 7. Cost model (frozen, both legs, return units)

Per event, BOTH legs (short token + long BTC), entry+exit (+stop fill):
- taker fee 0.05% per leg per side.
- slippage 0.10% per leg per side baseline; 0.20% on token leg if its 30d-median daily quote volume
  before T < $20M; 0.30% on any stop fill.
- funding: realized over hold, summed from real ccxt funding intervals fully inside [entry, exit/stop],
  signed (short token receives + / pays - ; long BTC opposite). No interval assumed favorable.
- cost_return = (total fees + slippage + funding pnl, both legs) / (token short notional). A trade whose
  costs exceed gross is a real loss, never excluded. Missing volume/funding/stop-path -> event excluded
  (fail closed), counted in a diagnostic exclusion ledger.

## 8. Verdict bars (frozen, every branch explicit)

Inputs: TEST headline-combo per-event short_net (after all costs). win = short_net > 0.
- PROMISING_SURVIVOR_UNIVERSE_ONLY (max reachable; NON-authorizing) iff ALL:
  a. TEST mean short_net > 0;
  b. TRAIN mean short_net > 0 (same sign, no flip);
  c. hit-rate > 52% on TEST;
  d. bootstrap one-sided 95% lower bound on TEST mean short_net > 0 (sec 6);
  e. >= 25 TEST events AND >= 8 TEST month-clusters;
  f. robustness: >= 6 of 8 combos have TEST mean short_net > 0 (sign agreement);
  g. cliffs-only sensitivity (cliff events only) does NOT flip TEST sign.
- NO_GO if any of (a)-(d),(f),(g) fails its positive condition (TEST<=0, sign flip, hit<=52%,
  bootstrap LB<=0, <6/8 agreement, cliffs-only flip) while event minimums (e) ARE met.
- INCONCLUSIVE_INSUFFICIENT_EVENTS if (e) not met, or any required statistic is non-finite.
- INVALID_PREREGISTRATION on hash/lineage drift (sec 9).
- DATA_FAIL_CLOSED if required manifests/inputs missing at run time.
Capital GO is UNREACHABLE from this artifact. PROMISING authorizes only a tiny live forward-confirmation
(3-stage gate), never direct capital. Every terminal outcome emits an explicit reason_code.

## 9. Anti-overfit governance & lineage (frozen, mirrors VRP)

- Trial budget = 8 combos hard-capped; headline pre-designated; all event-builder/cost/inference
  constants frozen as PLAN_* (no implicit knobs). No data-driven selection anywhere.
- Post-result change to ANY constant = INVALID artifact. TEST write-once; TRAIN only checks sign.
- Survivorship: universe = tokens in pinned emissions HEAD with a CURRENT venue perp; later-delisted
  names absent; unfixable on free data -> max outcome hard-capped PROMISING_SURVIVOR_UNIVERSE_ONLY.
- Point-in-time: per-event schedule from pre-T commit (sec 2); non-point-in-time events diagnostic only.
- Lineage (verify-time drift -> INVALID): artifact records emissions UNIVERSE_SHA, per-event source
  commit SHAs, primitive-test hashes, price/funding/volume cache manifest sha256s, ccxt request params,
  venue map, and the PLAN_* content hash. Mirrors vrp_free_preregistration verification.
- No fabrication: every event/price/funding/volume is a real observed datum; missing -> fail closed.

## 10. Crystallization plan (only after re-gate CLEAR)

1. src/ajentix_quant/research/unlock_preregistration.py: frozen PLAN_* constants + content hash +
   verifier (mirrors vrp_free_preregistration).
2. scripts/create_unlock_preregistration.py: emit immutable hashed artifact to docs/preregistration/.
3. Then (separately gated, in order): emissions git-history parser + primitive unit tests; ccxt
   collectors (OHLCV/funding/volume, manifest-hashed); point-in-time event builder; path-aware backtest
   (sizing/stop/cost/funding); block-bootstrap inference; final verdict mapper with the sec 8 reason codes.
   No backtest runs until this design is re-gated CLEAR.

# VRP free-data feasibility status

## Current build verdict

`INCONCLUSIVE` pending real-data collection. No capital is authorized.

The delta-neutral funding-harvest edge is dead out-of-sample for the current small-capital Bybit framing: observed funding was too small relative to round-trip costs. The surviving candidate is ETH Deribit defined-risk short-vol / VRP credit spreads, but only as a bounded research candidate.

## What is built

The free-data-native feasibility methodology is implemented as a deterministic, anti-gameable, non-authorizing research path:

- immutable VRP-free governance and pre-calibration freeze;
- free Deribit-history collector/cache for real historical trades and index path;
- causal IV reconstruction from real trade IV into reconstructed option chains;
- Tardis-free options-chain sample calibration for spread cost budgets;
- cost-budget gating with fail-closed sparse-bin behavior;
- immutable freeze / pre-registration verification;
- TRAIN-only breakeven, walk-forward, stress economics, and final verdict mapping;
- final vocabulary restricted to `NO_GO`, `PROMISING_PENDING_REAL_SPREAD`, and `INCONCLUSIVE`.

This methodology is deliberately runtime-LLM-free, deterministic, read-only, and network-free in tests. It fabricates neither quotes nor positive outcomes.

## Hard limitation

A capital `GO` is structurally impossible from this evidence class. Continuous historical Deribit bid/ask is absent for free. Reconstructed chains are not venue quotes. Calibrated spreads are sample-based, not a continuous historical spread tape. Therefore even a perfect positive reconstructed result is capped at `PROMISING_PENDING_REAL_SPREAD` and authorizes only a continuous real-spread confirmation, not capital.

Any capital step requires a real continuous-spread confirmation from a paid Tardis trial, a Deribit partnership/free tier, or an equivalent continuous bid/ask source.

## Terminal empirical work remaining

The terminal empirical verdict requires running the collectors locally on real FREE Deribit-history plus Tardis-free samples. The collectors are intentionally refused under CI and should be run locally with `env -u CI` when real data files are available.

Until those real-data upstream reports exist and chain through the final mapper, the honest observed outcome remains `INCONCLUSIVE` pending real-data collection, never a forced `GO`.

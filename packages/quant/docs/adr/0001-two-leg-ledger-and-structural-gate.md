# ADR-0001: Two-leg ledger, structural gate, and Edge Verdict report

## Status

Accepted.

## Decision

Use A2: a deterministic Bybit-calibrated two-leg ledger, a structural-invariant CI gate, and a non-gating Stage-1 Edge Verdict performance report.

The ledger models long spot plus short linear perp with explicit fees, funding, slippage, leverage cost, venue margin health checks, liquidation/deleverage events, and net-delta accounting. CI promotes only structural invariants through `scripts/run_stage1_gate.py`. The Edge Verdict is recorded as JSON/Markdown for human review and may return GO, NO-GO, or INCONCLUSIVE, but its performance thresholds are not a CI hard gate.

## Drivers

- Phase 1 needs honest delta-neutral economics, not a funding-only proxy.
- CI must remain deterministic, offline, and safe to run without venue credentials or network.
- Fixture and synthetic data are useful for wiring and invariant tests, but cannot justify a real performance GO.
- Train/test discipline must prevent strategy-parameter selection from reading held-out rows.

## Alternatives considered

- A1 funding-only backtest: simpler, but omits spot/perp ledger costs, basis, slippage, health, liquidation, and net-delta behavior.
- B1 real-only hard gate: honest about provenance, but unsuitable for offline CI and unavailable when the venue cache is absent.
- B2 synthetic hard gate: deterministic, but would create false confidence by allowing fixture/synthetic performance to promote the strategy.

## Why chosen

A2 separates safety from evidence quality. Structural invariants can be enforced offline in CI, while performance evidence is emitted as a report whose GO path requires real venue source quality and an out-of-sample test window. This preserves deterministic CI while preventing fabricated performance promotion.

## Consequences

- A GO is impossible unless every required stream is `source_quality=VENUE`, test-window thresholds are met, and no train/test collapse is detected.
- Missing real venue cache or fixture/proxy source quality yields INCONCLUSIVE, not a numeric GO.
- Fixture scenarios may validate wiring and structural behavior only.
- Committed scenario IDs are immutable. Changing committed fixture data requires a new scenario ID, for example `_v2`, plus an ADR note explaining the change.
- Edge Verdict reports are artifacts for human review; CI checks only that the report is well formed and exits 0.

## Follow-ups

- Freeze the venue-data fixture policy, stress assumptions, and strategy-parameter grid before using performance thresholds for promotion.
- Add real venue cache population/refresh governance around `scripts/populate_bybit_cache.py`.
- Calibrate future stress and collapse rules against frozen real venue snapshots.

## Deferred

Hard performance-threshold promotion is deferred to a future ADR-0002 after fixture governance, stress assumptions, and strategy parameters are frozen. Until ADR-0002 is accepted, the Edge Verdict remains a non-gating report.

# ajentix-alpha

> Retail opportunistic capital-growth toolkit for $500–2000. **Free data in, ranked opportunity
> sheet out.** The agent builds decision-support tooling; **the user executes all on-chain actions.**

## Why this exists

Sibling project `ajentix-quant` proved — across 21 pre-registered / OOS-screened mechanisms
(funding, momentum, arb, carry, reversal, factor, VRP, token-unlock) — that **a capital-authorizing,
market-neutral, *systematic* edge is structurally unreachable from FREE data at retail scale.** Free
data yields either OOS-dead signals or a non-authorizing source-quality / survivorship ceiling.

`ajentix-alpha` plays a different game: **opportunistic, EV/risk-driven capital allocation** (real
yield, incentives, points/airdrops) where the "edge" is doing work and bearing illiquidity / smart-
contract risk that others won't — consistent with the only survivor class (risk premia), but accessed
through allocation + ops rather than a backtested price signal.

## Hard boundaries

- **Agent role:** build scanners, EV/IL/risk models, sizing, monitoring, alerting. Read-only research.
- **User role:** every wallet action, deposit, claim, bridge, airdrop interaction, and opsec decision.
  The agent cannot and does not sign transactions or touch keys.
- **Not financial advice.** DeFi carries total-loss risk (contract exploits, depegs, reward-token
  collapse, chain failure). Every number here is modeled, not guaranteed.

## Discipline (what rigor means here)

Opportunities are forward bets, **not OOS-backtestable** like price edges. So the rigor is:
honest forward EV, impermanent-loss / drawdown modeling, hard risk caps, reward-decay + depeg +
contract-risk haircuts, capital efficiency, and **never trusting a quoted APR** without modeling its
decay and the reward token's price risk.

## First module: real-yield / incentive scanner

Ranks DeFi pools by **risk-adjusted yield** from the free DefiLlama yields API
(`https://yields.llama.fi/pools`, no key): base + reward APR, penalized for APY volatility (mu/sigma),
impermanent-loss exposure, low TVL/liquidity, short history, reward-share (less sticky), and APY decay
(spot vs 30d mean). Stablecoin-heavy core + a small higher-yield satellite. Airdrop/points tracking is
a planned satellite module (eligibility + cost/EV; execution is manual).

## Layout

```
src/ajentix_alpha/yields/   # free yields client + risk-adjusted ranking
scripts/                    # CLI report generators
tests/                      # deterministic unit tests (no network)
```

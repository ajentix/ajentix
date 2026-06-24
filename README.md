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
(spot vs 30d mean). Stablecoin-heavy core + a small higher-yield satellite. Run
`scan_yields.py --fetch` to refresh the snapshot (each fetch is archived for monitoring) and write
`reports/yield_opportunities.{json,md}`.

## Sizing: ranked sheet -> capped allocation plan

A ranked sheet says *which* pools are worth it; `yields/sizing.py` says *how much* of a $500-2000
budget to place in each, under hard risk caps: a satellite sleeve capped at 30% of budget (unused
satellite budget flows down into the safer core sleeve, never up), per-pool caps (34% core / 10%
satellite), a $50 min-position floor (sub-floor dust is dropped and redistributed so a small budget
concentrates into a few real positions), and a per-sleeve position count cap. Pools are weighted by
their conservative net APY and water-filled under the caps; anything that cannot be deployed stays as
cash and is blended in at 0% (no optimistic accounting). Run `scan_yields.py --budget 1000` to emit
`reports/allocation_plan.{json,md}` alongside the opportunity sheet.

## Monitoring: watch what you hold

Opportunities decay. `yields/monitor.py` diffs two snapshots and flags degradation on your
positions: APY collapse, TVL drain (exit-liquidity / bank-run), reward-emissions cut, a pool
vanishing from the feed (delist / exploit), a freshly-raised risk flag, or a CORE->SATELLITE
downgrade — each with a severity. It deliberately does **not** apply the universe filter, so a
position draining below the tradeable TVL floor still alerts. No price oracle, so every alert is
grounded only in what the free feed reports. Run `scan_yields.py --fetch` at two points in time, then
`monitor_yields.py --watch reports/allocation_plan.json` to alert only on positions you entered;
output is `reports/alerts.{json,md}`.

## Airdrops / points: EV under the safe-yield bar

There is no reliable free feed of live airdrop data, so `airdrops/model.py` does not pretend to
scrape one. You supply each campaign's capital, lock, modeled airdrop value, payout probability,
costs, and confidence; the model haircuts by probability + confidence, subtracts costs **and the
safe yield you forgo by locking capital**, and ranks by annualized EV per dollar. Net EV is expressed
in excess of that opportunity cost, so `NEGATIVE_EV` means literally "you'd be better off in the CORE
stablecoin pool." The baseline yield is auto-derived from the cached yields snapshot (best CORE net
APY). Edit `data/airdrops/campaigns.json` (a template) and run `scan_airdrops.py` to emit
`reports/airdrop_ev.{json,md}`.

## Layout

```
src/ajentix_alpha/yields/     # free yields client + snapshots, ranking, sizing, monitoring
src/ajentix_alpha/airdrops/   # airdrop / points EV model (user-supplied campaign inputs)
scripts/                      # CLI report generators (scan_yields, monitor_yields, scan_airdrops)
tests/                        # deterministic unit tests (no network)
```

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

Two optional risk layers harden the ranking when enabled (each from a free, unauthenticated feed,
snapshotted and applied offline): `--prices` adds **depeg** detection (coins.llama.fi) — a stablecoin
pool whose underlying drifts off $1 is haircut and barred from CORE; `--protocols` adds **protocol
risk** (api.llama.fi) — unaudited or freshly-listed protocols are flagged and barred from CORE. With
both, CORE means "deep, stable, *audited, established, on-peg*."

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

## Calibration: is the conservatism real?

These are forward bets, not OOS-backtestable price edges, so this is **not a backtest and not a
performance claim** — a feedback loop. `yields/validate.py` takes two archived snapshots and, for
pools present in both, checks whether realized APY came in at or above the conservative net APY we
quoted (the haircuts are meant to under-promise), whether SPIKE-flagged pools actually reverted, and
whether CORE TVL held up better than SATELLITE. Run `validate_yields.py` over your history to emit
`reports/calibration.{json,md}`. Short windows are weak signal; the report says so.

## Rebalancing: holdings -> churn-aware moves

`yields/rebalance.py` diffs what you **actually hold** against a freshly-sized target into concrete
BUY / SELL / INCREASE / REDUCE / HOLD actions. A minimum-trade floor leaves dust-sized adjustments as
HOLD so a small account isn't churned to death on gas, while risk exits always fire: a held pool that
has dropped out of the ranked universe (degraded / gone) or carries a critical monitor alert is SOLD
regardless of size (and its capital redeployed into survivors). Edit `data/holdings.json` (a
template), then run `rebalance.py [--alerts reports/alerts.json]` to emit
`reports/rebalance_plan.{json,md}`. Pass `--prices`/`--protocols` to rank the target against the same
depeg/protocol-risk-filtered universe the scanner and dashboard use (otherwise the standalone plan can
surface a pool the dashboard would bar from CORE).

## Costs: gas-aware breakeven

`yields/costs.py` holds conservative per-chain round-trip (enter + exit) cost estimates, because at
$500-2000 gas is not a rounding error — a $50 position on Ethereum can take years to repay its gas.
The allocation sheet shows a **breakeven-days** column per position, and the rebalancer applies a
**gas-payback guard**: a BUY/INCREASE/REDUCE only fires if the moved capital's yield repays the
round-trip cost inside the payback window — otherwise it stays HOLD. Cheap chains move freely; an
Ethereum nibble at low APY does not.

## Points farming: accrual + capital efficiency

The airdrop EV model scores a campaign before you enter; `airdrops/points.py` tracks one you are
already farming. Keep a dated log of point balances + deployed capital in
`data/airdrops/points_log.json`; `scan_points.py` reports points/day, points per dollar-day, and —
when you supply a modeled value-per-point — an implied APY-equivalent so a farm can be weighed
against the safe yield it ties capital up against. Emits `reports/points_status.{json,md}`.

## Notifications

`monitor_yields.py --webhook URL` (or the `AJENTIX_WEBHOOK_URL` env var) POSTs a JSON alert payload
to any webhook (Slack / Discord / Telegram-webhook / custom) when an alert at or above
`--notify-min` (default critical) is present. Dependency-free stdlib POST; the URL is a secret — pass
it at runtime, never commit it. Pair with a cron entry to get pinged only when a held position breaks.

## One command: the dashboard

`report.py` runs the whole pipeline in-process — scan -> size -> monitor -> calibrate -> airdrops ->
points -> rebalance — and folds every result into a single `reports/dashboard.{json,md}`. Sections
degrade gracefully (monitoring/calibration need two snapshots; airdrops/points/rebalance run only
when their input file exists). `report.py --fetch --prices --protocols --budget 1000` is the
everything-on run.

## Running it systematically (cron / launchd)

The decision loop — fetch free data, re-rank, re-size, diff your positions, and **push an alert when
something degrades** — is pure deterministic code with no keys, so it runs itself on a schedule.
`report.py --fetch --webhook URL` is the whole loop in one command: each run archives a fresh
snapshot, regenerates the dashboard, and POSTs an alert (Slack/Discord/Telegram/custom) when a
held/target position breaks at or above `--notify-min` (default critical). You only act when pinged.

```bash
# cron: every 6h, refresh + alert on critical degradation (URL is a secret — keep it in the env)
0 */6 * * * AJENTIX_WEBHOOK_URL=… cd /path/to/ajentix-alpha && \
  python3 scripts/report.py --fetch --prices --protocols --budget 1000 >> reports/cron.log 2>&1
```

On macOS, a launchd agent (`~/Library/LaunchAgents/…plist` with `StartInterval` 21600) does the same
across reboots. What stays manual is **execution**: every deposit / withdraw / bridge / claim is a
signed transaction. By design the agent never holds keys or signs — so the system tells you exactly
what to do and when, but you (or a separately-built, explicitly-keyed executor) place the trades.

## Layout

```
src/ajentix_alpha/yields/     # data clients + ranking, sizing, costs, monitoring,
                              #   calibration, rebalancing, notifications
src/ajentix_alpha/airdrops/   # airdrop EV model + points-farming tracker
src/ajentix_alpha/dashboard.py  # folds every module's output into one summary
scripts/                      # CLIs: scan_yields, scan_airdrops, scan_points,
                              #   monitor_yields, validate_yields, rebalance, report
tests/                        # deterministic unit tests (no network)
```

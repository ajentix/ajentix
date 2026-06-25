# Next direction — pivot from systematic quant to the asymmetric/opportunistic lane

> Terminal note for the ajentix-quant research program. This repo stays FROZEN as the systematic
> research record. The opportunistic lane below is a DIFFERENT game and gets its own repo + session.

## Meta-finding (why we pivot)
Across 21 pre-registered/OOS-screened mechanisms: funding-harvest (OOS-dead), 18 inefficiencies
(momentum/arb/carry/reversal/factor — OOS-dead/flip), VRP/short-vol (NO_GO at the free-data ceiling),
token-unlock (non-authorizing survivor ceiling + statistically underpowered). The wall is structural,
not bad luck: at $500-2000 retail on FREE data, a CAPITAL-AUTHORIZING market-neutral systematic edge
is unreachable. Free data -> either OOS-dead or a non-authorizing source-quality/survivorship ceiling.
The only survivors are risk premia, and pricing them to a capital GO needs real quotes/fills the free
tier will not give. ajentix-quant proved this honestly; it is a valuable "do not lose money here" record.

## Decision
- Keep ajentix-quant frozen (systematic-quant research record). Do NOT build the new lane inside it.
- New repo + new session for the opportunistic lane (different identity: active, ops, EV/risk-driven,
  NOT deterministic-backtest-driven).
- Capital-GO path for the OLD lane remains open only via PAID/partner data (Tardis/DefiLlama Pro) to
  confirm the two "real-but-unconfirmed" signals (VRP OTM skew; token-unlock). Parked, not dead.


## First concrete thesis (recommended): real-yield / incentive opportunity scanner

Why this one: free DefiLlama YIELDS api (no Pro needed) gives APR/TVL/IL/reward data -> real,
reproducible. Agent-buildable end-to-end. Measurable, so SOME honest-rigor transfers (unlike
airdrops, which are forward bets you cannot OOS-backtest). It is the controlled-risk path to actually
growing $1000.

Scope:
- Scanner ranks pools by RISK-ADJUSTED yield: base APR + reward APR, penalized for impermanent-loss
  exposure, TVL/liquidity floor, protocol age/audit, reward-token dump risk, chain risk.
- Hard risk caps; stablecoin-heavy core + small higher-yield satellite.
- Airdrop/points farming = SATELLITE tracker only (eligibility + cost/EV), since execution is manual.

## Hard reality: role split (LLM=0, read-only research agent)
- Agent BUILDS: opportunity scanner, EV/IL/risk model, alerting, monitoring, sizing calculator.
- User EXECUTES: all on-chain wallet actions, deposits, claims, airdrop interactions, opsec. The agent
  cannot sign transactions or interact with wallets.

## Discipline shift (the rigor that DOES apply here)
- Not OOS-backtestable like price edges. Rigor = honest forward EV, IL/loss modeling, hard risk caps,
  reward-dump/depeg/contract-risk haircuts, capital efficiency, and never trusting a quoted APR without
  modeling its decay + token-price risk. Same anti-self-deception spirit, different machinery.

## Kickoff steps for the new repo/session
1. New repo (e.g. ajentix-alpha): identity = retail opportunistic capital-growth, free-data + manual exec.
2. Wire the free DefiLlama yields endpoint (poolsOld/pools) + a deterministic risk-adjusted ranking.
3. IL model + reward-decay haircut + risk caps; output a ranked, EV-annotated opportunity sheet.
4. Airdrop tracker satellite. Then the user executes against the sheet; agent monitors + re-ranks.

## Open question for kickoff
User risk appetite: (1) lottery/airdrop-heavy, (2) measurable yield grind [RECOMMENDED], (3) both.
Default if unspecified: (2), with (1) as a satellite tracker.

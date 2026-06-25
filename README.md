<div align="center">

# ajentix

**Honest edge-hunting for small capital.**

Pre-registered, anti-overfit **systematic-edge research** (runtime LLM = 0) **+** a disciplined,
free-data **DeFi yield allocator** — built to find edge where larger capital won't fight, and to
tell you the truth when there isn't any.

[![CI](https://github.com/ajentix/ajentix/actions/workflows/ci.yml/badge.svg)](https://github.com/ajentix/ajentix/actions/workflows/ci.yml)
[![License: MIT](https://img.shields.io/badge/License-MIT-blue.svg)](LICENSE)
[![Python 3.12+](https://img.shields.io/badge/python-3.12%2B-blue.svg)](pyproject.toml)

</div>

## Highlights

- 🔬 **Anti-overfit by construction** — pre-registration governance, walk-forward validation, frozen
  constants, single cost/slippage paths. Pipelines **fail closed and never fabricate** data.
- 🧾 **An honest research record** — 21 pre-registered / OOS-screened mechanisms (funding, momentum,
  arb, carry, reversal, factor, VRP, token-unlock). Most are `NO_GO`. This repo documents *what
  doesn't work*, with receipts — the opposite of a backtest brochure.
- 💸 **A sober DeFi allocator** — risk-adjusted yield scanner with chain-maturity + recognized-stable
  CORE gates, gas-aware sizing, depeg/protocol-risk filters, monitoring, and webhook alerts.
  Read-only research: **you sign every transaction.**
- 🎲 **Aggressive mode, eyes open** — an optional max-yield "degen" lens that ranks by *quoted* APY
  and spells out exactly how each pick can go to zero (IL, rug, decay, reward-dump, depeg).
- ✅ **727 tests**, `ruff` + `mypy --strict`, **zero runtime deps** in `alpha` (stdlib-only by
  design; `quant` adds only `pydantic`).

## Why this exists

The crown-jewel edge in crypto is **market-neutral, not directional** — and the *opposite* end,
**opportunistic allocation**, is paid for bearing illiquidity and smart-contract risk that larger,
safer capital avoids. Both chase the same thing: **edge where big money won't fight** (niches,
event-driven and structural mispricings).

The honest meta-finding, earned across 21 studies: **at $500–2000 on free data, a capital-authorizing,
market-neutral _systematic_ edge is structurally unreachable.** Free data yields either OOS-dead
signals or a non-authorizing source-quality / survivorship ceiling. `ajentix` proves this rigorously
(a valuable "do not lose money here" record) and then plays the *other* game — disciplined,
EV/risk-driven allocation where the rigor is honest forward EV, hard risk caps, and never trusting a
quoted APR.

> [!WARNING]
> Research / engineering project. **Not financial advice.** Crypto and DeFi carry total-loss risk
> (exploits, depegs, reward collapse, liquidation, counterparty, gap risk). Every number is modelled.

## Contents

- [Quickstart](#quickstart)
- [What's inside](#whats-inside)
- [The research record](#the-research-record)
- [Layout](#layout)
- [Large files & data](#large-files--data)
- [Contributing](#contributing)
- [License](#license)

## Quickstart

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install pydantic pydantic-settings pytest ruff mypy   # quant needs pydantic; alpha is stdlib-only

make check        # ruff + mypy (strict) + pytest across both packages (727 tests)

# alpha: a live, risk-adjusted DeFi yield sheet + capped $1,000 allocation plan (read-only)
python3 packages/alpha/scripts/report.py --fetch --prices --protocols --budget 1000
```

Tests run **per package** (`make` cds into each): both packages expose a top-level `scripts`
package, so a single root `pytest` would collide on `import scripts`.

## What's inside

| Package | What it is | Runtime LLM |
| --- | --- | --- |
| [**`packages/quant`**](packages/quant/README.md) | Deterministic, anti-overfit **systematic-edge research**: pre-registration governance, walk-forward, cost/slippage modelling, VRP / funding / token-unlock studies. Agents build and measure; they do not trade. | 0 |
| [**`packages/alpha`**](packages/alpha/README.md) | **Opportunistic capital allocation** from free data: real-yield / incentive scanners, EV/IL/risk models, gas-aware sizing, monitoring + webhook alerts, and an honest aggressive (degen) lens. | 0 |

Each package keeps its own `README.md`, `pyproject.toml`, and test suite. Per-package detail lives in
the linked docs.

## The research record

The point of `quant` is **not** a strategy to copy — it's a rigorously honest map of dead ends:

- **Delta-neutral funding harvest** — `NO_GO` out-of-sample: real funding (~0.003–0.01% / 8h) is
  dwarfed by ~0.165% round-trip cost at $1k.
- **VRP / defined-risk short-vol (ETH)** — run end-to-end on **5.09M real Deribit trades**: terminal
  `NO_GO`. ATM variance premium is dead; the only real premium is a thin (~10–17% of width),
  fat-tailed OTM put-skew — marginal-to-negative after a hard defined-risk cap.
- **Token-unlock supply-shock** — frozen pre-registration; max reachable outcome is hard-capped
  non-authorizing (free data cannot reconstruct delisted perps).

Write-ups live in [`packages/quant/docs/research`](packages/quant/docs/research).

## Layout

```
packages/
  quant/   # systematic-edge research + rigorous backtest framework
  alpha/   # opportunistic yield/allocation decision-support + alerting
pyproject.toml   # shared dev tooling (ruff); tests/typecheck run per package
```

## Large files & data

Data caches and generated reports are **never committed** (`.gitignore`): they are large and
regenerable.

- **Cheap to refetch** (DefiLlama yield snapshots) — `python3 packages/alpha/scripts/scan_yields.py --fetch`.
- **Expensive to rebuild** (Deribit history + reconstruction, ~3.5GB from ~5M real trades) — kept
  local; sync to object storage for backup. Credentials stay in `.env` (ignored).
- **Generated reports** (`packages/*/reports`) — rebuilt by the pipelines; not versioned.

## Contributing

Contributions are held to a high bar — honest, reproducible, never over-promising. See
[CONTRIBUTING.md](CONTRIBUTING.md). `make check` must be green (ruff + mypy strict + tests).

## License

[MIT](LICENSE) © 2026 [@yeongjunyoo](https://github.com/yeongjunyoo)

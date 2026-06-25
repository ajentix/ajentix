# ajentix

Monorepo for the ajentix edge-hunting stack. Consolidates two previously-separate repos, with full
git history preserved under `packages/`:

- **`packages/quant`** — deterministic, anti-overfit **systematic-edge research**: pre-registration
  governance, walk-forward, cost/slippage modelling, and the VRP / funding / token-unlock studies.
  Runtime LLM = 0; agents build and measure, they do not trade.
- **`packages/alpha`** — **opportunistic capital allocation** from free data: real-yield / incentive
  scanners, EV/IL/risk models, gas-aware sizing, monitoring + webhook alerts. Read-only research;
  the user executes every on-chain action.

Both chase the same thing from opposite ends — **edge where larger capital won't fight** (niches,
event-driven / structural mispricings) — which is why they now live together.

## Layout

```
packages/
  quant/   # systematic-edge research + rigorous backtest framework
  alpha/   # opportunistic yield/allocation decision-support + alerting
pyproject.toml   # shared dev tooling (one-command test/lint across both packages)
```

## Develop

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install pydantic pydantic-settings pytest ruff mypy   # quant needs pydantic; alpha is stdlib-only
make test        # runs both packages' suites (705 tests: 90 alpha + 615 quant)
make lint        # ruff across both packages
make check       # lint + typecheck + test
```

Tests run **per package** (`make` cds into each): both packages expose a top-level `scripts`
package, so a single root `pytest` would collide on `import scripts`. Each package keeps its own
pyproject (build, pythonpath, mypy strictness).

Per-package details live in `packages/<name>/README.md`.

## Large files

Data caches and generated reports are **never committed** (`.gitignore`): they are large and
regenerable. Tiers:

- **Cheap to refetch** (DefiLlama yield snapshots, `packages/alpha/data/cache`) — just regenerate:
  `python3 packages/alpha/scripts/scan_yields.py --fetch`.
- **Expensive to rebuild** (Deribit history + reconstruction, `packages/quant/data/cache`, ~3.5GB
  from ~5M real trades) — kept local; for cross-machine backup, sync to object storage (Cloudflare
  R2 / B2 recommended — cheap, no egress) rather than git-LFS. Credentials stay in `.env` (ignored).
- **Generated reports** (`packages/*/reports`) — rebuilt by the pipelines; not versioned.

Nothing here is financial advice. DeFi / crypto carry total-loss risk; all numbers are modelled.

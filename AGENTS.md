# AGENTS.md — working agreement for agents in this repo

## Workflow policy (default behavior)

- **Just commit and push.** After completing a self-contained task and **verifying it green**
  (see below), stage the changed files, commit with a clear conventional-commit message, and
  `git push` to `origin` — **without asking for confirmation.** This is the default for ordinary
  task work (features, fixes, refactors, docs).
- **Verify before you commit.** Run `make check` (lint + typecheck + test) from the repo root, or
  the relevant per-package gates, and only commit when they pass. Never commit red.
- **One logical change per commit.** Keep unrelated concerns (e.g. a feature vs. this meta-doc) in
  separate commits.
- **Respect other people's work.** Never revert, stash, force-push, or delete changes you did not
  make. Commit only what your task touched; do not sweep in unrelated working-tree edits.
- **Ask only when it genuinely matters** — a destructive/irreversible step, or a real fork in
  direction the repo can't answer. Routine task work does not need a check-in.

## Repo shape

Monorepo of two edge-hunting packages (full git history preserved under `packages/`):

- **`packages/quant`** — deterministic, anti-overfit **systematic-edge research**. Runtime LLM = 0;
  agents build/measure, they do not trade. Treat as a **frozen research record** (both systematic
  edges are terminal `NO_GO`); do not build new lanes inside it without an explicit decision.
- **`packages/alpha`** — **opportunistic capital allocation** from free data (yield/incentive
  scanners, EV/IL/risk models, sizing, monitoring, alerts). Read-only research; **the user executes
  every on-chain action** — the agent never holds keys or signs transactions.

## Tooling

- Python venv at repo root: `.venv/bin/python` (from root) or `../../.venv/bin/python` (from a
  package dir). Target is py311; `pip install pydantic pydantic-settings pytest ruff mypy`.
- `make test` / `make lint` / `make typecheck` / `make check` run **per package** (each `make` cds
  into the package). A single root `pytest` is intentionally NOT used: both packages expose a
  top-level `scripts` package and would collide on `import scripts`. Each package keeps its own
  `pyproject.toml` (build, pythonpath, mypy strictness, ruff line-length 100).

## Conventions

- Risk knobs are **explicit, documented, frozen constants** — no hidden optimism, no magic numbers.
- Prefer the narrowest correct change; reuse existing patterns over inventing parallel ones.
- **Never commit data caches or generated reports** — they are large and regenerable
  (`.gitignore`). Credentials stay in `.env` (ignored).
- Nothing here is financial advice; all numbers are modeled.

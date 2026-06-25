# Contributing

Thanks for your interest. This is a research + decision-support project, so contributions are held
to a deliberately high bar: **honest, reproducible, and never over-promising**.

## Ground rules

- **No fabricated results.** Every number must come from real, reproducible data or a clearly
  labelled model. Pipelines fail closed rather than emit a guess.
- **Risk constants are explicit and documented.** No magic numbers, no hidden optimism. If you add
  a haircut, cap, or threshold, give it a name and a one-line rationale.
- **Tests are not optional.** New behaviour ships with focused tests for edge values, branch
  conditions, and error handling. Don't test tautologies or defaults.
- **The agent builds; the user executes.** Nothing here signs transactions or touches keys, and
  contributions must keep it that way.

## Dev setup

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install pydantic pydantic-settings pytest ruff mypy
make check        # ruff + mypy (strict) + pytest, per package — must be green before you push
```

Tests run **per package** (`make` cds into each); both `packages/*` expose a top-level `scripts`
package, so a single root `pytest` would collide on `import scripts`.

## Before opening a PR

1. `make check` is green (lint + typecheck + tests).
2. Directly affected docs, callsites, and bundled defaults are updated — or you say why not.
3. Commit messages describe **what changed and why**, not just what.

Nothing here is financial advice; crypto/DeFi carry total-loss risk. Keep that framing intact.

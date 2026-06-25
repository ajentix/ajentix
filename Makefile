# ajentix monorepo dev commands. Tests/typecheck run per package (each package is self-contained
# and both expose a top-level `scripts` package, so a unified pytest would collide on imports).
PY ?= ../../.venv/bin/python
PACKAGES = alpha quant

.PHONY: test lint typecheck check
test:
	@for p in $(PACKAGES); do echo "== pytest $$p =="; (cd packages/$$p && $(PY) -m pytest -q) || exit 1; done

lint:
	.venv/bin/python -m ruff check packages

typecheck:
	@for p in $(PACKAGES); do echo "== mypy $$p =="; (cd packages/$$p && $(PY) -m mypy) || exit 1; done

check: lint typecheck test

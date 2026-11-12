.PHONY: install lint format typecheck test coverage plugin-validate check clean

PYTHON ?= python

install:
	$(PYTHON) -m pip install -e ".[dev]"

lint:
	$(PYTHON) -m ruff check .
	$(PYTHON) -m ruff format --check .

format:
	$(PYTHON) -m ruff format .
	$(PYTHON) -m ruff check --fix .

typecheck:
	$(PYTHON) -m mypy

test:
	$(PYTHON) -m pytest --cov --cov-report=term-missing

coverage: test

plugin-validate:
	$(PYTHON) scripts/check_manifest.py
	@if [ "$(HERMES_VALIDATE)" = "1" ]; then \
		command -v hermes >/dev/null 2>&1 || { \
			echo "HERMES_VALIDATE=1 but hermes is not on PATH" >&2; exit 1; }; \
		hermes plugins validate .; \
	else \
		echo "offline manifest checks only; set HERMES_VALIDATE=1 to run the Hermes validator"; \
	fi

check: lint typecheck test plugin-validate

clean:
	rm -rf .pytest_cache .mypy_cache .ruff_cache .coverage htmlcov build dist
	find . -name '__pycache__' -type d -prune -exec rm -rf {} +

UV ?= uv

.PHONY: help install lock lint format test docs docs-check build clean

help:  ## Show this help
	@grep -E '^[a-zA-Z_-]+:.*?## .*$$' $(MAKEFILE_LIST) | awk 'BEGIN {FS = ":.*?## "}; {printf "  \033[36m%-12s\033[0m %s\n", $$1, $$2}'

install:  ## Sync the venv to uv.lock with all extras (editable)
	$(UV) sync --locked --all-extras

lock:  ## Re-resolve uv.lock after a dependency change in pyproject.toml
	$(UV) lock

lint:  ## Ruff check + format check
	$(UV)x ruff@0.15.16 check src/ tests/
	$(UV)x ruff@0.15.16 format --check src/ tests/

format:  ## Ruff autoformat + autofix
	$(UV)x ruff@0.15.16 format src/ tests/
	$(UV)x ruff@0.15.16 check --fix src/ tests/

test:  ## Run the test suite
	$(UV) run --no-sync pytest --tb=short -q

docs:  ## Regenerate docs/cli.md from the Typer app
	$(UV) run --no-sync typer argus_atlas.cli utils docs --name argus-atlas --title "argus-atlas CLI reference" --output docs/cli.md

docs-check: docs  ## Fail if docs/cli.md is out of date with the CLI
	git diff --exit-code -- docs/cli.md

build:  ## Build sdist + wheel
	$(UV) build

clean:  ## Remove build + cache artifacts
	rm -rf dist build .venv *.egg-info .pytest_cache .ruff_cache src/argus_atlas/_version.py

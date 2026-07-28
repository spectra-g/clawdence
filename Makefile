.DEFAULT_GOAL := help
.PHONY: help setup fmt lint typecheck test check scan schema schema-test

help: ## Show this help
	@grep -hE '^[a-z-]+:.*?## ' $(MAKEFILE_LIST) | awk -F':.*?## ' '{printf "  \033[36m%-10s\033[0m %s\n", $$1, $$2}'

setup: ## Install the toolchain and the git hooks
	uv sync
	uv run pre-commit install

fmt: ## Format
	uv run ruff format .
	uv run ruff check --fix .

lint: ## Lint (no writes)
	uv run ruff check .
	uv run ruff format --check .

typecheck: ## mypy --strict
	uv run mypy

test: ## Run the test suite
	uv run pytest

scan: ## Scan the working tree and history for secrets
	gitleaks dir . --config .gitleaks.toml
	gitleaks git . --config .gitleaks.toml

schema: ## Regenerate schemas/ from the domain model
	uv run clawdence schema export

schema-test: ## Assert schemas/ matches the model, and the contracts round-trip
	uv run clawdence schema check
	uv run pytest tests/domain

check: lint typecheck test schema-test ## Everything CI runs, minus the history scan

.DEFAULT_GOAL := help
.PHONY: help setup fmt lint typecheck test check scan schema schema-test contract-tests record

help: ## Show this help
	@grep -hE '^[a-z-]+:.*?## ' $(MAKEFILE_LIST) | awk -F':.*?## ' '{printf "  \033[36m%-15s\033[0m %s\n", $$1, $$2}'

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

# The obligations every adapter must meet, wherever it lives. Marked rather than
# selected by path, so an adapter added in a new package is still covered by
# subclassing the contract — which is the only thing it has to remember to do.
contract-tests: ## Run the port contract suite against every adapter
	uv run pytest -m contract

# Refreshes the recorded LLM interactions. Needs real credentials and spends
# real money, which is why it is a separate target you have to type: a suite
# that could reach a provider by default is a suite that bills CI.
record: ## Re-record LLM cassettes (costs money; needs credentials)
	CLAWDENCE_CASSETTE=record uv run pytest

scan: ## Scan the working tree and history for secrets
	gitleaks dir . --config .gitleaks.toml
	gitleaks git . --config .gitleaks.toml

schema: ## Regenerate schemas/ from the domain model
	uv run clawdence schema export

schema-test: ## Assert schemas/ matches the model, and the contracts round-trip
	uv run clawdence schema check
	uv run pytest tests/domain

check: lint typecheck test schema-test ## Everything CI runs, minus the history scan

.DEFAULT_GOAL := help
.PHONY: help install up down logs ps test test-unit lint fmt typecheck check bench cov clean nuke

UV ?= uv

help: ## Show available targets
	@grep -E '^[a-zA-Z_-]+:.*?## .*$$' $(MAKEFILE_LIST) \
		| awk 'BEGIN {FS = ":.*?## "}; {printf "  \033[36m%-12s\033[0m %s\n", $$1, $$2}'

install: ## Sync the workspace and install pre-commit hooks
	$(UV) sync
	$(UV) run pre-commit install

up: ## Start infrastructure (Postgres, Redis) and wait for healthy
	docker compose up -d --wait

down: ## Stop infrastructure, keeping volumes
	docker compose down

logs: ## Tail infrastructure logs
	docker compose logs -f

ps: ## Show infrastructure status
	docker compose ps

test: ## Run the test suite, excluding tests that need infrastructure
	$(UV) run pytest -m "not integration and not e2e and not chaos and not perf"

test-unit: ## Run unit and property tests only
	$(UV) run pytest tests/unit tests/property

test-integration: ## Run tests that need Docker (testcontainers spins its own Postgres)
	$(UV) run pytest -m integration

test-e2e: ## Run the end-to-end slice (needs Docker; testcontainers spins its own Postgres)
	$(UV) run pytest -m e2e

lint: ## Check formatting and lint rules
	$(UV) run ruff check .
	$(UV) run ruff format --check .

fmt: ## Apply formatting and autofixable lint rules
	$(UV) run ruff format .
	$(UV) run ruff check --fix .

typecheck: ## Run mypy in strict mode
	$(UV) run mypy

check: lint typecheck test ## Everything CI runs

# By marker rather than by directory: `tests/perf/` is empty, so pointing the target at
# it collected nothing and NFR-1 was measured nowhere. Marked tests live next to the
# code they measure.
bench: ## Run benchmarks (PLAN.md §13.1, NFR-1)
	$(UV) run pytest -m perf --benchmark-only

cov: ## Run tests with a coverage report
	$(UV) run pytest --cov --cov-report=term-missing --cov-report=html

clean: ## Remove caches and build artifacts
	rm -rf .pytest_cache .mypy_cache .ruff_cache .hypothesis htmlcov .coverage dist build
	find . -type d -name __pycache__ -prune -exec rm -rf {} +

nuke: down ## Stop infrastructure and delete its volumes (destroys local data)
	docker compose down -v

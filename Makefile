.DEFAULT_GOAL := help
.PHONY: help install up down demo-up demo-seed demo-agent demo-mcp demo-conc demo-down demo-reset demo-tamper demo-untamper logs ps test test-unit \
        test-integration \
        test-e2e chaos lint fmt typecheck check bench cov clean nuke security sbom \
        benchmarks evidence

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

# T-056. An overlay on docker-compose.yml, not a second stack — same shape
# docker-compose.observability.yml uses. `demo-up` is the "one-command demo bring-up"
# T-056 asks for; NFR-8 (<90s) is measured against this in the `demo-stack` CI job.
demo-up: ## Build and start the full demo stack (control plane, PEP, tools) and wait healthy
	docker compose -f docker-compose.yml -f docker-compose.demo.yml up -d --wait --build

# The tokens live in the `demo-secrets` volume, written by the `seed` one-shot service
# during `demo-up` — not on the host — so this runs inside a container that mounts it.
# `--no-deps` because everything it needs is already up by the time you run this.
# It ends waiting up to 10 minutes on the bulk buyer's escalation: approve it in the console
# as the CFO and the agent completes the order itself (APICTA-03). Ctrl-C leaves it pending.
demo-seed: ## Drive the demo scenario through a running stack, so the console has content (T-057)
	docker compose -f docker-compose.yml -f docker-compose.demo.yml run --rm --no-deps seed python scripts/seed_demo.py --drive --tokens /secrets/demo-tokens.json --pep-url http://pep:8080 --control-plane-api-url http://controlplane:8000 --await-approval 600

# The sibling race, live (`scripts/demo_concurrency.py`). Every run is a fresh mandate —
# one run spends the pool — so: seed a new one, recreate the three PEPs bound to it, fire.
# Builds first, profile included: a stale `bootstrap` image writes a role map without the
# siblings, and every payment is then refused by policy rather than by the pool. The `up`
# is a no-op on a running, current stack and brings a cold one up.
demo-conc: ## Three siblings, three PEPs, one pool, all at once: show the ledger holds the total
	docker compose -f docker-compose.yml -f docker-compose.demo.yml --profile conc build
	docker compose -f docker-compose.yml -f docker-compose.demo.yml up -d --wait
	docker compose -f docker-compose.yml -f docker-compose.demo.yml --profile conc run --rm seed-conc python scripts/demo_concurrency.py --out /secrets --new
	docker compose -f docker-compose.yml -f docker-compose.demo.yml --profile conc up -d --wait --no-deps --force-recreate pep-conc-a pep-conc-b pep-conc-c
	docker compose -f docker-compose.yml -f docker-compose.demo.yml --profile conc run --rm --no-deps seed-conc python scripts/demo_concurrency.py --drive

# Edits one audit record directly so "Verify chain" can be shown failing, then puts it back.
# Runs in the seed container for the same reason demo-seed does: it has the DSN.
demo-tamper: ## Edit one audit record so Verify chain turns red (demo only; undo with demo-untamper)
	docker compose -f docker-compose.yml -f docker-compose.demo.yml run --rm --no-deps seed python scripts/demo_tamper.py

demo-untamper: ## Restore the record demo-tamper edited, so Verify chain is green again
	docker compose -f docker-compose.yml -f docker-compose.demo.yml run --rm --no-deps seed python scripts/demo_tamper.py --undo

# APICTA-05. The live LLM agent reads a benign invoice and a poisoned one through the PEP as
# agt-doc-reader. `--force-attempt` sends the poisoned payment itself if the model resisted,
# labelled FORCED on screen, so the refusal is shown either way. Run after demo-seed.
demo-agent: ## A live LLM agent meets a prompt injection; the PEP refuses what its token never held
	docker compose -f docker-compose.yml -f docker-compose.demo.yml run --rm --no-deps seed python scripts/demo_live_agent.py --tokens /secrets/demo-tokens.json --pep-url http://pep:8080 --control-plane-url http://controlplane:8000 --force-attempt

# APICTA-06. The refusal ladder again as MCP tools/call messages to POST /proxy/mcp, each rung
# checked against the reason code REST gets for it; exits 1 if any differs. Needs only the
# tokens demo-up's seed wrote, not demo-seed's traffic.
demo-mcp: ## The refusal ladder over MCP tools/call, checked against what REST decides
	docker compose -f docker-compose.yml -f docker-compose.demo.yml run --rm --no-deps seed python scripts/demo_mcp.py --tokens /secrets/demo-tokens.json --pep-url http://pep:8080

demo-down: ## Stop the demo stack, keeping volumes
	docker compose -f docker-compose.yml -f docker-compose.demo.yml down

# The audit chain is append-only and the tree only resets when tokens are re-minted, so a
# rehearsal leaves decisions, spend and escalations behind. This wipes every demo volume —
# database, secrets, Keycloak — and brings the stack back empty. Run `demo-seed` after.
demo-reset: ## Wipe all demo data and bring the stack back up empty (destroys local data)
	docker compose -f docker-compose.yml -f docker-compose.demo.yml down -v
	docker compose -f docker-compose.yml -f docker-compose.demo.yml up -d --wait --build

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

# Nightly, per PLAN.md §13 — deliberately not part of `check` or `test-integration`.
# CH-1 alone holds Postgres down for 30 s, so this is minutes, not seconds.
chaos: ## Run the chaos scenarios and regenerate the results table (§13.2, T-052)
	$(UV) run pytest -m chaos
	$(UV) run python scripts/generate_chaos_results.py

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

# The `security-scan` CI job (`.github/workflows/ci.yml`) is authoritative — trivy and
# gitleaks in particular need binaries CI installs from actions rather than uv. This
# target runs the tools that *are* uv-installable, so a local iteration can find issues
# before pushing.
security: ## Run bandit, pip-audit, the SBOM check, and the log secret-scanning test (T-054)
	$(UV) run bandit -c pyproject.toml -r packages scripts
	$(UV) export --no-emit-workspace --no-editable --format=requirements-txt > /tmp/agentiam-requirements.txt
	$(UV) run pip-audit --disable-pip -r /tmp/agentiam-requirements.txt --strict
	$(UV) run python scripts/generate_sbom.py
	$(UV) run pytest tests/security/test_secret_scanning.py

sbom: ## Regenerate `docs/evidence/sbom.json` from the current venv (T-054)
	$(UV) run python scripts/generate_sbom.py --write

benchmarks: ## Regenerate `docs/benchmarks/performance.md` from the committed JSON (T-053)
	$(UV) run python scripts/generate_benchmark_results.py

evidence: ## Regenerate `docs/evidence/evidence-pack.html` from committed sources (T-055)
	$(UV) run python scripts/generate_evidence_pack.py

clean: ## Remove caches and build artifacts
	rm -rf .pytest_cache .mypy_cache .ruff_cache .hypothesis htmlcov .coverage dist build
	find . -type d -name __pycache__ -prune -exec rm -rf {} +

nuke: down ## Stop infrastructure and delete its volumes (destroys local data)
	docker compose down -v

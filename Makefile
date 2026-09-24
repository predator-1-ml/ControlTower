# AI Operations Control Tower
#
# Windows: run these from Git Bash, or execute the underlying commands directly.

BACKEND := backend

# uv lays the venv out per OS: Scripts/python.exe on Windows, bin/python on
# POSIX. Hardcoding either breaks every target on the other. Windows always
# sets OS=Windows_NT. The path is relative: every target cd's into $(BACKEND).
ifeq ($(OS),Windows_NT)
PY := .venv/Scripts/python.exe
else
PY := .venv/bin/python
endif

.DEFAULT_GOAL := help
.PHONY: help setup up rebuild ps tail down logs migrate seed ingest test test-db lint fmt verify-resume clean

help: ## Show available targets
	@grep -hE '^[a-zA-Z_-]+:.*?## ' $(MAKEFILE_LIST) \
		| awk 'BEGIN{FS=":.*?## "}{printf "  \033[36m%-16s\033[0m %s\n", $$1, $$2}'

setup: ## Create the venv and install backend deps
	cd $(BACKEND) && uv venv --python 3.12 && uv pip install -e ".[dev]"

up: ## Start the full stack (postgres + backend + frontend)
	docker compose up -d

rebuild: ## Rebuild images and restart — needed after changing dependencies
	# Source is volume-mounted so code changes are live without this. Deps are
	# baked into the image (/opt/venv, node_modules), so pyproject.toml or
	# package.json changes need a rebuild or you get ModuleNotFoundError against
	# a package that is plainly installed locally.
	docker compose up -d --build

ps: ## Show service status
	docker compose ps

tail: ## Follow backend logs
	docker compose logs -f backend

down: ## Stop everything
	docker compose down

logs: ## Tail service logs
	docker compose logs -f --tail=100

migrate: ## Run checkpointer + domain migrations (advisory-locked, single process)
	cd $(BACKEND) && $(PY) -m app.scripts.migrate

dev: ## Run the backend locally on :8000
	# --reload is REQUIRED on Windows, not just convenient: uvicorn returns
	# ProactorEventLoop unless it is using a subprocess, and psycopg's async mode
	# cannot use it. Without --reload the app dies at startup with PoolTimeout.
	cd $(BACKEND) && $(PY) -m uvicorn app.main:app \
		--host 127.0.0.1 --port 8000 --reload --timeout-keep-alive 305

ingest: ## Embed knowledge chunks (needs live AWS credentials)
	cd $(BACKEND) && $(PY) -m app.scripts.ingest

seed: ## Load demo fixtures (safe to re-run; truncates first, so run ingest after)
	cd $(BACKEND) && $(PY) -m app.scripts.seed

test: ## Run unit tests (no database needed)
	cd $(BACKEND) && $(PY) -m pytest tests/ -q

test-db: ## Run all tests including integration (needs: up, migrate, seed)
	cd $(BACKEND) && CONTROL_TOWER_DB_TESTS=1 $(PY) -m pytest tests/ -q

lint: ## Lint and type-check
	cd $(BACKEND) && $(PY) -m ruff check app tests

fmt: ## Auto-format
	cd $(BACKEND) && $(PY) -m ruff format app tests

verify-resume: ## Prove durable execution across process death (needs: up, migrate)
	@echo "--- process 1: run until the interrupt, then exit ---"
	cd $(BACKEND) && $(PY) -m app.scripts.verify_resume start  --thread mk-$(USER)
	@echo ""
	@echo "--- process 2: brand-new process, resumes from Postgres ---"
	cd $(BACKEND) && $(PY) -m app.scripts.verify_resume resume --thread mk-$(USER)

clean: ## Remove venv, caches, and the Postgres volume
	docker compose down -v
	rm -rf $(BACKEND)/.venv $(BACKEND)/.pytest_cache $(BACKEND)/.ruff_cache
	find $(BACKEND) -name __pycache__ -type d -prune -exec rm -rf {} +

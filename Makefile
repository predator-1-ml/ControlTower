# AI Operations Control Tower
#
# Windows: run these from Git Bash, or execute the underlying commands directly.

BACKEND := backend
PY      := $(BACKEND)/.venv/Scripts/python.exe   # POSIX venvs: .venv/bin/python

.DEFAULT_GOAL := help
.PHONY: help setup up down logs migrate test lint fmt verify-resume clean

help: ## Show available targets
	@grep -hE '^[a-zA-Z_-]+:.*?## ' $(MAKEFILE_LIST) \
		| awk 'BEGIN{FS=":.*?## "}{printf "  \033[36m%-16s\033[0m %s\n", $$1, $$2}'

setup: ## Create the venv and install backend deps
	cd $(BACKEND) && uv venv --python 3.12 && uv pip install -e ".[dev]"

up: ## Start Postgres (+ app services) in the background
	docker compose up -d

down: ## Stop everything
	docker compose down

logs: ## Tail service logs
	docker compose logs -f --tail=100

migrate: ## Run checkpointer + domain migrations (advisory-locked, single process)
	cd $(BACKEND) && .venv/Scripts/python.exe -m app.scripts.migrate

test: ## Run the test suite
	cd $(BACKEND) && .venv/Scripts/python.exe -m pytest tests/ -q

lint: ## Lint and type-check
	cd $(BACKEND) && .venv/Scripts/python.exe -m ruff check app tests

fmt: ## Auto-format
	cd $(BACKEND) && .venv/Scripts/python.exe -m ruff format app tests

verify-resume: ## Prove durable execution across process death (needs: up, migrate)
	@echo "--- process 1: run until the interrupt, then exit ---"
	cd $(BACKEND) && .venv/Scripts/python.exe -m app.scripts.verify_resume start  --thread mk-$(USER)
	@echo ""
	@echo "--- process 2: brand-new process, resumes from Postgres ---"
	cd $(BACKEND) && .venv/Scripts/python.exe -m app.scripts.verify_resume resume --thread mk-$(USER)

clean: ## Remove venv, caches, and the Postgres volume
	docker compose down -v
	rm -rf $(BACKEND)/.venv $(BACKEND)/.pytest_cache $(BACKEND)/.ruff_cache
	find $(BACKEND) -name __pycache__ -type d -prune -exec rm -rf {} +

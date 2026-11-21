PY ?= python3
VENV := .venv
BIN := $(VENV)/bin

.PHONY: install dev test test-failures test-runtimes smoke demo visual fmt lint typecheck check load estimate token clean

$(BIN)/python:
	$(PY) -m venv $(VENV)
	$(BIN)/pip install -q --upgrade pip

# Stamp file: dependencies are reinstalled only when a requirements file changes.
$(VENV)/.installed: requirements.txt requirements-dev.txt | $(BIN)/python
	$(BIN)/pip install -q -r requirements-dev.txt
	@touch $@

install: $(VENV)/.installed ## install runtime + dev dependencies

dev: install ## run locally with hot reload on http://127.0.0.1:8080 (UI at /, chat at /chat)
	AGENTLINK_MODE=$${AGENTLINK_MODE:-development} $(BIN)/python -m agentlink.serve --host 127.0.0.1 --port 8080 --reload

test: ## run the whole automated suite (no LLM, no network)
	$(BIN)/pytest -q -p no:warnings

test-failures: ## only the failure/recovery scenarios (expired sessions, fencing, split-brain, coordinator loss, ...)
	$(BIN)/pytest -q -p no:warnings tests/test_recovery.py tests/test_client.py

test-runtimes: ## OPT-IN: real Claude Code / Codex / OpenCode runs (spends tokens). ONLY=claude,codex,opencode,cross
	RUN_REAL_RUNTIMES=1 $(BIN)/python scripts/e2e_runtimes.py $(if $(ONLY),--only $(ONLY),)

demo: ## TESTING ONLY: synthetic participants against a running dev server (ADMIN=<admin token>)
	$(BIN)/python scripts/synthetic_office.py --server http://127.0.0.1:8080 --admin-token "$(ADMIN)" --seconds $${SECONDS_:-600}

visual: ## headless Chromium check of / and /chat (TOKEN=<monitor token>)
	$(BIN)/python scripts/visual_check.py --url http://127.0.0.1:8080 --token "$(TOKEN)"

fmt: ## format code
	$(BIN)/ruff format agentlink tests scripts
	$(BIN)/ruff check --fix agentlink tests scripts

lint:
	$(BIN)/ruff format --check agentlink tests scripts
	$(BIN)/ruff check agentlink tests scripts

typecheck:
	$(BIN)/mypy

check: lint typecheck test ## everything CI runs

smoke: ## start a real server and exercise it through the CLI
	bash scripts/smoke.sh

load: ## simulate 5 agents + 15 sub-agents and measure latency/RSS
	$(BIN)/python scripts/loadtest.py

estimate: ## measure bytes/message and project 24 h storage
	$(BIN)/python scripts/estimate_storage.py

token: ## print a fresh random token
	@$(BIN)/python -m agentlink.cli gen-token

clean:
	rm -rf .pytest_cache .mypy_cache .ruff_cache

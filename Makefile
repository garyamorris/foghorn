# One entry point for humans and CI. `make help` lists targets.
.DEFAULT_GOAL := help
UV ?= uv run --frozen
PORT ?= 2026  # 2024 is Studio's conventional port; override with PORT=2024

.PHONY: eval-gate studio-guide dev-reload data data-check sync-uv-config help install dev lint fmt typecheck test test-fast e2e eval cov check config-check docker-dockerfile clean

help:  ## show targets
	@grep -E '^[a-zA-Z_-]+:.*## ' $(MAKEFILE_LIST) | awk -F':.*## ' '{printf "  %-18s %s\n", $$1, $$2}'

install:  ## create venv from uv.lock (exact)
	uv sync --frozen

dev:  ## Studio-compatible server (tracing off unless TRACE=1); never add --allow-blocking
	RESUMABLE_STREAM_TTL_SECONDS=7200 LANGSMITH_TRACING=$(if $(TRACE),true,false) $(UV) langgraph dev --port $(PORT) --no-browser --no-reload --n-jobs-per-worker 8  # default is 1 despite --help; --no-reload keeps the virtual clock/replay state

dev-reload:  ## like `dev` but with hot reload (resets virtual clock + replay position on every edit)
	RESUMABLE_STREAM_TTL_SECONDS=7200 LANGSMITH_TRACING=false $(UV) langgraph dev --port $(PORT) --no-browser --n-jobs-per-worker 8

data:  ## regenerate datasets (deterministic) / verify committed ones
	$(UV) python -m foghorn.data build

data-check:  ## committed datasets must equal the generator output
	$(UV) python -m foghorn.data build --check

lint:  ## ruff lint + format check
	$(UV) ruff check src tests
	$(UV) ruff format --check src tests

fmt:  ## autofix + format
	$(UV) ruff check --fix src tests
	$(UV) ruff format src tests

typecheck:  ## mypy --strict on src
	$(UV) mypy

test-fast:  ## everything except e2e (seconds)
	$(UV) pytest -m "not e2e and not live" -q

e2e:  ## spawns a real `langgraph dev` on a free port, loopback only
	$(UV) pytest -m e2e -q

eval:  ## offline eval gate (thresholds fail the build)
	$(UV) pytest tests/evals -q -s

eval-gate:  ## offline eval gate: baselines, quality floors, hard safety gates (no network)
	$(UV) python -m foghorn.evals run --suite all --profile competent --check-baseline --assert-thresholds

cov:  ## full suite with coverage gate (fail_under in pyproject)
	$(UV) pytest --cov -q

config-check:  ## langgraph.json validation + every graph loads exactly like the server
	$(UV) langgraph validate -c langgraph.json
	$(UV) pytest tests/contract -q

check: lint typecheck cov config-check data-check eval-gate  ## what CI runs

docker-dockerfile:  ## write a Dockerfile (no docker needed); build needs a daemon
	$(UV) langgraph dockerfile -c langgraph.json Dockerfile.generated

clean:
	rm -rf .pytest_cache .mypy_cache .ruff_cache .coverage htmlcov .langgraph_api

sync-uv-config:  ## regenerate langgraph.uv.json (locked Docker build config) from langgraph.json
	$(UV) python scripts/sync_uv_config.py

studio-guide:  ## regenerate docs/STUDIO_GUIDE.md from the per-graph GUIDE.md files
	$(UV) python scripts/build_studio_guide.py

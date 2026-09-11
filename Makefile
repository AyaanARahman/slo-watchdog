# slo-guard — developer entrypoints
.DEFAULT_GOAL := help
SHELL := /bin/bash

VENV      := .venv
PIP       := $(VENV)/bin/pip
PYTHON312 ?= /opt/homebrew/bin/python3.12

IMAGE := budget-api
TAG   ?= dev

.PHONY: help
help: ## Show available targets
	@grep -hE '^[a-zA-Z_-]+:.*?## ' $(MAKEFILE_LIST) \
	  | awk 'BEGIN{FS=":.*?## "};{printf "  \033[36m%-14s\033[0m %s\n", $$1, $$2}'

## ---------- service ----------

.PHONY: venv
venv: ## Create the virtualenv and install the service in editable mode
	test -d $(VENV) || $(PYTHON312) -m venv $(VENV)
	$(PIP) install -q --upgrade pip
	$(PIP) install -q -e "service[dev]"

.PHONY: lint
lint: ## Run ruff (lint + format check)
	$(VENV)/bin/ruff check service
	$(VENV)/bin/ruff format --check service

.PHONY: fmt
fmt: ## Auto-format with ruff
	$(VENV)/bin/ruff check --fix service
	$(VENV)/bin/ruff format service

.PHONY: typecheck
typecheck: ## Run mypy in strict mode
	cd service && ../$(VENV)/bin/mypy app

.PHONY: test
test: ## Run the test suite
	cd service && ../$(VENV)/bin/pytest

.PHONY: check
check: lint typecheck test ## Lint, typecheck, and test

.PHONY: run
run: ## Run the service locally on :8000
	cd service && ../$(VENV)/bin/uvicorn app.main:app --host 0.0.0.0 --port 8000 --reload

## ---------- image ----------

.PHONY: build
build: ## Build the container image
	docker build -t $(IMAGE):$(TAG) service

## ---------- cluster ----------

.PHONY: local-up
local-up: ## Bring up kind + monitoring stack + app
	@echo "not yet implemented"

.PHONY: local-down
local-down: ## Tear down the kind cluster
	@echo "not yet implemented"

.PHONY: harness
harness: ## Run all chaos scenarios and assert alert behaviour
	@echo "not yet implemented"

.PHONY: clean
clean: ## Remove caches and the virtualenv
	rm -rf $(VENV) .pytest_cache .ruff_cache .mypy_cache
	find . -name __pycache__ -type d -prune -exec rm -rf {} +

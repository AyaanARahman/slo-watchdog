# slo-guard — developer entrypoints
.DEFAULT_GOAL := help
SHELL := /bin/bash

VENV      := .venv
PIP       := $(VENV)/bin/pip
PYTHON312 ?= /opt/homebrew/bin/python3.12

IMAGE   := budget-api
TAG     ?= dev
CLUSTER := slo-watchdog
NS      := budget

# Host URLs, via the kind extraPortMappings in kind.yaml.
API_URL  := http://localhost:30080
PROM_URL := http://localhost:30090
ALERT_URL:= http://localhost:30093
GRAF_URL := http://localhost:30030

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

.PHONY: cluster
cluster: ## Create the kind cluster (no-op if it exists)
	@kind get clusters 2>/dev/null | grep -qx $(CLUSTER) \
	  && echo "cluster $(CLUSTER) already exists" \
	  || kind create cluster --config kind.yaml

.PHONY: load
load: build ## Build the image and side-load it into kind
	kind load docker-image $(IMAGE):$(TAG) --name $(CLUSTER)

.PHONY: monitoring
monitoring: ## Install/upgrade kube-prometheus-stack
	helm repo add prometheus-community https://prometheus-community.github.io/helm-charts
	helm repo update prometheus-community
	helm upgrade --install monitoring prometheus-community/kube-prometheus-stack \
	  --namespace monitoring --create-namespace \
	  --values monitoring/kube-prometheus-stack-values.yaml \
	  --wait --timeout 10m

.PHONY: deploy
deploy: load ## Apply the app manifests and wait for rollout
	kubectl apply -f k8s/
	kubectl -n $(NS) rollout status deploy/budget-api --timeout=120s

.PHONY: dashboards
dashboards: ## Provision Grafana dashboards from monitoring/grafana/dashboards/*.json
	kubectl create configmap grafana-dashboard-budget-api \
	  --namespace monitoring \
	  --from-file=monitoring/grafana/dashboards/ \
	  --dry-run=client -o yaml \
	  | kubectl label --local -f - grafana_dashboard=1 -o yaml \
	  | kubectl apply -f -

.PHONY: local-up
local-up: cluster monitoring deploy dashboards urls ## Bring up the whole stack

.PHONY: urls
urls: ## Print the host URLs for every component
	@echo ""
	@echo "  budget-api    $(API_URL)"
	@echo "  Prometheus    $(PROM_URL)"
	@echo "  Alertmanager  $(ALERT_URL)"
	@echo "  Grafana       $(GRAF_URL)  (admin/admin)"
	@echo ""

.PHONY: status
status: ## Show pods and whether Prometheus has found the target
	kubectl get pods -A -o wide
	@echo ""
	@echo "budget-api scrape targets known to Prometheus:"
	@curl -s "$(PROM_URL)/api/v1/targets?state=active" \
	  | grep -o '"job":"budget-api"[^}]*"health":"[a-z]*"' || echo "  none yet"

.PHONY: local-down
local-down: ## Delete the kind cluster
	kind delete cluster --name $(CLUSTER)

.PHONY: harness
harness: ## Run all chaos scenarios and assert alert behaviour
	@echo "not yet implemented"

.PHONY: clean
clean: ## Remove caches and the virtualenv
	rm -rf $(VENV) .pytest_cache .ruff_cache .mypy_cache
	find . -name __pycache__ -type d -prune -exec rm -rf {} +

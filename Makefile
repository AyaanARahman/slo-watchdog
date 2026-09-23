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

# Pinned by digest, not by tag: `latest` would silently change the generated rules
# between runs, and generated files must be reproducible from their input.
SLOTH_IMAGE := ghcr.io/slok/sloth@sha256:ad651f2de49307f5e5f971be82927ef99ac27bd67df6aa6aff713957ede5593f
SLO_SPEC    := slo/budget-api.slo.yaml
SLO_RULES   := slo/generated/budget-api.rules.yaml

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
	$(VENV)/bin/ruff check service harness
	$(VENV)/bin/ruff format --check service harness

.PHONY: fmt
fmt: ## Auto-format with ruff
	$(VENV)/bin/ruff check --fix service harness
	$(VENV)/bin/ruff format service harness

.PHONY: typecheck
typecheck: ## Run mypy in strict mode
	cd service && ../$(VENV)/bin/mypy app
	# --strict explicitly: run from the repo root, mypy finds no config file (the
	# strict settings live in service/pyproject.toml) and would silently fall back
	# to default mode.
	$(VENV)/bin/mypy --strict harness/run.py

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

## ---------- infrastructure (Terraform) ----------
##
## Terraform owns the cluster and the monitoring platform. It does NOT own the
## application: `make deploy` does. See terraform/main.tf for why that boundary is
## where it is.

TF := terraform -chdir=terraform

.PHONY: tf-init
tf-init: ## Initialise Terraform providers
	$(TF) init

.PHONY: tf-validate
tf-validate: ## Format-check and validate the Terraform config
	$(TF) fmt -check -recursive
	$(TF) validate

.PHONY: tf-plan
tf-plan: ## Show what Terraform would change
	$(TF) plan

.PHONY: tf-apply
tf-apply: ## Provision the cluster and monitoring stack with Terraform
	$(TF) apply

.PHONY: tf-destroy
tf-destroy: ## Tear down everything Terraform created
	$(TF) destroy

.PHONY: tf-up
tf-up: ## Terraform for the platform, then deploy the app on top
	$(TF) apply -auto-approve
	$(MAKE) deploy dashboards slo-apply urls

## ---------- SLOs ----------

.PHONY: slo-generate
slo-generate: ## Expand the SLO spec into Prometheus recording + alerting rules
	docker run --rm -v "$(PWD):/work" -w /work $(SLOTH_IMAGE) \
	  generate -i $(SLO_SPEC) -o $(SLO_RULES)

.PHONY: slo-check
slo-check: ## Fail if the generated rules are stale or hand-edited
	@docker run --rm -v "$(PWD):/work" -w /work $(SLOTH_IMAGE) \
	  generate -i $(SLO_SPEC) -o slo/generated/.check.yaml
	@if diff -u $(SLO_RULES) slo/generated/.check.yaml; then \
	  rm -f slo/generated/.check.yaml; \
	  echo "generated rules are up to date"; \
	else \
	  rm -f slo/generated/.check.yaml; \
	  echo "ERROR: $(SLO_RULES) is stale. Run 'make slo-generate'."; \
	  exit 1; \
	fi

.PHONY: slo-apply
slo-apply: ## Apply the generated PrometheusRule to the cluster
	kubectl apply -f $(SLO_RULES)

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
local-up: cluster monitoring deploy dashboards slo-apply urls ## Bring up the whole stack

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
	$(VENV)/bin/python -u harness/run.py

.PHONY: harness-list
harness-list: ## List the available scenarios
	@$(VENV)/bin/python -u harness/run.py --list

.PHONY: clean
clean: ## Remove caches and the virtualenv
	rm -rf $(VENV) .pytest_cache .ruff_cache .mypy_cache
	find . -name __pycache__ -type d -prune -exec rm -rf {} +

# Developer entry points. `make help` lists everything.
SHELL := /bin/bash
.DEFAULT_GOAL := help
COMPOSE := docker compose
K6 := $(COMPOSE) --profile loadtest run --rm k6 run

.PHONY: help
help: ## Show this help
	@grep -hE '^[a-zA-Z0-9_-]+:.*?## ' $(MAKEFILE_LIST) | awk 'BEGIN {FS = ":.*?## "}; {printf "  \033[36m%-22s\033[0m %s\n", $$1, $$2}'

# ---------------------------------------------------------------- local stack
.env:
	cp .env.example .env
	@echo "Created .env — edit the CHANGE_ME values."

.PHONY: up down restart ps logs
up: .env ## Build and start the full local stack
	$(COMPOSE) up -d --build
	@echo "API:        http://localhost:8080/docs"
	@echo "Grafana:    http://localhost:3000"
	@echo "Prometheus: http://localhost:9090"
	@echo "RabbitMQ:   http://localhost:15672"

down: ## Stop the stack (keeps data volumes)
	$(COMPOSE) down

destroy: ## Stop the stack AND delete all data volumes
	$(COMPOSE) down -v

ps: ## Container status and health
	$(COMPOSE) ps

logs: ## Follow logs of all services (make logs S=api-1 for one)
	$(COMPOSE) logs -f --tail=100 $(S)

migrate: ## Apply database migrations
	$(COMPOSE) run --rm migrate

psql: ## Open psql inside the PostgreSQL container
	$(COMPOSE) exec postgres psql -U taskplatform -d taskplatform

# ---------------------------------------------------------------- quality
.PHONY: install lint format test test-integration dashboards
install: ## Create .venv with dev dependencies
	python3 -m venv .venv && .venv/bin/pip install -r requirements-dev.txt

lint: ## Ruff + mypy
	ruff check . && mypy app

format: ## Auto-format
	ruff check --fix . && ruff format app tests

test: ## Unit tests (no infrastructure needed)
	pytest tests/unit

test-integration: ## Integration tests against the local compose services
	$(COMPOSE) up -d postgres redis rabbitmq && $(COMPOSE) run --rm migrate
	RUN_INTEGRATION=1 POSTGRES_HOST=localhost REDIS_HOST=localhost RABBITMQ_HOST=localhost \
	  pytest -m integration tests/integration

dashboards: ## Regenerate Grafana dashboard JSON
	python3 monitoring/grafana/generate_dashboards.py

docs: ## Build docs/tutorial.html from docs/tutorial/src
	python3 docs/tutorial/build.py

# ---------------------------------------------------------------- experiments
.PHONY: load-smoke load-normal load-high load-burst load-tasks scale-experiment chaos
load-smoke: ## k6 smoke test
	$(K6) /scripts/smoke.js
load-normal: ## k6 normal traffic
	$(K6) /scripts/normal.js
load-high: ## k6 ramp to saturation
	$(K6) /scripts/high.js
load-burst: ## k6 spike test
	$(K6) /scripts/burst.js
load-tasks: ## k6 async task pipeline
	$(K6) /scripts/task-processing.js
scale-experiment: ## Compare 1 vs 2 vs 3 API servers
	scripts/scale-experiment.sh normal 150
chaos: ## List failure-injection scenarios (make chaos S=kill-worker)
	scripts/chaos.sh $(or $(S),list)

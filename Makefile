-include .env
export

POSTGRES_USER ?= base_gov
POSTGRES_DB ?= base_gov

.PHONY: help env build-db up down destroy logs ps psql init-db ingest-postgres lint fmt typecheck test check

help: ## List available targets
	@grep -E '^[a-zA-Z_-]+:.*?## .*$$' $(MAKEFILE_LIST) | sort | awk 'BEGIN {FS = ":.*?## "}; {printf "%-18s %s\n", $$1, $$2}'

env: ## Create .env from .env.example if missing
	@[ -f .env ] || cp .env.example .env

build-db: ## Build the postgres + pg_parquet image
	docker compose build

up: ## Start the postgres service
	docker compose up -d

down: ## Stop services
	docker compose down

destroy: ## Stop services and remove volumes
	docker compose down -v

logs: ## Tail postgres logs
	docker compose logs -f postgres

ps: ## Show running services
	docker compose ps

psql: ## Open a psql shell against the running container
	docker compose exec postgres psql -U $$POSTGRES_USER -d $$POSTGRES_DB

init-db: ## Re-apply sql/00*.sql against the running container (safe on a warm volume)
	@for f in sql/00*.sql; do \
		echo "==> $$f"; \
		docker compose exec -T postgres psql -v ON_ERROR_STOP=1 -U $$POSTGRES_USER -d $$POSTGRES_DB -f - < $$f; \
	done

ingest-postgres: ## Load data/*.parquet into procurement.* via bin/ingest-postgres.sh
	bin/ingest-postgres.sh

lint: ## Run ruff checks
	uv run ruff check .

fmt: ## Run ruff formatter
	uv run ruff format .

typecheck: ## Run mypy
	uv run mypy src

test: ## Run pytest
	uv run pytest

check: lint typecheck test ## Run lint + typecheck + test

.PHONY: install dev worker test test-pg lint migrate samples smoke up down

install:            ## create a venv and install everything (incl. dev tools)
	python -m venv .venv && . .venv/bin/activate && pip install -e ".[dev]"

dev:                ## run the API locally (inline processing, SQLite, no other services)
	uvicorn app.asgi:app --reload --port 8000

worker:             ## run an arq worker (needs Redis and PROCESSING_MODE=worker on the API)
	arq app.workers.settings.WorkerSettings

test:               ## unit + integration tests on SQLite (the Redis test skips itself)
	pytest

test-pg:            ## same suite on Postgres:  make test-pg URL=postgresql+asyncpg://user:pw@localhost/test
	TEST_DATABASE_URL=$(URL) pytest

lint:
	ruff check .

migrate:            ## apply migrations to $DATABASE_URL
	alembic upgrade head

samples:            ## regenerate ./samples
	python -m scripts.make_sample_data

smoke:              ## end-to-end check against a running instance (default :8000)
	python -m scripts.smoke_test $(or $(URL),http://localhost:8000)

up:                 ## full production-shaped stack (nginx, api, worker, postgres, redis)
	docker compose -f docker/docker-compose.yml up --build

down:
	docker compose -f docker/docker-compose.yml down

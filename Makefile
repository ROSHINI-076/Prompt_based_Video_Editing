.PHONY: up down test lint openapi bench

up:
	docker compose up -d --build

down:
	docker compose down

test:
	python scripts/test_suite.py

lint:
	python -m ruff check incubrix/ tests/
	python -m ruff format --check incubrix/ tests/

openapi:
	python scripts/generate_openapi.py


bench:
	python -m incubrix.runner.bench

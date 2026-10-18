.PHONY: up down test lint openapi bench

up:
	docker compose up -d --build

down:
	docker compose down

test: lint
	@which docker >/dev/null 2>&1 && docker compose up -d postgres minio minio-init 2>/dev/null || true
	python -m pytest tests/ -v --tb=short

lint:
	python -m ruff check incubrix/ tests/
	python -m ruff format --check incubrix/ tests/

openapi:
	python -c "from incubrix.api.app import app; import json; open('openapi.yaml', 'w', encoding='utf-8').write(json.dumps(app.openapi(), indent=2))"


bench:
	python -m incubrix.runner.bench

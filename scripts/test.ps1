# Run lint, format, and unit + integration tests
# Starts postgres and minio through docker compose if not already running

Write-Host "Ensuring Postgres and MinIO containers are running..."
try {
    docker compose up -d postgres minio minio-init
} catch {
    Write-Warning "Could not start docker containers: $_"
}

Write-Host "Running Ruff lint checks..."
python -m ruff check incubrix/ tests/
if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }

Write-Host "Running Ruff format check..."
python -m ruff format --check incubrix/ tests/
if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }

Write-Host "Running pytest test suite (unit + integration)..."
python -m pytest tests/ -v --tb=short
if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }

Write-Host "All tests completed successfully!"

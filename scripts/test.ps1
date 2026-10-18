# Run lint, format, and unit + integration tests
# Starts postgres and minio through docker compose if not already running

# On some Windows Docker Desktop / Buildx setups, the build permission gate can
# block docker compose before the app is built. Set it only for this process so
# the rest of the environment remains unchanged.
if (-not $env:BUILDX_BAKE_ENTITLEMENTS_FS) {
    $env:BUILDX_BAKE_ENTITLEMENTS_FS = "0"
}

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

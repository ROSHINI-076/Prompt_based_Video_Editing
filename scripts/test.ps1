# Run lint, format, integration, and Compose end-to-end tests.

if (-not $env:BUILDX_BAKE_ENTITLEMENTS_FS) {
    $env:BUILDX_BAKE_ENTITLEMENTS_FS = "0"
}

python scripts/test_suite.py
exit $LASTEXITCODE

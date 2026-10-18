# IncuBrix Prompt-Based Video Editing Engine

## Python version

Python 3.11 or newer is required. This matches the package metadata, Ruff and mypy configuration, CI workflow, and Docker image.

## Local checks

Run `make test` to check Ruff lint, Ruff formatting, and the pytest suite.

## Authentication

Create a local `.env` file from `.env.example`, then generate a random bearer token:

```bash
python -c "import secrets; print(secrets.token_hex(32))"
```

Paste that value into `API_TOKEN` in `.env` and keep the same value in `docker-compose.yml` via the shell environment or `.env` file. Send it on every authenticated request as:

```bash
curl -H "Authorization: Bearer <token>" http://localhost:8000/api/v1/jobs/nonexistent
```

## Troubleshooting (Windows)

On some Windows Docker Desktop / Buildx setups, `docker compose up -d --build` can fail during the build phase with a BuildKit entitlement complaint such as:

`additional privileges requested: pass "--allow=fs.read=Dockerfile" to grant requested privileges`

This is a Docker environment issue, not an application error. The project itself is not using a bad build context, and the repo is a normal directory rather than a symlink. The working workaround on the affected machine was to set the entitlement only for the current process:

```powershell
$env:BUILDX_BAKE_ENTITLEMENTS_FS = "0"
docker compose up -d --build
```

A normal machine should still use the plain setup below with no special flags:

```bash
docker compose up -d --build
```

The repo also includes a stable Compose project name (`name: incubrix`) to avoid the warning about an unnamed project when the directory is reached through a Windows path or a symlinked layout.
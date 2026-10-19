"""Start the real Compose stack and run the repository's required checks."""

from __future__ import annotations

import os
import secrets
import subprocess
import sys
import time
from urllib.error import URLError
from urllib.request import Request, urlopen


def _run(command: list[str], env: dict[str, str]) -> int:
    return subprocess.run(command, env=env, check=False).returncode


def _wait_for_api(env: dict[str, str]) -> bool:
    request = Request(
        "http://127.0.0.1:8000/ready",
        headers={"Authorization": f"Bearer {env['API_TOKEN']}"},
    )
    for _ in range(120):
        try:
            with urlopen(request, timeout=2) as response:
                if response.status == 200:
                    return True
        except (OSError, URLError):
            time.sleep(1)
    return False


def main() -> int:
    report_engine_coverage = "--coverage-engine" in sys.argv[1:]
    env = os.environ.copy()
    env.setdefault("BUILDX_BAKE_ENTITLEMENTS_FS", "0")
    if not env.get("API_TOKEN"):
        env["API_TOKEN"] = secrets.token_urlsafe(32)
    env["AUTH_DISABLED"] = "false"
    env["TEST_MODE"] = "false"
    env["STT_PROVIDER"] = "mock"
    env["STT_MOCK_TRANSCRIPT_PATH"] = ""
    env["LLM_PROVIDER"] = "mock"
    env["EMBEDDER_PROVIDER"] = "mock"
    env["DATABASE_URL"] = (
        "postgresql+psycopg2://incubrix:incubrix@localhost:5432/incubrix"
    )
    env["DATABASE_URL_ASYNC"] = env["DATABASE_URL"]
    env["S3_ENDPOINT"] = "http://localhost:9000"
    if not env.get("S3_ACCESS_KEY"):
        env["S3_ACCESS_KEY"] = f"ibx{secrets.token_hex(6)}"
    if not env.get("S3_SECRET_KEY"):
        env["S3_SECRET_KEY"] = secrets.token_urlsafe(32)
    env.setdefault("S3_BUCKET", "incubrix")
    if not env.get("HMAC_SECRET"):
        env["HMAC_SECRET"] = secrets.token_urlsafe(32)
    if not env.get("CALLBACK_HMAC_SECRET"):
        env["CALLBACK_HMAC_SECRET"] = secrets.token_urlsafe(32)

    print("Starting Postgres, MinIO, API, worker, and bucket initialization...")
    compose_env = env.copy()
    compose_env["DATABASE_URL"] = (
        "postgresql+psycopg2://incubrix:incubrix@postgres:5432/incubrix"
    )
    compose_env["DATABASE_URL_ASYNC"] = compose_env["DATABASE_URL"]
    compose_env["S3_ENDPOINT"] = "http://minio:9000"
    if _run(["docker", "compose", "up", "-d", "--build"], compose_env) != 0:
        return 1
    if not _wait_for_api(env):
        print("Compose API did not become ready within 120 seconds.", file=sys.stderr)
        return 1

    pytest_command = [sys.executable, "-m", "pytest", "tests/", "-q"]
    if report_engine_coverage:
        pytest_command.extend(
            ["--cov=incubrix.engine", "--cov-report=term-missing"]
        )

    for command in (
        [sys.executable, "-m", "ruff", "check", "incubrix/", "tests/"],
        [sys.executable, "-m", "ruff", "format", "--check", "incubrix/", "tests/"],
        pytest_command,
    ):
        status = _run(command, env)
        if status:
            return status
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

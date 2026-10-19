"""Application configuration using pydantic-settings.

All settings are read from environment variables. No hard-coded paths,
keys, or model names. See .env.example for a complete list.
"""

from __future__ import annotations

from pydantic import Field
from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    """Central configuration. Every value comes from an env var."""

    model_config = {"env_file": ".env", "env_file_encoding": "utf-8", "extra": "ignore"}

    # ── Server ──────────────────────────────────────────────────────
    HOST: str = "0.0.0.0"
    PORT: int = 8000
    DEBUG: bool = False
    API_TOKEN: str = ""  # required unless TEST_MODE + AUTH_DISABLED are both explicitly true
    AUTH_DISABLED: bool = False  # only for explicit local/test-only startup bypasses
    RATE_LIMIT_PER_MINUTE: int = 60
    MAX_UPLOAD_BYTES: int = 524_288_000

    # ── Database ────────────────────────────────────────────────────
    DATABASE_URL: str = "postgresql+psycopg2://incubrix:incubrix@postgres:5432/incubrix"
    DATABASE_URL_ASYNC: str = "postgresql+asyncpg://incubrix:incubrix@postgres:5432/incubrix"
    DB_POOL_SIZE: int = 5
    DB_MAX_OVERFLOW: int = 10

    # ── S3 / MinIO ──────────────────────────────────────────────────
    S3_ENDPOINT: str = "http://minio:9000"
    S3_ACCESS_KEY: str = ""
    S3_SECRET_KEY: str = ""
    S3_BUCKET: str = "incubrix"
    S3_REGION: str = "us-east-1"
    S3_PRESIGNED_EXPIRY: int = 3600  # seconds

    # ── Adapters ────────────────────────────────────────────────────
    STT_PROVIDER: str = "mock"  # mock | faster_whisper
    STT_MODEL: str = ""
    STT_DEVICE: str = "cpu"
    LLM_PROVIDER: str = "mock"  # mock | groq | openai
    LLM_MODEL: str = ""
    LLM_BASE_URL: str = ""
    LLM_API_KEY: str = ""
    GROQ_API_KEY: str = ""
    OPENAI_API_KEY: str = ""
    LLM_TIMEOUT_S: float = Field(default=30.0, gt=0)
    LLM_MAX_RETRIES: int = Field(default=2, ge=0)
    EMBEDDER_PROVIDER: str = "mock"  # mock | local
    EMBEDDER_MODEL: str = ""
    EMBEDDER_DIMENSIONS: int = Field(default=128, ge=1)

    # ── Engine limits ───────────────────────────────────────────────
    MIN_SPEED: float = 0.25
    MAX_SPEED: float = 4.0
    MIN_CAPTION_S: float = 0.7
    LOW_CONFIDENCE_THRESHOLD: float = 0.6

    # ── Upload limits ───────────────────────────────────────────────
    MAX_UPLOAD_SIZE_MB: int = 500
    URL_FETCH_TIMEOUT_S: int = 120
    URL_FETCH_MAX_SIZE_MB: int = 500
    MAX_DOWNLOAD_BYTES: int = 524_288_000
    URL_MAX_REDIRECTS: int = 3

    # ── Jobs / callbacks ────────────────────────────────────────────
    WORKER_POLL_INTERVAL_S: float = 1.0
    WORKER_CONCURRENCY: int = 2
    HMAC_SECRET: str = ""
    CALLBACK_HMAC_SECRET: str = ""
    CALLBACK_MAX_RETRIES: int = 3
    CALLBACK_BACKOFF_S: float = 2.0
    JOB_MAX_ATTEMPTS: int = 3
    JOB_HEARTBEAT_STALE_S: int = 120
    IDEMPOTENCY_TTL_S: int = 86_400
    STT_MOCK_TRANSCRIPT_PATH: str = ""

    # ── Logging ─────────────────────────────────────────────────────
    LOG_LEVEL: str = "INFO"
    LOG_FORMAT: str = "json"  # json | console

    # ── Cost estimation (price per unit from env) ───────────────────
    STT_COST_PER_MINUTE: float = 0.0
    LLM_COST_PER_1K_TOKENS: float = 0.0
    LLM_INPUT_COST_PER_1K_TOKENS: float = 0.0
    LLM_OUTPUT_COST_PER_1K_TOKENS: float = 0.0

    # ── Misc ────────────────────────────────────────────────────────
    TEST_MODE: bool = False  # enables file:// URL intake


def validate_auth_configuration(settings: Settings | None = None) -> Settings:
    """Require a bearer token unless an explicit test-only bypass is enabled."""
    resolved = settings or Settings()
    if not resolved.API_TOKEN and not (resolved.TEST_MODE and resolved.AUTH_DISABLED):
        raise RuntimeError(
            "API_TOKEN is required unless TEST_MODE=true and "
            "AUTH_DISABLED=true are both set explicitly."
        )
    return resolved


def get_settings() -> Settings:
    """Return a cached Settings instance."""
    return validate_auth_configuration()

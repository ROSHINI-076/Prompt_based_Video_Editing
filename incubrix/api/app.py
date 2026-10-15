"""FastAPI application entry point.

Provides /health and /ready endpoints, structured error handling,
and correlation-id middleware.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request, Response
from fastapi.responses import JSONResponse

from incubrix.config import get_settings
from incubrix.logging import correlation_id_var, get_logger, setup_logging

log = get_logger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncGenerator[None, None]:
    """Application lifespan: setup and teardown."""
    settings = get_settings()
    setup_logging(log_level=settings.LOG_LEVEL, log_format=settings.LOG_FORMAT)
    log.info("incubrix_starting", host=settings.HOST, port=settings.PORT)
    yield
    log.info("incubrix_shutting_down")


app = FastAPI(
    title="IncuBrix Video Editing Engine",
    version="0.1.0",
    lifespan=lifespan,
)


# ── Correlation-ID middleware ──────────────────────────────────────
@app.middleware("http")
async def correlation_id_middleware(request: Request, call_next) -> Response:
    """Thread a correlation ID through every request/response.

    If the client sends X-Correlation-Id, use it; otherwise generate one.
    Echo it back in the response header.
    """
    cid = request.headers.get("x-correlation-id", str(uuid.uuid4()))
    correlation_id_var.set(cid)
    response = await call_next(request)
    response.headers["X-Correlation-Id"] = cid
    return response


# ── Global exception handler ──────────────────────────────────────
@app.exception_handler(Exception)
async def global_exception_handler(request: Request, exc: Exception) -> JSONResponse:
    """Return standard error JSON for unhandled exceptions."""
    log.error("unhandled_exception", error=str(exc), exc_info=True)
    return JSONResponse(
        status_code=500,
        content={
            "error": {
                "code": "INTERNAL_ERROR",
                "message": "An unexpected error occurred.",
                "retryable": True,
                "details": str(exc) if get_settings().DEBUG else None,
            }
        },
    )


# ── Health endpoints ──────────────────────────────────────────────
@app.get("/health")
async def health() -> dict:
    """Liveness check: process is alive."""
    return {"status": "ok"}


@app.get("/ready")
async def ready() -> dict:
    """Readiness check: Postgres and MinIO are reachable.

    Returns 503 when either dependency is down.
    """
    errors: list[str] = []

    # Check Postgres
    try:
        from sqlalchemy import create_engine, text

        settings = get_settings()
        engine = create_engine(settings.DATABASE_URL, pool_pre_ping=True)
        with engine.connect() as conn:
            conn.execute(text("SELECT 1"))
        engine.dispose()
    except Exception as e:
        errors.append(f"postgres: {e}")

    # Check MinIO / S3
    try:
        import boto3
        from botocore.client import Config as BotoConfig

        settings = get_settings()
        s3 = boto3.client(
            "s3",
            endpoint_url=settings.S3_ENDPOINT,
            aws_access_key_id=settings.S3_ACCESS_KEY,
            aws_secret_access_key=settings.S3_SECRET_KEY,
            region_name=settings.S3_REGION,
            config=BotoConfig(signature_version="s3v4"),
        )
        s3.head_bucket(Bucket=settings.S3_BUCKET)
    except Exception as e:
        errors.append(f"minio: {e}")

    if errors:
        return JSONResponse(
            status_code=503,
            content={"status": "unavailable", "errors": errors},
        )

    return {"status": "ready"}

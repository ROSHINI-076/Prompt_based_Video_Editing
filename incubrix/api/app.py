"""FastAPI application entry point for the IncuBrix API."""

from __future__ import annotations

import hmac
import os
import tempfile
import time
import uuid
from collections import defaultdict, deque
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Depends, FastAPI, HTTPException, Request, Response
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

from incubrix.api.errors import APIError, api_error
from incubrix.api.idempotency import IdempotencyMiddleware
from incubrix.config import get_settings
from incubrix.db.engine import get_session_factory
from incubrix.db.models import TranscriptRecord, VideoRecord
from incubrix.db.repository import ConcurrencyError, SessionNotFoundError, SessionRepository
from incubrix.jobs.queue import JobQueue
from incubrix.logging import correlation_id_var, get_logger, setup_logging
from incubrix.pipeline.instructions import run_instruction

log = get_logger(__name__)


def require_api_token(request: Request) -> None:
    """Enforce bearer-token auth for every /api/v1 route before any lookup."""
    settings = get_settings()
    if settings.AUTH_DISABLED and settings.TEST_MODE:
        return

    auth_header = request.headers.get("authorization", "")
    if not auth_header:
        raise HTTPException(status_code=401, detail="Missing bearer token.")

    scheme, _, token = auth_header.partition(" ")
    if scheme.lower() != "bearer" or not token:
        raise HTTPException(status_code=401, detail="Missing bearer token.")

    if not hmac.compare_digest(token, settings.API_TOKEN):
        raise HTTPException(status_code=401, detail="Invalid bearer token.")


api_router = APIRouter(prefix="/api/v1", dependencies=[Depends(require_api_token)])

_RATE_BUCKETS: defaultdict[str, deque[float]] = defaultdict(deque)


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncGenerator[None, None]:
    """Setup the app-wide logging and runtime configuration."""
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
app.add_middleware(IdempotencyMiddleware)


@app.middleware("http")
async def security_and_correlation_middleware(request: Request, call_next):
    """Attach a correlation ID, enforce bearer auth, and impose a simple rate limit."""
    cid = request.headers.get("x-correlation-id", str(uuid.uuid4()))
    correlation_id_var.set(cid)

    exempt_paths = {"/health", "/ready", "/openapi.json", "/docs", "/docs/oauth2-redirect"}
    if request.url.path in exempt_paths:
        response = await call_next(request)
        response.headers["X-Correlation-Id"] = cid
        return response

    settings = get_settings()

    if request.url.path.startswith("/api/v1"):
        if settings.AUTH_DISABLED and settings.TEST_MODE:
            pass
        else:
            auth_header = request.headers.get("authorization", "")
            if not auth_header:
                response = JSONResponse(
                    status_code=401,
                    content=api_error(
                        "UNAUTHORIZED", "Valid bearer token required.", status=401
                    ).as_payload(),
                )
                response.headers["WWW-Authenticate"] = "Bearer"
                response.headers["X-Correlation-Id"] = cid
                return response

            scheme, _, token = auth_header.partition(" ")
            if scheme.lower() != "bearer" or not token:
                response = JSONResponse(
                    status_code=401,
                    content=api_error(
                        "UNAUTHORIZED", "Valid bearer token required.", status=401
                    ).as_payload(),
                )
                response.headers["WWW-Authenticate"] = "Bearer"
                response.headers["X-Correlation-Id"] = cid
                return response

            if not hmac.compare_digest(token, settings.API_TOKEN):
                response = JSONResponse(
                    status_code=401,
                    content=api_error(
                        "UNAUTHORIZED", "Valid bearer token required.", status=401
                    ).as_payload(),
                )
                response.headers["WWW-Authenticate"] = "Bearer"
                response.headers["X-Correlation-Id"] = cid
                return response

    if settings.RATE_LIMIT_PER_MINUTE > 0:
        client_key = request.headers.get(
            "x-forwarded-for", request.client.host if request.client else "unknown"
        )
        bucket = _RATE_BUCKETS[client_key]
        now = time.monotonic()
        bucket.append(now)
        while bucket and now - bucket[0] > 60:
            bucket.popleft()
        if len(bucket) > settings.RATE_LIMIT_PER_MINUTE:
            body = {
                "error": {
                    "code": "RATE_LIMITED",
                    "message": "Rate limit exceeded.",
                    "retryable": False,
                    "details": None,
                }
            }
            response = JSONResponse(status_code=429, content=body)
            response.headers["X-Correlation-Id"] = cid
            return response

    response = await call_next(request)
    response.headers["X-Correlation-Id"] = cid
    return response


@app.exception_handler(APIError)
async def api_error_handler(request: Request, exc: APIError) -> JSONResponse:
    """Standard application-defined error payloads."""
    del request
    response = JSONResponse(status_code=exc.status, content=exc.as_payload())
    if exc.status == 401:
        response.headers["WWW-Authenticate"] = "Bearer"
    return response


@app.exception_handler(RequestValidationError)
async def validation_error_handler(request: Request, exc: RequestValidationError) -> JSONResponse:
    """Normalise request validation errors to the API contract."""
    del request
    details = {"errors": exc.errors()}
    return JSONResponse(
        status_code=422,
        content={
            "error": {
                "code": "VALIDATION_ERROR",
                "message": "Request validation failed.",
                "retryable": False,
                "details": details,
            }
        },
    )


@app.exception_handler(StarletteHTTPException)
async def http_exception_handler(request: Request, exc: StarletteHTTPException) -> JSONResponse:
    """Ensure HTTPException responses also follow the public error structure."""
    del request
    code = exc.status_code
    if code == 401:
        payload = api_error("UNAUTHORIZED", "Valid bearer token required.", status=401)
    elif code == 404:
        payload = api_error("NOT_FOUND", "The requested resource was not found.", status=404)
    elif code == 413:
        payload = api_error(
            "PAYLOAD_TOO_LARGE", "Payload exceeds the configured size limit.", status=413
        )
    elif code == 429:
        payload = api_error("RATE_LIMITED", "Rate limit exceeded.", status=429)
    else:
        payload = api_error("INTERNAL_ERROR", "An unexpected error occurred.", status=500)
    response = JSONResponse(status_code=code, content=payload.as_payload())
    if code == 401:
        response.headers["WWW-Authenticate"] = "Bearer"
    return response


@app.exception_handler(Exception)
async def global_exception_handler(request: Request, exc: Exception) -> JSONResponse:
    """Catch uncaught exceptions and preserve the standard error envelope."""
    del request
    log.error("unhandled_exception", error_type=type(exc).__name__, message=str(exc), exc_info=True)
    return JSONResponse(
        status_code=500,
        content=api_error(
            "INTERNAL_ERROR", "An unexpected error occurred.", retryable=True
        ).as_payload(),
    )


@api_router.get("/videos/{video_id}")
async def get_video(video_id: str) -> dict[str, Any]:
    """Read the persisted video metadata record."""
    with get_session_factory()() as session:
        record = session.get(VideoRecord, video_id)
        if record is None:
            raise api_error("NOT_FOUND", f"Video '{video_id}' was not found.", status=404)
        return {
            "id": record.id,
            "sha256": record.sha256,
            "duration_s": record.duration_s,
            "fps": record.fps,
            "has_audio": record.has_audio,
            "status": record.status,
            "warnings": record.warnings,
            "source_key": record.source_key,
        }


@api_router.get("/videos/{video_id}/transcript")
async def get_transcript(video_id: str) -> dict[str, Any]:
    """Return the transcript stored for a source video."""
    with get_session_factory()() as session:
        record = session.get(TranscriptRecord, video_id)
        if record is None:
            raise api_error(
                "NOT_FOUND", f"Transcript for video '{video_id}' was not found.", status=404
            )
        return {"video_id": video_id, "transcript": record.data}


@api_router.post("/videos")
async def create_video(request: Request) -> JSONResponse:
    """Queue an intake/transcription job from a multipart upload or public URL."""
    settings = get_settings()
    payload: dict[str, Any] = {}
    form = None
    try:
        form = await request.form()
    except Exception:
        form = None
    if form is not None:
        uploaded = form.get("file")
        if uploaded is not None:
            content = await uploaded.read()
            if len(content) > settings.MAX_UPLOAD_BYTES:
                raise api_error(
                    "PAYLOAD_TOO_LARGE",
                    "Uploaded file exceeds the configured upload limit.",
                    status=413,
                )
            suffix = Path(str(uploaded.filename or "upload.mp4")).suffix or ".mp4"
            fd, tmp_path = tempfile.mkstemp(suffix=suffix)
            os.close(fd)
            with open(tmp_path, "wb") as handle:
                handle.write(content)
            payload = {
                "video_id": f"vid_{uuid.uuid4().hex[:12]}",
                "local_path": tmp_path,
                "filename": uploaded.filename or "upload.mp4",
            }
        else:
            url = form.get("url")
            if url:
                payload = {"url": str(url)}
    else:
        try:
            payload = await request.json()
        except Exception as exc:
            raise api_error(
                "VALIDATION_ERROR",
                "Expected multipart upload or JSON body.",
                status=422,
                details={"error": str(exc)},
            ) from exc

    if not payload:
        raise api_error("VALIDATION_ERROR", "Either a file upload or URL is required.", status=422)
    if "url" in payload and not payload.get("url"):
        raise api_error("VALIDATION_ERROR", "The request URL cannot be empty.", status=422)

    queue = JobQueue(
        max_attempts=settings.JOB_MAX_ATTEMPTS, heartbeat_stale_s=settings.JOB_HEARTBEAT_STALE_S
    )
    job = queue.enqueue("intake_and_transcribe", payload)
    return JSONResponse(status_code=202, content={"job_id": job.id})


@api_router.post("/sessions")
async def create_session(request: Request) -> dict[str, Any]:
    """Create a session bound to a previously ingested video."""
    body = await request.json()
    video_id = body.get("video_id")
    if not video_id:
        raise api_error(
            "VALIDATION_ERROR", "A video_id is required to create a session.", status=422
        )
    repo = SessionRepository()
    loaded = repo.create_session(video_id)
    return {
        "session_id": loaded.session_id,
        "video_id": loaded.video_id,
        "version": loaded.version,
    }


@api_router.get("/sessions/{session_id}")
async def get_session(session_id: str) -> dict[str, Any]:
    """Return the current session summary and timeline hash."""
    repo = SessionRepository()
    try:
        loaded = repo.load_session(session_id)
    except SessionNotFoundError as exc:
        raise api_error("NOT_FOUND", str(exc), status=404) from exc
    return {
        "session_id": loaded.session_id,
        "video_id": loaded.video_id,
        "version": loaded.version,
        "timeline_hash": loaded.manager.get_timeline_hash(),
        "duration_s": round(
            loaded.manager.get_duration_frames() / loaded.manager.state.source.fps, 3
        ),
    }


@api_router.post("/sessions/{session_id}/instructions")
async def post_instruction(session_id: str, request: Request) -> dict[str, Any]:
    """Apply a natural-language instruction to a session and persist the new state."""
    body = await request.json()
    if not isinstance(body, dict):
        raise api_error(
            "VALIDATION_ERROR", "Instruction payload must be a JSON object.", status=422
        )
    if "say" in body:
        text = str(body["say"]).strip()
        answer = None
    elif "answer" in body:
        text = str(body["answer"]).strip()
        answer = text
    else:
        raise api_error("VALIDATION_ERROR", "Expected either 'say' or 'answer'.", status=422)
    if not text:
        raise api_error("VALIDATION_ERROR", "Instruction text cannot be empty.", status=422)

    repo = SessionRepository()
    try:
        loaded = repo.load_session(session_id)
    except SessionNotFoundError as exc:
        raise api_error("NOT_FOUND", str(exc), status=404) from exc

    try:
        result = run_instruction(loaded.manager, text, answer=answer)
        repo.save_session(session_id, loaded.manager, loaded.version)
        return result
    except ConcurrencyError as exc:
        raise api_error("SESSION_CONFLICT", str(exc), status=409, retryable=True) from exc


@api_router.get("/sessions/{session_id}/timeline")
async def get_timeline(session_id: str) -> dict[str, Any]:
    """Return the current timeline projection and hash."""
    repo = SessionRepository()
    try:
        loaded = repo.load_session(session_id)
    except SessionNotFoundError as exc:
        raise api_error("NOT_FOUND", str(exc), status=404) from exc
    return loaded.manager.get_timeline_output()


@api_router.get("/sessions/{session_id}/history")
async def get_history(session_id: str) -> dict[str, Any]:
    """Return the active and redo stacks with their instructions."""
    repo = SessionRepository()
    try:
        loaded = repo.load_session(session_id)
    except SessionNotFoundError as exc:
        raise api_error("NOT_FOUND", str(exc), status=404) from exc
    return loaded.manager.get_history()


@api_router.post("/sessions/{session_id}/render")
async def create_render_job(session_id: str) -> JSONResponse:
    """Queue a render job for a session. Rendering is a later phase; jobs fail cleanly."""
    settings = get_settings()
    queue = JobQueue(
        max_attempts=settings.JOB_MAX_ATTEMPTS, heartbeat_stale_s=settings.JOB_HEARTBEAT_STALE_S
    )
    job = queue.enqueue("render", {"session_id": session_id})
    return JSONResponse(status_code=202, content={"job_id": job.id})


@api_router.get("/jobs/{job_id}")
async def get_job(job_id: str) -> dict[str, Any]:
    """Read the current state of a background job."""
    queue = JobQueue(
        max_attempts=get_settings().JOB_MAX_ATTEMPTS,
        heartbeat_stale_s=get_settings().JOB_HEARTBEAT_STALE_S,
    )
    job = queue.get(job_id)
    if job is None:
        raise api_error("NOT_FOUND", f"Job '{job_id}' was not found.", status=404)
    return {
        "job_id": job.id,
        "type": job.type,
        "status": job.status,
        "progress": job.progress,
        "result": job.result,
        "error": job.error,
        "attempts": job.attempts,
        "max_attempts": job.max_attempts,
    }


@app.get("/health")
async def health() -> dict[str, str]:
    """Process liveness endpoint."""
    return {"status": "ok"}


@app.get("/ready")
async def ready() -> Response:
    """Readiness check for Postgres and MinIO."""
    settings = get_settings()
    errors: list[str] = []
    try:
        from sqlalchemy import create_engine, text

        engine = create_engine(settings.DATABASE_URL, pool_pre_ping=True)
        with engine.connect() as connection:
            connection.execute(text("SELECT 1"))
        engine.dispose()
    except Exception as exc:  # pragma: no cover - coverage not needed here
        errors.append(f"postgres: {exc}")
    try:
        import boto3
        from botocore.client import Config as BotoConfig

        s3 = boto3.client(
            "s3",
            endpoint_url=settings.S3_ENDPOINT,
            aws_access_key_id=settings.S3_ACCESS_KEY,
            aws_secret_access_key=settings.S3_SECRET_KEY,
            region_name=settings.S3_REGION,
            config=BotoConfig(signature_version="s3v4"),
        )
        s3.head_bucket(Bucket=settings.S3_BUCKET)
    except Exception as exc:  # pragma: no cover - coverage not needed here
        errors.append(f"minio: {exc}")
    if errors:
        details = {"checks": errors}
        return JSONResponse(
            status_code=503,
            content={
                "status": "unavailable",
                "errors": errors,
                "error": {
                    "code": "DATABASE_UNAVAILABLE",
                    "message": "A required dependency is unavailable.",
                    "retryable": True,
                    "details": details,
                },
            },
        )
    return JSONResponse(content={"status": "ready"})


app.include_router(api_router)

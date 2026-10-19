"""HTTP integration tests using the real FastAPI app, Postgres, and MinIO."""

from __future__ import annotations

import hashlib
import hmac
import json
import secrets
import socket
import threading
import uuid
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from datetime import timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select, text
from sqlalchemy.engine import make_url

from incubrix.adapters.storage.s3 import S3Storage, compute_file_sha256
from incubrix.config import Settings, get_settings
from incubrix.db.engine import get_engine, get_session_factory, run_migrations
from incubrix.db.mapping import source_info_to_video_kwargs, transcript_to_record_kwargs
from incubrix.db.models import EditRecord, JobRecord, TranscriptRecord, VideoRecord
from incubrix.db.repository import SessionRepository
from incubrix.engine.models import Sentence, SourceInfo, Transcript, Word
from incubrix.jobs.queue import JobQueue, utc_now
from incubrix.jobs.worker import Worker

pytestmark = pytest.mark.integration


@pytest.fixture(scope="session", autouse=True)
def real_postgres_and_minio() -> None:
    """Require and initialize the actual integration database and object store."""
    settings = Settings()
    run_migrations(settings.DATABASE_URL)
    with get_engine(settings.DATABASE_URL).connect() as connection:
        connection.execute(text("SELECT 1"))
    S3Storage(settings).ensure_bucket()


@pytest.fixture
def api_app(monkeypatch: pytest.MonkeyPatch) -> tuple[Any, dict[str, str]]:
    """Start the real app with a configured token and its auth bypass disabled."""
    token = secrets.token_urlsafe(32)
    monkeypatch.setenv("API_TOKEN", token)
    monkeypatch.setenv("AUTH_DISABLED", "false")
    monkeypatch.setenv("TEST_MODE", "false")

    settings = get_settings()
    assert settings.API_TOKEN == token

    from incubrix.api.app import app

    return app, {
        **{"Authorization": f"Bearer {settings.API_TOKEN}"},
        "X-Forwarded-For": f"test-client-{uuid.uuid4().hex}",
    }


@pytest.fixture
def http_client(api_app: tuple[Any, dict[str, str]]):
    """Provide a real-app TestClient with a settings-backed bearer token."""
    app, headers = api_app

    class IdempotentTestClient(TestClient):
        def post(self, url, *args, **kwargs):
            request_headers = dict(kwargs.get("headers") or {})
            if "idempotency-key" not in {name.lower() for name in request_headers}:
                request_headers["Idempotency-Key"] = f"test-{uuid.uuid4().hex}"
            kwargs["headers"] = request_headers
            return super().post(url, *args, **kwargs)

    with IdempotentTestClient(
        app, headers={"X-Forwarded-For": headers["X-Forwarded-For"]}
    ) as client:
        yield client, headers


def _seed_video() -> str:
    """Insert a deterministic-shaped source and transcript into real Postgres."""
    video_id = f"vid_http_{uuid.uuid4().hex}"
    source = SourceInfo(
        sha256=hashlib.sha256(video_id.encode("utf-8")).hexdigest(),
        duration_frames=120 * 30,
        fps=30.0,
        has_audio=True,
    )
    words = [
        Word(id=f"w{index:04d}", text=word, start_s=float(index), end_s=index + 0.5)
        for index, word in enumerate(("Welcome", "to", "this", "video", "today"))
    ]
    transcript = Transcript(
        words=words,
        sentences=[Sentence(first_word_idx=0, last_word_idx=len(words) - 1)],
        speakers=["host"],
    )
    with get_session_factory().begin() as session:
        session.add(
            VideoRecord(
                **source_info_to_video_kwargs(
                    source, video_id=video_id, source_key=f"sources/{video_id}.mp4"
                )
            )
        )
        session.add(TranscriptRecord(**transcript_to_record_kwargs(transcript, video_id=video_id)))
    return video_id


def _create_session(client: TestClient, headers: dict[str, str]) -> str:
    video_id = _seed_video()
    response = client.post("/api/v1/sessions", json={"video_id": video_id}, headers=headers)
    assert response.status_code == 200, response.text
    return response.json()["session_id"]


def _assert_standard_error(response, status: int, code: str, retryable: bool | None = None) -> None:
    assert response.status_code == status, response.text
    error = response.json()["error"]
    assert error["code"] == code
    assert set(error) == {"code", "message", "retryable", "details"}
    if retryable is not None:
        assert error["retryable"] is retryable


def _closed_local_port() -> int:
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        return listener.getsockname()[1]


class _SpecificJobQueue(JobQueue):
    """Claim only the job under test, leaving shared integration jobs alone."""

    def __init__(self, job_id: str) -> None:
        super().__init__()
        self.job_id = job_id

    def claim(self, worker_id: str, now=None):
        current = now or utc_now()
        with self._sessions.begin() as session:
            job = session.execute(
                select(JobRecord)
                .where(JobRecord.id == self.job_id, JobRecord.status == "queued")
                .with_for_update(skip_locked=True)
            ).scalar_one_or_none()
            if job is None:
                return None
            job.status = "running"
            job.attempts += 1
            job.locked_by = worker_id
            job.heartbeat_at = current
            job.started_at = job.started_at or current
            session.flush()
            session.expunge(job)
            return job


def _pause_enqueued_jobs(monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep the Compose worker from racing this test's explicitly-run worker."""

    def enqueue_paused(queue, job_type, payload, callback_url=None):
        current = utc_now()
        record = JobRecord(
            id=str(uuid.uuid4()),
            type=job_type,
            payload=payload,
            callback_url=callback_url,
            max_attempts=queue._max_attempts,
            available_at=current + timedelta(hours=1),
            created_at=current,
        )
        with queue._sessions.begin() as session:
            session.add(record)
            session.flush()
            session.expunge(record)
        return record

    monkeypatch.setattr(JobQueue, "enqueue", enqueue_paused)


def _run_specific_job(job_id: str, settings: Settings, handlers: dict[str, Any]) -> None:
    worker = Worker(
        settings=settings,
        queue=_SpecificJobQueue(job_id),
        handlers=handlers,
        worker_id=f"http-test-{uuid.uuid4().hex[:8]}",
    )
    assert worker.run_once() is True


def _get_job(client: TestClient, headers: dict[str, str], job_id: str) -> dict[str, Any]:
    response = client.get(f"/api/v1/jobs/{job_id}", headers=headers)
    assert response.status_code == 200, response.text
    return response.json()


@contextmanager
def _callback_endpoint(status_code: int):
    received: list[tuple[bytes, str | None]] = []

    class CallbackHandler(BaseHTTPRequestHandler):
        def do_POST(self):  # noqa: N802
            body = self.rfile.read(int(self.headers.get("content-length", "0")))
            received.append((body, self.headers.get("X-IncuBrix-Signature")))
            self.send_response(status_code)
            self.end_headers()

        def log_message(self, format: str, *args: Any) -> None:
            del format, args

    server = ThreadingHTTPServer(("127.0.0.1", 0), CallbackHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}/callback", received
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def _create_callback_job(
    client: TestClient,
    headers: dict[str, str],
    callback_url: str,
) -> str:
    response = client.post(
        "/api/v1/videos",
        json={"url": "https://callback-job.invalid/video.mp4", "callback_url": callback_url},
        headers=headers,
    )
    assert response.status_code == 202, response.text
    return response.json()["job_id"]


def _generate_tiny_video(destination: Path) -> None:
    from tests.media import generate_video

    generate_video(destination, duration_s=300)


def _assert_caption_time(timeline: dict[str, Any], start_s: float, end_s: float) -> None:
    caption = next(item for item in timeline["overlay_displays"] if item["text"] == "Welcome!")
    assert caption["start_s"] == pytest.approx(start_s, abs=0.1)
    assert caption["end_s"] == pytest.approx(end_s, abs=0.1)


def test_instruction_idempotency_replays_response_and_applies_once(http_client) -> None:
    client, headers = http_client
    session_id = _create_session(client, headers)
    url = f"/api/v1/sessions/{session_id}/instructions"
    request_body = {"say": "Cut the first 5 seconds"}
    request_headers = {**headers, "Idempotency-Key": f"instruction-{uuid.uuid4().hex}"}

    first = client.post(url, json=request_body, headers=request_headers)
    replay = client.post(url, json=request_body, headers=request_headers)

    assert first.status_code == 200, first.text
    assert replay.status_code == first.status_code
    assert replay.content == first.content
    history = client.get(f"/api/v1/sessions/{session_id}/history", headers=headers)
    assert history.status_code == 200, history.text
    assert len(history.json()["stack"]) == 1


def test_idempotency_rejects_reused_key_with_different_instruction_body(http_client) -> None:
    client, headers = http_client
    session_id = _create_session(client, headers)
    url = f"/api/v1/sessions/{session_id}/instructions"
    key = f"changed-body-{uuid.uuid4().hex}"

    first = client.post(
        url,
        json={"say": "Cut the first 5 seconds"},
        headers={**headers, "Idempotency-Key": key},
    )
    changed = client.post(
        url,
        json={"say": "Cut the last 5 seconds"},
        headers={**headers, "Idempotency-Key": key},
    )

    assert first.status_code == 200, first.text
    _assert_standard_error(changed, 409, "IDEMPOTENCY_KEY_REUSED")


def test_video_intake_idempotency_returns_same_job_without_repeating_work(http_client) -> None:
    client, headers = http_client
    url = f"https://example.invalid/{uuid.uuid4().hex}.mp4"
    key = f"video-{uuid.uuid4().hex}"
    request_headers = {**headers, "Idempotency-Key": key}

    first = client.post("/api/v1/videos", json={"url": url}, headers=request_headers)
    replay = client.post("/api/v1/videos", json={"url": url}, headers=request_headers)

    assert first.status_code == 202, first.text
    assert replay.status_code == first.status_code
    assert replay.content == first.content
    job_id = first.json()["job_id"]
    with get_session_factory()() as session:
        job_count = session.scalar(
            select(func.count())
            .select_from(JobRecord)
            .where(
                JobRecord.type == "intake_and_transcribe",
                JobRecord.payload["url"].as_string() == url,
            )
        )
    assert job_count == 1
    assert replay.json()["job_id"] == job_id


@pytest.mark.parametrize(
    ("url", "resolved_address"),
    [
        ("https://10.23.4.5/video.mp4", "10.23.4.5"),
        ("https://127.0.0.1/video.mp4", "127.0.0.1"),
        ("https://169.254.10.20/video.mp4", "169.254.10.20"),
        ("http://public-url.test/video.mp4", "93.184.216.34"),
    ],
    ids=["private-ip", "loopback", "link-local", "non-https"],
)
def test_video_intake_rejects_unsafe_urls_without_outbound_requests(
    http_client, monkeypatch: pytest.MonkeyPatch, url: str, resolved_address: str
) -> None:
    client, headers = http_client
    _pause_enqueued_jobs(monkeypatch)
    outbound_requests: list[str] = []
    resolver_calls: list[tuple[str, int]] = []

    def fake_resolver(host, port, *args, **kwargs):
        resolver_calls.append((host, port))
        return [
            (socket.AF_INET, socket.SOCK_STREAM, socket.IPPROTO_TCP, "", (resolved_address, port))
        ]

    class NoRequestClient:
        def __init__(self, *args, **kwargs):
            del args, kwargs

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return None

        def stream(self, method: str, requested_url: str):
            outbound_requests.append(requested_url)
            raise AssertionError("unsafe intake attempted an HTTP request")

    monkeypatch.setattr("incubrix.jobs.handlers.socket.getaddrinfo", fake_resolver)
    monkeypatch.setattr("incubrix.jobs.handlers.httpx.Client", NoRequestClient)

    submitted = client.post("/api/v1/videos", json={"url": url}, headers=headers)
    assert submitted.status_code == 202, submitted.text
    job_id = submitted.json()["job_id"]

    from incubrix.jobs.handlers import build_handlers

    settings = Settings()
    _run_specific_job(job_id, settings, build_handlers(settings))
    job = _get_job(client, headers, job_id)

    assert job["status"] == "failed", (job, resolver_calls)
    assert job["error"]["code"] == "INVALID_URL"
    assert job["error"]["retryable"] is False
    assert set(job["error"]) == {"code", "message", "retryable", "details"}
    assert outbound_requests == []


def test_video_intake_rejects_redirect_to_link_local_before_fetching_redirect(
    http_client, monkeypatch: pytest.MonkeyPatch
) -> None:
    client, headers = http_client
    _pause_enqueued_jobs(monkeypatch)
    source_url = "https://public-redirect.test/video.mp4"
    redirect_url = "https://169.254.169.254/latest/meta-data"
    transport_requests: list[str] = []

    def fake_resolver(host, port, *args, **kwargs):
        address = "93.184.216.34" if host == "public-redirect.test" else "169.254.169.254"
        return [(socket.AF_INET, socket.SOCK_STREAM, socket.IPPROTO_TCP, "", (address, port))]

    class RedirectResponse:
        status_code = 302
        headers = {"location": redirect_url}

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return None

    class FakeClient:
        def __init__(self, *args, **kwargs):
            del args, kwargs

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return None

        def stream(self, method: str, url: str):
            assert method == "GET"
            transport_requests.append(url)
            return RedirectResponse()

    monkeypatch.setattr("incubrix.jobs.handlers.socket.getaddrinfo", fake_resolver)
    monkeypatch.setattr("incubrix.jobs.handlers.httpx.Client", FakeClient)
    submitted = client.post("/api/v1/videos", json={"url": source_url}, headers=headers)
    assert submitted.status_code == 202, submitted.text
    job_id = submitted.json()["job_id"]

    from incubrix.jobs.handlers import build_handlers

    settings = Settings()
    _run_specific_job(job_id, settings, build_handlers(settings))
    job = _get_job(client, headers, job_id)

    assert job["status"] == "failed"
    assert job["error"]["code"] == "INVALID_URL"
    assert set(job["error"]) == {"code", "message", "retryable", "details"}
    assert transport_requests == [source_url]


def test_http_callback_signature_matches_exact_raw_body(http_client, monkeypatch) -> None:
    client, headers = http_client
    secret = "http-callback-signing-secret"
    monkeypatch.setenv("CALLBACK_HMAC_SECRET", secret)
    _pause_enqueued_jobs(monkeypatch)

    with _callback_endpoint(204) as (callback_url, received):
        job_id = _create_callback_job(client, headers, callback_url)
        settings = Settings()
        _run_specific_job(
            job_id,
            settings,
            {"intake_and_transcribe": lambda payload, progress: {"ok": True}},
        )

    job = _get_job(client, headers, job_id)
    assert job["status"] == "succeeded"
    assert len(received) == 1
    raw_body, signature = received[0]
    expected = hmac.new(secret.encode("utf-8"), raw_body, hashlib.sha256).hexdigest()
    assert hmac.compare_digest(signature or "", expected)
    assert json.loads(raw_body)["job_id"] == job_id


def test_http_callback_failures_retry_log_and_do_not_fail_job(
    http_client, monkeypatch, caplog
) -> None:
    client, headers = http_client
    monkeypatch.setenv("CALLBACK_HMAC_SECRET", "http-callback-failure-secret")
    monkeypatch.setenv("CALLBACK_MAX_RETRIES", "2")
    monkeypatch.setenv("CALLBACK_BACKOFF_S", "0")
    _pause_enqueued_jobs(monkeypatch)
    caplog.set_level("WARNING", logger="incubrix.jobs.worker")

    with _callback_endpoint(503) as (callback_url, received):
        job_id = _create_callback_job(client, headers, callback_url)
        settings = Settings()
        _run_specific_job(
            job_id,
            settings,
            {"intake_and_transcribe": lambda payload, progress: {"ok": True}},
        )

    job = _get_job(client, headers, job_id)
    assert job["status"] == "succeeded"
    assert job["error"] is None
    assert len(received) == 2
    assert sum("job_callback_failed" in record.getMessage() for record in caplog.records) == 2


def test_http_unauthorized_error_shape(http_client) -> None:
    client, _ = http_client
    response = client.get("/api/v1/jobs/missing")

    _assert_standard_error(response, 401, "UNAUTHORIZED", retryable=False)


def test_http_not_found_error_shape(http_client) -> None:
    client, headers = http_client
    response = client.get("/api/v1/jobs/missing", headers=headers)

    _assert_standard_error(response, 404, "NOT_FOUND", retryable=False)


def test_http_method_not_allowed_error_shape(http_client) -> None:
    client, headers = http_client
    response = client.post("/api/v1/jobs/missing", headers=headers)

    _assert_standard_error(response, 405, "METHOD_NOT_ALLOWED", retryable=False)


def test_http_oversized_upload_error_shape(http_client, monkeypatch) -> None:
    client, headers = http_client
    monkeypatch.setenv("MAX_UPLOAD_BYTES", "8")
    response = client.post(
        "/api/v1/videos",
        files={"file": ("too-large.mp4", b"x" * 32, "video/mp4")},
        headers=headers,
    )

    _assert_standard_error(response, 413, "PAYLOAD_TOO_LARGE", retryable=False)


def test_http_rate_limit_error_shape(http_client, monkeypatch) -> None:
    client, headers = http_client
    monkeypatch.setenv("RATE_LIMIT_PER_MINUTE", "1")
    client_ip = f"198.51.100.{uuid.uuid4().int % 250 + 1}"
    request_headers = {**headers, "X-Forwarded-For": client_ip}

    first = client.get("/api/v1/jobs/missing", headers=request_headers)
    limited = client.get("/api/v1/jobs/missing", headers=request_headers)

    _assert_standard_error(first, 404, "NOT_FOUND", retryable=False)
    _assert_standard_error(limited, 429, "RATE_LIMITED", retryable=False)


def test_http_route_matrix_success_and_failure(http_client, monkeypatch) -> None:
    client, headers = http_client
    _pause_enqueued_jobs(monkeypatch)
    video_id = _seed_video()

    assert client.get("/health").status_code == 200
    assert client.get("/ready").status_code == 200
    assert client.get(f"/api/v1/videos/{video_id}", headers=headers).status_code == 200
    assert client.get(f"/api/v1/videos/{video_id}/transcript", headers=headers).status_code == 200
    _assert_standard_error(
        client.get("/api/v1/videos/does-not-exist", headers=headers), 404, "NOT_FOUND"
    )
    _assert_standard_error(
        client.get("/api/v1/videos/does-not-exist/transcript", headers=headers),
        404,
        "NOT_FOUND",
    )
    _assert_standard_error(
        client.post("/api/v1/videos", json={}, headers=headers), 422, "VALIDATION_ERROR"
    )
    queued_video = client.post(
        "/api/v1/videos",
        json={"url": "https://route-matrix.invalid/video.mp4"},
        headers=headers,
    )
    assert queued_video.status_code == 202, queued_video.text

    _assert_standard_error(
        client.post("/api/v1/sessions", json={}, headers=headers), 422, "VALIDATION_ERROR"
    )
    created = client.post("/api/v1/sessions", json={"video_id": video_id}, headers=headers)
    assert created.status_code == 200, created.text
    session_id = created.json()["session_id"]
    assert client.get(f"/api/v1/sessions/{session_id}", headers=headers).status_code == 200
    assert client.get(f"/api/v1/sessions/{session_id}/timeline", headers=headers).status_code == 200
    assert client.get(f"/api/v1/sessions/{session_id}/history", headers=headers).status_code == 200
    assert (
        client.post(
            f"/api/v1/sessions/{session_id}/instructions",
            json={"say": "Cut the first 5 seconds"},
            headers=headers,
        ).status_code
        == 200
    )
    _assert_standard_error(
        client.post(f"/api/v1/sessions/{session_id}/instructions", json={}, headers=headers),
        422,
        "VALIDATION_ERROR",
    )
    for suffix in ("", "/timeline", "/history"):
        _assert_standard_error(
            client.get(f"/api/v1/sessions/missing{suffix}", headers=headers),
            404,
            "NOT_FOUND",
        )
    _assert_standard_error(
        client.post("/api/v1/sessions/missing/instructions", json={"say": "Undo"}, headers=headers),
        404,
        "NOT_FOUND",
    )
    _assert_standard_error(
        client.post("/api/v1/sessions/missing/render", headers=headers), 404, "NOT_FOUND"
    )
    render_job = client.post(f"/api/v1/sessions/{session_id}/render", headers=headers)
    assert render_job.status_code == 202, render_job.text
    assert (
        client.get(f"/api/v1/jobs/{render_job.json()['job_id']}", headers=headers).status_code
        == 200
    )
    assert (
        client.get(f"/api/v1/jobs/{queued_video.json()['job_id']}", headers=headers).status_code
        == 200
    )
    _assert_standard_error(client.get("/api/v1/jobs/missing", headers=headers), 404, "NOT_FOUND")


def test_http_url_intake_success_without_external_network(
    tmp_path, http_client, monkeypatch
) -> None:
    client, headers = http_client
    _pause_enqueued_jobs(monkeypatch)
    monkeypatch.setenv("STT_PROVIDER", "mock")
    monkeypatch.setenv("STT_MOCK_TRANSCRIPT_PATH", "")
    source_path = tmp_path / "url-source.mp4"
    from tests.media import generate_video

    generate_video(source_path)
    content = source_path.read_bytes()

    class Response:
        status_code = 200
        headers = {"content-length": str(len(content))}

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return None

        def raise_for_status(self):
            return None

        def iter_bytes(self):
            yield content

    class Client:
        def __init__(self, *args, **kwargs):
            del args, kwargs

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return None

        def stream(self, method: str, url: str):
            assert method == "GET"
            assert url == "https://offline-intake.invalid/video.mp4"
            return Response()

    monkeypatch.setattr("incubrix.jobs.handlers._validate_public_https_url", lambda url: None)
    monkeypatch.setattr("incubrix.jobs.handlers.httpx.Client", Client)
    response = client.post(
        "/api/v1/videos",
        json={"url": "https://offline-intake.invalid/video.mp4"},
        headers=headers,
    )
    assert response.status_code == 202, response.text

    from incubrix.jobs.handlers import build_handlers

    job_id = response.json()["job_id"]
    _run_specific_job(job_id, Settings(), build_handlers(Settings()))
    completed = _get_job(client, headers, job_id)
    assert completed["status"] == "succeeded", completed
    assert completed["progress"] == 100
    assert completed["result"]["manifest"]["key"] == f"manifests/jobs/{job_id}.json"
    assert (
        client.get(f"/api/v1/videos/{completed['result']['video_id']}", headers=headers).status_code
        == 200
    )


def test_http_job_lifecycle_and_json_log_contract(http_client, monkeypatch, caplog) -> None:
    client, headers = http_client
    _pause_enqueued_jobs(monkeypatch)
    caplog.set_level("INFO")
    caplog.clear()
    correlation_id = f"test-correlation-{uuid.uuid4().hex}"
    accepted = client.post(
        "/api/v1/videos",
        json={"url": "https://lifecycle.invalid/video.mp4"},
        headers={**headers, "X-Correlation-Id": correlation_id},
    )
    assert accepted.status_code == 202, accepted.text
    job_id = accepted.json()["job_id"]

    started = threading.Event()
    release = threading.Event()

    def handler(payload, progress):
        del payload
        progress(37)
        started.set()
        assert release.wait(timeout=15)
        return {"download_url": "https://downloads.invalid/result.mp4"}

    worker_error: list[BaseException] = []

    def run_worker() -> None:
        try:
            _run_specific_job(job_id, Settings(), {"intake_and_transcribe": handler})
        except BaseException as exc:
            worker_error.append(exc)

    thread = threading.Thread(target=run_worker)
    thread.start()
    try:
        assert started.wait(timeout=10)
        running = _get_job(client, headers, job_id)
        assert running["status"] == "running"
        assert running["progress"] == 37
    finally:
        release.set()
        thread.join(timeout=20)
    assert not thread.is_alive()
    assert worker_error == []
    succeeded = _get_job(client, headers, job_id)
    assert succeeded["status"] == "succeeded"
    assert succeeded["progress"] == 100
    assert succeeded["result"]["download_url"] == "https://downloads.invalid/result.mp4"

    failed_accept = client.post(
        "/api/v1/videos",
        json={"url": "https://failed-lifecycle.invalid/video.mp4"},
        headers=headers,
    )
    assert failed_accept.status_code == 202, failed_accept.text
    failed_job_id = failed_accept.json()["job_id"]

    def fail_handler(payload, progress):
        del payload, progress
        raise RuntimeError("test-only worker failure")

    for _ in range(Settings().JOB_MAX_ATTEMPTS):
        _run_specific_job(failed_job_id, Settings(), {"intake_and_transcribe": fail_handler})
    failed = _get_job(client, headers, failed_job_id)
    assert failed["status"] == "failed"
    assert failed["error"]["code"] == "INTERNAL_ERROR"

    health = client.get("/health")
    assert health.status_code == 200
    generated_id = health.headers["x-correlation-id"]
    records = [
        record.msg
        for record in caplog.records
        if isinstance(record.msg, dict) and "message" in record.msg
    ]
    assert records
    for record in records:
        assert {"timestamp", "level", "message"} <= set(record)
    request_records = [
        record for record in records if record["message"] == "http_request_completed"
    ]
    assert any(record.get("correlation_id") == correlation_id for record in request_records)
    assert any(record.get("correlation_id") == generated_id for record in request_records)
    job_record = next(record for record in records if record["message"] == "job_started")
    assert job_record["job_id"] == job_id
    assert job_record["correlation_id"] == correlation_id
    settings = Settings()
    serialized_logs = json.dumps(records)
    for secret in (
        headers["Authorization"].removeprefix("Bearer "),
        settings.API_TOKEN,
        settings.S3_ACCESS_KEY,
        settings.S3_SECRET_KEY,
        settings.LLM_API_KEY,
        settings.GROQ_API_KEY,
        settings.OPENAI_API_KEY,
        settings.HMAC_SECRET,
        settings.CALLBACK_HMAC_SECRET,
    ):
        if secret:
            assert secret not in serialized_logs


def test_http_validation_error_includes_field_details(http_client) -> None:
    client, headers = http_client
    response = client.post("/api/v1/sessions", json={}, headers=headers)

    _assert_standard_error(response, 422, "VALIDATION_ERROR", retryable=False)
    assert response.json()["error"]["details"]["field"] == "video_id"


def test_http_post_requires_idempotency_key(api_app) -> None:
    app, headers = api_app
    with TestClient(app) as client:
        response = client.post(
            "/api/v1/sessions",
            json={"video_id": "missing-video"},
            headers=headers,
        )

    _assert_standard_error(response, 422, "VALIDATION_ERROR", retryable=False)
    assert "Idempotency-Key" in response.json()["error"]["message"]


def test_http_internal_error_hides_traceback(api_app, monkeypatch) -> None:
    app, headers = api_app

    def fail_job_lookup(self, job_id):
        raise RuntimeError(f"test-only failure for {job_id}")

    monkeypatch.setattr(JobQueue, "get", fail_job_lookup)
    with TestClient(app, raise_server_exceptions=False) as client:
        response = client.get("/api/v1/jobs/existing", headers=headers)

    _assert_standard_error(response, 500, "INTERNAL_ERROR", retryable=True)
    assert "Traceback" not in response.text
    assert "test-only failure" not in response.text


def test_http_upload_intake_and_eight_step_worked_example(
    tmp_path, http_client, monkeypatch
) -> None:
    client, headers = http_client
    monkeypatch.setenv("STT_PROVIDER", "mock")
    monkeypatch.setenv("LLM_PROVIDER", "mock")
    monkeypatch.setenv("EMBEDDER_PROVIDER", "mock")

    from tests.unit.conftest import transcript as synthetic_transcript_fixture

    transcript = synthetic_transcript_fixture.__wrapped__()
    transcript_path = tmp_path / "worked-example-transcript.json"
    transcript_path.write_text(json.dumps(transcript.model_dump(mode="json")), encoding="utf-8")
    monkeypatch.setenv("STT_MOCK_TRANSCRIPT_PATH", str(transcript_path))

    source_path = tmp_path / "worked-example.mp4"
    _generate_tiny_video(source_path)
    original_sha256 = compute_file_sha256(source_path)
    _pause_enqueued_jobs(monkeypatch)

    uploaded = client.post(
        "/api/v1/videos",
        files={"file": (source_path.name, source_path.read_bytes(), "video/mp4")},
        headers=headers,
    )
    assert uploaded.status_code == 202, uploaded.text
    intake_job_id = uploaded.json()["job_id"]

    settings = Settings()
    from incubrix.jobs.handlers import build_handlers

    _run_specific_job(intake_job_id, settings, build_handlers(settings))
    intake_job = _get_job(client, headers, intake_job_id)
    assert intake_job["status"] == "succeeded", intake_job
    video_id = intake_job["result"]["video_id"]
    manifest_link = intake_job["result"]["manifest"]
    assert manifest_link["key"] == f"manifests/jobs/{intake_job_id}.json"
    assert manifest_link["url"].startswith("http")
    manifest_path = tmp_path / "intake-manifest.json"
    S3Storage(settings).get_to_path(manifest_link["key"], manifest_path)
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    assert manifest["job_id"] == intake_job_id
    assert set(manifest["providers"]) == {"stt", "llm", "embedder"}
    assert set(manifest["stage_timings_s"]) == {
        "download_s",
        "source_storage_s",
        "probe_s",
        "transcription_s",
        "persistence_s",
    }
    assert manifest["cost_estimate"]["currency"] == "USD"
    assert all(
        {"license", "commercial_use", "ai_generated"} <= set(asset) for asset in manifest["assets"]
    )
    manifest_text = json.dumps(manifest)
    for secret in (
        settings.API_TOKEN,
        settings.S3_ACCESS_KEY,
        settings.S3_SECRET_KEY,
        settings.LLM_API_KEY,
        settings.GROQ_API_KEY,
        settings.OPENAI_API_KEY,
        settings.HMAC_SECRET,
        settings.CALLBACK_HMAC_SECRET,
    ):
        if secret:
            assert secret not in manifest_text

    video_response = client.get(f"/api/v1/videos/{video_id}", headers=headers)
    transcript_response = client.get(f"/api/v1/videos/{video_id}/transcript", headers=headers)
    assert video_response.status_code == 200, video_response.text
    assert transcript_response.status_code == 200, transcript_response.text
    assert video_response.json()["sha256"] == original_sha256
    assert any(
        word["text"].casefold() == "welcome"
        for word in transcript_response.json()["transcript"]["words"]
    )

    source_verification = S3Storage(settings).verify_source(video_response.json())
    assert source_verification.is_valid is True
    assert source_verification.computed_sha256 == original_sha256
    assert compute_file_sha256(source_path) == original_sha256

    created = client.post("/api/v1/sessions", json={"video_id": video_id}, headers=headers)
    assert created.status_code == 200, created.text
    session_id = created.json()["session_id"]
    instruction_url = f"/api/v1/sessions/{session_id}/instructions"

    def say(instruction: str) -> dict[str, Any]:
        response = client.post(
            instruction_url,
            json={"say": instruction},
            headers=headers,
        )
        assert response.status_code == 200, response.text
        return response.json()

    def read_timeline() -> dict[str, Any]:
        response = client.get(f"/api/v1/sessions/{session_id}/timeline", headers=headers)
        assert response.status_code == 200, response.text
        return response.json()

    step_1 = say("Add a caption 'Welcome!' when I say welcome to the channel")
    assert step_1["status"] == "applied"
    _assert_caption_time(read_timeline(), 42.0, 43.9)

    step_2 = say("Cut the first 30 seconds")
    assert step_2["duration_s"] == pytest.approx(270.0, abs=0.1)
    _assert_caption_time(read_timeline(), 12.0, 13.9)

    clarification = say("Remove the part about pricing")
    assert clarification["status"] == "needs_clarification"
    assert "3" in clarification["question"]
    assert any("all" in str(option).casefold() for option in clarification["options"])
    answered = client.post(
        instruction_url,
        json={"answer": "the second one"},
        headers=headers,
    )
    assert answered.status_code == 200, answered.text
    step_3 = answered.json()
    assert step_3["status"] == "applied"
    assert step_3["duration_s"] == pytest.approx(265.0, abs=0.2)

    step_4 = say("Speed up from 1:00 to 1:30 by 2x")
    assert step_4["duration_s"] == pytest.approx(250.0, abs=0.2)

    step_5 = say("Actually, put the intro back")
    assert step_5["duration_s"] == pytest.approx(280.0, abs=0.2)
    _assert_caption_time(read_timeline(), 42.0, 43.9)

    step_6 = say("Undo")
    assert step_6["duration_s"] == pytest.approx(250.0, abs=0.2)
    _assert_caption_time(read_timeline(), 12.0, 13.9)

    step_7 = say("Make that caption red")
    assert step_7["status"] == "applied"
    step_8 = say("Render")
    assert step_8["status"] == "applied"
    assert any("no video was rendered" in warning.casefold() for warning in step_8["warnings"])

    final_timeline = read_timeline()
    _assert_caption_time(final_timeline, 12.0, 13.9)
    assert next(overlay for overlay in final_timeline["overlays"] if overlay["text"] == "Welcome!")[
        "style"
    ] == {"color": "red"}
    final_session = client.get(f"/api/v1/sessions/{session_id}", headers=headers)
    history = client.get(f"/api/v1/sessions/{session_id}/history", headers=headers)
    assert final_session.status_code == 200, final_session.text
    assert final_session.json()["duration_s"] == pytest.approx(250.0, abs=0.1)
    assert history.status_code == 200, history.text
    assert len(history.json()["stack"]) == 5
    assert history.json()["redo"] == []
    assert compute_file_sha256(source_path) == original_sha256


def test_concurrent_idempotent_instructions_have_one_effect(api_app) -> None:
    app, headers = api_app
    with TestClient(app) as setup_client:
        session_id = _create_session(
            setup_client,
            {**headers, "Idempotency-Key": f"setup-{uuid.uuid4().hex}"},
        )
    request_url = f"/api/v1/sessions/{session_id}/instructions"
    request_body = {"say": "Cut the first 5 seconds"}
    request_headers = {**headers, "Idempotency-Key": f"parallel-{uuid.uuid4().hex}"}
    start = threading.Barrier(2)

    def send_request():
        with TestClient(app) as client:
            start.wait(timeout=10)
            return client.post(request_url, json=request_body, headers=request_headers)

    with ThreadPoolExecutor(max_workers=2) as pool:
        responses = list(pool.map(lambda _: send_request(), range(2)))

    assert [response.status_code for response in responses] == [200, 200]
    assert responses[0].content == responses[1].content
    with TestClient(app) as client:
        history = client.get(f"/api/v1/sessions/{session_id}/history", headers=headers)
    assert history.status_code == 200, history.text
    assert len(history.json()["stack"]) == 1


def test_concurrent_session_instructions_return_conflict_and_preserve_history(
    api_app, monkeypatch: pytest.MonkeyPatch
) -> None:
    app, headers = api_app
    with TestClient(app) as setup_client:
        session_id = _create_session(
            setup_client,
            {**headers, "Idempotency-Key": f"setup-{uuid.uuid4().hex}"},
        )
    request_url = f"/api/v1/sessions/{session_id}/instructions"
    start = threading.Barrier(2)
    loaded_sessions = threading.Barrier(2)
    load_lock = threading.Lock()
    load_count = 0
    original_load = SessionRepository.load_session

    def synchronized_load(repository, requested_id, db=None):
        nonlocal load_count
        loaded = original_load(repository, requested_id, db)
        if requested_id == session_id:
            with load_lock:
                load_count += 1
                should_wait = load_count <= 2
            if should_wait:
                loaded_sessions.wait(timeout=10)
        return loaded

    monkeypatch.setattr(SessionRepository, "load_session", synchronized_load)

    def send_request(instruction: str):
        with TestClient(app) as client:
            start.wait(timeout=10)
            return client.post(
                request_url,
                json={"say": instruction},
                headers={
                    **headers,
                    "Idempotency-Key": f"concurrent-{uuid.uuid4().hex}",
                },
            )

    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [
            pool.submit(send_request, "Cut the first 5 seconds"),
            pool.submit(send_request, "Cut the last 5 seconds"),
        ]
        responses = [future.result(timeout=30) for future in futures]

    successful = [response for response in responses if response.status_code == 200]
    conflicts = [response for response in responses if response.status_code == 409]
    assert len(successful) == 1
    assert len(conflicts) == 1
    _assert_standard_error(conflicts[0], 409, "SESSION_CONFLICT")
    assert conflicts[0].json()["error"]["retryable"] is True

    with TestClient(app) as client:
        history = client.get(f"/api/v1/sessions/{session_id}/history", headers=headers)
        session = client.get(f"/api/v1/sessions/{session_id}", headers=headers)
    assert history.status_code == 200, history.text
    assert len(history.json()["stack"]) == 1
    assert session.status_code == 200, session.text
    assert session.json()["version"] == 2
    with get_session_factory()() as db:
        edit_count = db.scalar(
            select(func.count()).select_from(EditRecord).where(EditRecord.session_id == session_id)
        )
    assert edit_count == 1


def test_ready_returns_200_when_postgres_and_minio_are_up(http_client) -> None:
    client, _ = http_client
    response = client.get("/ready")
    health = client.get("/health")

    assert response.status_code == 200
    assert response.json() == {"status": "ready"}
    assert health.status_code == 200
    assert health.json() == {"status": "ok"}


def test_ready_returns_503_when_postgres_is_unreachable(http_client, monkeypatch) -> None:
    client, _ = http_client
    settings = get_settings()
    closed_url = make_url(settings.DATABASE_URL).set(host="127.0.0.1", port=_closed_local_port())
    monkeypatch.setenv("DATABASE_URL", closed_url.render_as_string(hide_password=False))

    response = client.get("/ready")
    health = client.get("/health")

    _assert_standard_error(response, 503, "DATABASE_UNAVAILABLE")
    assert response.json()["error"]["retryable"] is True
    assert health.status_code == 200


def test_ready_returns_503_when_minio_is_unreachable(http_client, monkeypatch) -> None:
    client, _ = http_client
    monkeypatch.setenv("S3_ENDPOINT", f"http://127.0.0.1:{_closed_local_port()}")
    monkeypatch.setenv("AWS_MAX_ATTEMPTS", "1")

    response = client.get("/ready")
    health = client.get("/health")

    _assert_standard_error(response, 503, "STORAGE_UNAVAILABLE")
    assert response.json()["error"]["retryable"] is True
    assert health.status_code == 200

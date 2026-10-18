"""HTTP integration tests using the real FastAPI app, Postgres, and MinIO."""

from __future__ import annotations

import hashlib
import os
import socket
import threading
import uuid
from concurrent.futures import ThreadPoolExecutor
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select, text
from sqlalchemy.engine import make_url

from incubrix.adapters.storage.s3 import S3Storage
from incubrix.config import Settings, get_settings
from incubrix.db.engine import get_engine, get_session_factory, run_migrations
from incubrix.db.mapping import source_info_to_video_kwargs, transcript_to_record_kwargs
from incubrix.db.models import EditRecord, JobRecord, TranscriptRecord, VideoRecord
from incubrix.db.repository import SessionRepository
from incubrix.engine.models import Sentence, SourceInfo, Transcript, Word

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
    token = os.environ.get("API_TOKEN") or "integration-http-bearer-token"
    monkeypatch.setenv("API_TOKEN", token)
    monkeypatch.setenv("AUTH_DISABLED", "false")
    monkeypatch.setenv("TEST_MODE", "false")

    settings = get_settings()
    assert settings.API_TOKEN == token

    from incubrix.api.app import app

    return app, {"Authorization": f"Bearer {settings.API_TOKEN}"}


@pytest.fixture
def http_client(api_app: tuple[Any, dict[str, str]]):
    """Provide a real-app TestClient with a settings-backed bearer token."""
    app, headers = api_app
    with TestClient(app) as client:
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


def _assert_standard_error(response, status: int, code: str) -> None:
    assert response.status_code == status, response.text
    error = response.json()["error"]
    assert error["code"] == code
    assert set(error) == {"code", "message", "retryable", "details"}


def _closed_local_port() -> int:
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        return listener.getsockname()[1]


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


def test_concurrent_idempotent_instructions_have_one_effect(api_app) -> None:
    app, headers = api_app
    with TestClient(app) as setup_client:
        session_id = _create_session(setup_client, headers)
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
        session_id = _create_session(setup_client, headers)
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
                headers=headers,
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

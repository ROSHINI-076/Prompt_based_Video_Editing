"""Worker dispatch, retry policy, and callback signing tests."""

from __future__ import annotations

import hashlib
import hmac
import json
from unittest.mock import Mock

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from incubrix.config import Settings
from incubrix.db.models import Base
from incubrix.jobs.handlers import JobExecutionError, _probe_video, _validate_public_https_url
from incubrix.jobs.queue import JobQueue
from incubrix.jobs.worker import Worker


@pytest.fixture
def queue() -> JobQueue:
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    return JobQueue(sessionmaker(bind=engine, expire_on_commit=False), max_attempts=2)


def test_worker_runs_handler_and_persists_success(queue: JobQueue) -> None:
    worker = Worker(
        settings=Settings(_env_file=None),
        queue=queue,
        handlers={"intake_and_transcribe": lambda payload, progress: {"value": payload["value"]}},
        worker_id="worker-a",
    )
    job = queue.enqueue("intake_and_transcribe", {"value": 42})

    assert worker.run_once() is True
    finished = queue.get(job.id)
    assert finished.status == "succeeded"
    assert finished.result == {"value": 42}
    assert worker.run_once() is False


def test_worker_marks_unimplemented_render_as_failed(queue: JobQueue) -> None:
    def not_implemented(payload: dict, progress: object) -> dict:
        raise JobExecutionError("NOT_IMPLEMENTED", "Rendering is not implemented yet.")

    worker = Worker(
        settings=Settings(_env_file=None),
        queue=queue,
        handlers={"render": not_implemented},
        worker_id="worker-a",
    )
    job = queue.enqueue("render", {})

    assert worker.run_once() is True
    finished = queue.get(job.id)
    assert finished.status == "failed"
    assert finished.error["code"] == "NOT_IMPLEMENTED"
    assert finished.error["retryable"] is False


def test_callback_signature_is_hmac_of_exact_json_body(
    queue: JobQueue, monkeypatch: pytest.MonkeyPatch
) -> None:
    settings = Settings(_env_file=None, CALLBACK_HMAC_SECRET="test-callback-secret")
    worker = Worker(
        settings=settings,
        queue=queue,
        handlers={"render": lambda payload, progress: {"ok": True}},
        worker_id="worker-a",
    )
    job = queue.enqueue("render", {}, callback_url="https://callback.example/hook")
    captured: dict = {}

    def post(url: str, *, content: bytes, headers: dict, timeout: float) -> Mock:
        captured.update({"url": url, "content": content, "headers": headers})
        response = Mock()
        response.raise_for_status.return_value = None
        return response

    monkeypatch.setattr("incubrix.jobs.worker.httpx.post", post)
    assert worker.run_once() is True

    expected = hmac.new(
        settings.CALLBACK_HMAC_SECRET.encode(),
        captured["content"],
        hashlib.sha256,
    ).hexdigest()
    callback = json.loads(captured["content"])
    assert callback["job_id"] == job.id
    assert callback["status"] == "succeeded"
    assert captured["headers"]["X-IncuBrix-Signature"] == expected


def test_callback_failure_does_not_change_successful_job(
    queue: JobQueue, monkeypatch: pytest.MonkeyPatch
) -> None:
    worker = Worker(
        settings=Settings(_env_file=None, CALLBACK_MAX_RETRIES=1),
        queue=queue,
        handlers={"render": lambda payload, progress: {"ok": True}},
        worker_id="worker-a",
    )
    job = queue.enqueue("render", {}, callback_url="https://callback.example/hook")

    def fail_post(*args: object, **kwargs: object) -> None:
        import httpx

        raise httpx.ConnectError("unavailable")

    monkeypatch.setattr("incubrix.jobs.worker.httpx.post", fail_post)
    assert worker.run_once() is True
    assert queue.get(job.id).status == "succeeded"


def test_url_ssrf_rejects_private_dns_resolution(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        "incubrix.jobs.handlers.socket.getaddrinfo",
        lambda *args, **kwargs: [(2, 1, 6, "", ("127.0.0.1", 443))],
    )
    with pytest.raises(JobExecutionError, match="cannot resolve"):
        _validate_public_https_url("https://attacker.example/video.mp4")


def test_video_probe_parses_metadata(monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
    completed = Mock(
        stdout=json.dumps(
            {
                "format": {"duration": "3.5"},
                "streams": [
                    {
                        "codec_type": "video",
                        "avg_frame_rate": "30/1",
                        "r_frame_rate": "30/1",
                    },
                    {"codec_type": "audio"},
                ],
            }
        )
    )
    monkeypatch.setattr("incubrix.jobs.handlers.subprocess.run", lambda *a, **k: completed)

    assert _probe_video(tmp_path / "video.mp4") == (3.5, 30.0, True, False)

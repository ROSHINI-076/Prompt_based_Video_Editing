"""Unit tests for durable queue state transitions using SQLite."""

from __future__ import annotations

from datetime import timedelta

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from incubrix.db.models import Base, JobRecord
from incubrix.jobs.queue import JobQueue, utc_now


@pytest.fixture
def job_queue() -> JobQueue:
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    return JobQueue(factory, max_attempts=2, heartbeat_stale_s=10)


def test_job_lifecycle_persists_claim_progress_and_result(job_queue: JobQueue) -> None:
    queued = job_queue.enqueue("render", {"session_id": "s1"})
    assert queued.status == "queued"
    assert queued.attempts == 0

    claimed = job_queue.claim("worker-a")
    assert claimed is not None
    assert claimed.id == queued.id
    assert claimed.status == "running"
    assert claimed.attempts == 1
    assert job_queue.claim("worker-b") is None

    job_queue.heartbeat(claimed.id, "worker-a", progress=45)
    assert job_queue.get(claimed.id).progress == 45
    job_queue.succeed(claimed.id, "worker-a", {"url": "s3://result"})

    completed = job_queue.get(claimed.id)
    assert completed.status == "succeeded"
    assert completed.progress == 100
    assert completed.result == {"url": "s3://result"}
    assert completed.locked_by is None


def test_transient_failure_retries_then_exhausts_attempts(job_queue: JobQueue) -> None:
    job = job_queue.enqueue("intake_and_transcribe", {})
    claimed = job_queue.claim("worker-a")
    assert claimed is not None
    retried = job_queue.fail(
        claimed.id,
        "worker-a",
        {"code": "STORAGE_UNAVAILABLE", "retryable": True},
        retryable=True,
    )
    assert retried is True
    assert job_queue.get(job.id).status == "queued"

    with job_queue._sessions.begin() as session:
        record = session.get(JobRecord, job.id)
        record.available_at = utc_now() - timedelta(seconds=1)
    second_claim = job_queue.claim("worker-a")
    assert second_claim is not None
    assert second_claim.attempts == 2

    retried = job_queue.fail(
        second_claim.id,
        "worker-a",
        {"code": "STORAGE_UNAVAILABLE", "retryable": True},
        retryable=True,
    )
    assert retried is False
    assert job_queue.get(job.id).status == "failed"


def test_stale_running_job_is_recovered(job_queue: JobQueue) -> None:
    job = job_queue.enqueue("render", {})
    claimed = job_queue.claim("worker-crashed")
    assert claimed is not None
    stale_time = utc_now() - timedelta(seconds=30)
    with job_queue._sessions.begin() as session:
        record = session.get(JobRecord, job.id)
        record.heartbeat_at = stale_time

    assert job_queue.recover_stale() == 1
    recovered = job_queue.get(job.id)
    assert recovered.status == "queued"
    assert recovered.locked_by is None
    assert job_queue.recover_stale() == 0


def test_invalid_progress_and_non_owner_cannot_mutate(job_queue: JobQueue) -> None:
    job = job_queue.enqueue("render", {})
    claimed = job_queue.claim("worker-a")
    assert claimed is not None
    with pytest.raises(ValueError, match="between 0 and 100"):
        job_queue.heartbeat(job.id, "worker-a", progress=101)
    with pytest.raises(ValueError, match="does not own"):
        job_queue.succeed(job.id, "worker-b", {})

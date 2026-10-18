"""Postgres-backed job enqueueing, claiming, progress, and retry state."""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from incubrix.db.engine import get_session_factory
from incubrix.db.models import JobRecord


def utc_now() -> datetime:
    """Return timezone-aware UTC time for durable queue timestamps."""
    return datetime.now(timezone.utc)


class JobQueue:
    """Use row locks with SKIP LOCKED so workers never claim the same job."""

    def __init__(
        self,
        session_factory: sessionmaker[Session] | None = None,
        *,
        max_attempts: int = 3,
        heartbeat_stale_s: int = 120,
    ) -> None:
        self._sessions = session_factory or get_session_factory()
        self._max_attempts = max_attempts
        self._heartbeat_stale_s = heartbeat_stale_s

    def enqueue(
        self,
        job_type: str,
        payload: dict[str, Any],
        callback_url: str | None = None,
    ) -> JobRecord:
        """Persist a job and return its queued record."""
        now = utc_now()
        record = JobRecord(
            id=str(uuid.uuid4()),
            type=job_type,
            payload=payload,
            callback_url=callback_url,
            max_attempts=self._max_attempts,
            available_at=now,
            created_at=now,
        )
        with self._sessions.begin() as session:
            session.add(record)
            session.flush()
            session.expunge(record)
        return record

    def recover_stale(self, now: datetime | None = None) -> int:
        """Requeue abandoned work or fail it once retries are exhausted."""
        current = now or utc_now()
        stale_before = current - timedelta(seconds=self._heartbeat_stale_s)
        with self._sessions.begin() as session:
            stale = (
                session.execute(
                    select(JobRecord)
                    .where(
                        JobRecord.status == "running",
                        JobRecord.heartbeat_at < stale_before,
                    )
                    .with_for_update(skip_locked=True)
                )
                .scalars()
                .all()
            )
            for job in stale:
                job.locked_by = None
                job.heartbeat_at = None
                if job.attempts >= job.max_attempts:
                    job.status = "failed"
                    job.error = {
                        "code": "JOB_HEARTBEAT_EXPIRED",
                        "message": "Worker heartbeat expired and retry limit was reached.",
                        "retryable": False,
                        "details": None,
                    }
                    job.finished_at = current
                else:
                    job.status = "queued"
                    job.available_at = current + timedelta(seconds=2**job.attempts)
            return len(stale)

    def claim(self, worker_id: str, now: datetime | None = None) -> JobRecord | None:
        """Atomically claim one available queued job."""
        current = now or utc_now()
        with self._sessions.begin() as session:
            job = session.execute(
                select(JobRecord)
                .where(
                    JobRecord.status == "queued",
                    JobRecord.available_at <= current,
                )
                .order_by(JobRecord.available_at, JobRecord.created_at, JobRecord.id)
                .with_for_update(skip_locked=True)
                .limit(1)
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

    def heartbeat(self, job_id: str, worker_id: str, progress: int | None = None) -> None:
        """Update a claimed job's lease and optionally its progress."""
        if progress is not None and not 0 <= progress <= 100:
            raise ValueError("Job progress must be between 0 and 100.")
        with self._sessions.begin() as session:
            job = self._owned_job(session, job_id, worker_id)
            job.heartbeat_at = utc_now()
            if progress is not None:
                job.progress = progress

    def succeed(self, job_id: str, worker_id: str, result: dict[str, Any]) -> None:
        """Mark a claimed job complete and persist its result."""
        with self._sessions.begin() as session:
            job = self._owned_job(session, job_id, worker_id)
            job.status = "succeeded"
            job.progress = 100
            job.result = result
            job.error = None
            job.finished_at = utc_now()
            job.heartbeat_at = utc_now()
            job.locked_by = None

    def fail(
        self,
        job_id: str,
        worker_id: str,
        error: dict[str, Any],
        *,
        retryable: bool,
    ) -> bool:
        """Persist an error, retry transient failures with exponential backoff."""
        now = utc_now()
        with self._sessions.begin() as session:
            job = self._owned_job(session, job_id, worker_id)
            should_retry = retryable and job.attempts < job.max_attempts
            job.error = error
            job.heartbeat_at = now
            job.locked_by = None
            if should_retry:
                job.status = "queued"
                job.available_at = now + timedelta(seconds=2 ** (job.attempts - 1))
            else:
                job.status = "failed"
                job.finished_at = now
            return should_retry

    @staticmethod
    def _owned_job(session: Session, job_id: str, worker_id: str) -> JobRecord:
        job = session.get(JobRecord, job_id)
        if job is None or job.status != "running" or job.locked_by != worker_id:
            raise ValueError(f"Worker {worker_id!r} does not own running job {job_id!r}.")
        return job

    def get(self, job_id: str) -> JobRecord | None:
        """Return a detached job record for API projection."""
        with self._sessions() as session:
            job = session.get(JobRecord, job_id)
            if job is not None:
                session.expunge(job)
            return job

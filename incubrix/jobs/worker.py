"""Postgres job worker with heartbeats, retries, callbacks, and clean shutdown."""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import signal
import threading
import time
import uuid
from collections.abc import Callable
from typing import Any

import httpx

from incubrix.config import Settings, get_settings
from incubrix.db.engine import run_migrations
from incubrix.jobs.handlers import JobExecutionError, build_handlers
from incubrix.jobs.queue import JobQueue
from incubrix.logging import (
    correlation_id_var,
    get_logger,
    job_id_var,
    setup_logging,
)

log = get_logger(__name__)

Handler = Callable[[dict[str, Any], Callable[[int], None]], dict[str, Any]]


class Worker:
    """Claim and execute jobs through the durable queue."""

    def __init__(
        self,
        *,
        settings: Settings | None = None,
        queue: JobQueue | None = None,
        handlers: dict[str, Handler] | None = None,
        worker_id: str | None = None,
    ) -> None:
        self.settings = settings or get_settings()
        self.queue = queue or JobQueue(
            max_attempts=self.settings.JOB_MAX_ATTEMPTS,
            heartbeat_stale_s=self.settings.JOB_HEARTBEAT_STALE_S,
        )
        self.handlers = handlers or build_handlers(self.settings)
        self.worker_id = worker_id or f"worker-{uuid.uuid4().hex[:12]}"

    def run_once(self) -> bool:
        """Process one available job; return False if the queue is empty."""
        self.queue.recover_stale()
        job = self.queue.claim(self.worker_id)
        if job is None:
            return False

        token = job_id_var.set(job.id)
        correlation_token = correlation_id_var.set(
            str(job.payload.get("_correlation_id") or job.id)
        )
        log.info("job_started", job_type=job.type, attempt=job.attempts)
        heartbeat_stop = threading.Event()
        heartbeat_thread = threading.Thread(
            target=self._heartbeat_loop,
            args=(job.id, heartbeat_stop),
            name=f"heartbeat-{job.id}",
            daemon=True,
        )
        heartbeat_thread.start()
        try:
            handler = self.handlers.get(job.type)
            if handler is None:
                raise JobExecutionError(
                    "UNKNOWN_JOB_TYPE", "No handler is registered for this job."
                )
            result = handler(
                {**job.payload, "job_id": job.id},
                lambda progress: self.queue.heartbeat(job.id, self.worker_id, progress),
            )
            self.queue.succeed(job.id, self.worker_id, result)
            log.info("job_succeeded", job_type=job.type)
        except JobExecutionError as exc:
            error = exc.as_dict()
            self.queue.fail(job.id, self.worker_id, error, retryable=exc.retryable)
            log.warning("job_failed", job_type=job.type, error_code=exc.code)
        except Exception:
            error = {
                "code": "INTERNAL_ERROR",
                "message": "Job handler failed unexpectedly.",
                "retryable": True,
                "details": None,
            }
            self.queue.fail(job.id, self.worker_id, error, retryable=True)
            log.error("job_failed", job_type=job.type, error_code="INTERNAL_ERROR")
        finally:
            heartbeat_stop.set()
            heartbeat_thread.join(timeout=2)
            job_id_var.reset(token)
            correlation_id_var.reset(correlation_token)

        self._send_callback(job.id)
        return True

    def _heartbeat_loop(self, job_id: str, stop: threading.Event) -> None:
        interval = max(1.0, self.settings.JOB_HEARTBEAT_STALE_S / 3)
        while not stop.wait(interval):
            try:
                self.queue.heartbeat(job_id, self.worker_id)
            except Exception:
                log.warning("job_heartbeat_failed")
                return

    def _send_callback(self, job_id: str) -> None:
        """Send the final state with an HMAC signature; callback errors don't fail jobs."""
        job = self.queue.get(job_id)
        if job is None or not job.callback_url:
            return
        body = json.dumps(
            {
                "job_id": job.id,
                "status": job.status,
                "progress": job.progress,
                "result": job.result,
                "error": job.error,
            },
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        signature = hmac.new(
            self.settings.CALLBACK_HMAC_SECRET.encode("utf-8"),
            body,
            hashlib.sha256,
        ).hexdigest()
        for attempt in range(self.settings.CALLBACK_MAX_RETRIES):
            try:
                response = httpx.post(
                    job.callback_url,
                    content=body,
                    headers={
                        "Content-Type": "application/json",
                        "X-IncuBrix-Signature": signature,
                    },
                    timeout=self.settings.URL_FETCH_TIMEOUT_S,
                )
                response.raise_for_status()
                return
            except httpx.HTTPError:
                log.warning("job_callback_failed", attempt=attempt + 1)
                if attempt + 1 < self.settings.CALLBACK_MAX_RETRIES:
                    time.sleep(self.settings.CALLBACK_BACKOFF_S * 2**attempt)

    def run(self, stop: threading.Event) -> None:
        """Poll the queue until shutdown is requested."""
        while not stop.is_set():
            if not self.run_once():
                stop.wait(self.settings.WORKER_POLL_INTERVAL_S)


def main() -> None:
    """Run configured worker concurrency with graceful signal handling."""
    settings = get_settings()
    setup_logging(log_level=settings.LOG_LEVEL, log_format=settings.LOG_FORMAT)
    run_migrations(settings.DATABASE_URL)

    stop = threading.Event()

    def request_shutdown(signum: int, frame: Any) -> None:
        del frame
        log.info("worker_shutdown_signal", signal=signum)
        stop.set()

    signal.signal(signal.SIGTERM, request_shutdown)
    signal.signal(signal.SIGINT, request_shutdown)

    workers = [
        Worker(settings=settings, worker_id=f"{os.getpid()}-{index}")
        for index in range(max(1, settings.WORKER_CONCURRENCY))
    ]
    threads = [
        threading.Thread(target=worker.run, args=(stop,), name=worker.worker_id)
        for worker in workers
    ]
    log.info("worker_started", concurrency=len(threads))
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    log.info("worker_stopped")


if __name__ == "__main__":
    main()

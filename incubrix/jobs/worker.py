"""Background job worker.

Claims jobs from Postgres using SELECT ... FOR UPDATE SKIP LOCKED.
Stub implementation for Phase 0 – just starts and logs.
"""

from __future__ import annotations

import signal
import time

from incubrix.config import get_settings
from incubrix.logging import get_logger, setup_logging

log = get_logger(__name__)
_running = True


def _shutdown(signum, frame):
    global _running
    log.info("worker_shutdown_signal", signal=signum)
    _running = False


def main() -> None:
    """Worker entry point."""
    settings = get_settings()
    setup_logging(log_level=settings.LOG_LEVEL, log_format=settings.LOG_FORMAT)
    signal.signal(signal.SIGTERM, _shutdown)
    signal.signal(signal.SIGINT, _shutdown)

    log.info("worker_started", poll_interval=settings.WORKER_POLL_INTERVAL_S)
    while _running:
        # Phase 0 stub: no jobs table yet, just heartbeat
        time.sleep(settings.WORKER_POLL_INTERVAL_S)
    log.info("worker_stopped")


if __name__ == "__main__":
    main()

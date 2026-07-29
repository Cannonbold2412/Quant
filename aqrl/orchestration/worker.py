"""One job, one subprocess (TRD §9.3's process-not-thread stance applied to
job execution, not just fold parallelism).

Invoked as `python -m aqrl.orchestration.worker --job-uid <uid>`. Per-job
process isolation means a crash, a runaway allocation, or a stuck C extension
in one job cannot touch another job's memory — and gives `dispatch.py` a
plain OS process to `SIGTERM`/`SIGKILL` for the per-experiment time budget
(TRD §9.6).

Two connections, deliberately. The main thread runs the handler and owns the
transaction that persists its outcome; a second, independent connection
drives the heartbeat from a background thread. They must be separate
connections — SQLite connections are not safe to share across threads, and
WAL mode is exactly what makes two connections against the same file
harmless here.
"""
from __future__ import annotations

import argparse
import os
import socket
import sys
import threading
import time
import traceback
from typing import NoReturn

from ..config import get_settings
from ..db import connect, transaction
from ..db.repositories import JobRepository, LeaseLost
from ..logging import configure, get_logger, log_context
from .budgets import consume
from .failures import handle_job_failure
from .handlers import get_handler

__all__ = ["main", "run_job"]

_EXIT_OK = 0
_EXIT_HANDLER_FAILED = 1
_EXIT_NO_SUCH_JOB = 2
_EXIT_LEASE_LOST_AT_START = 3
_EXIT_LEASE_LOST_MID_JOB = 4


def _default_worker_id() -> str:
    return f"{socket.gethostname()}:{os.getpid()}"


def _heartbeat_loop(job_id: int, worker_id: str, lease_seconds: int, stop_event: threading.Event) -> None:
    conn = connect()
    jobs = JobRepository(conn)
    interval = max(lease_seconds / 3, 1.0)
    log = get_logger(__name__)
    try:
        while not stop_event.wait(interval):
            try:
                jobs.heartbeat(job_id, worker_id, lease_seconds=lease_seconds)
            except LeaseLost:
                log.warning("heartbeat: lease lost", extra={"job_id": job_id, "worker_id": worker_id})
                return
    finally:
        conn.close()


def run_job(job_uid: str, *, worker_id: str | None = None, lease_seconds: int | None = None) -> int:
    """Run one job to completion. Returns a process-style exit code.

    The function `main()` wraps for the CLI entry point, and what the crash
    tests invoke directly against an in-process (but still real, on-disk)
    database — a real `SIGKILL` test needs a real subprocess, so
    `dispatch.py` shells out to `python -m aqrl.orchestration.worker` rather
    than calling this function in-process.
    """
    configure()
    log = get_logger(__name__)
    settings = get_settings()
    worker_id = worker_id or _default_worker_id()
    lease_seconds = lease_seconds or settings.default_lease_seconds

    conn = connect()
    try:
        jobs = JobRepository(conn)
        job = jobs.get_by_uid(job_uid)
        if job is None:
            log.error("no such job", extra={"job_uid": job_uid})
            return _EXIT_NO_SUCH_JOB

        try:
            jobs.mark_running(job["id"], worker_id)
        except LeaseLost:
            log.warning("lease already lost before start", extra={"job_uid": job_uid})
            return _EXIT_LEASE_LOST_AT_START

        stop_event = threading.Event()
        heartbeat = threading.Thread(
            target=_heartbeat_loop, args=(job["id"], worker_id, lease_seconds, stop_event), daemon=True
        )
        heartbeat.start()

        start = time.monotonic()
        try:
            with log_context(job_uid=job["uid"], job_type=job["job_type"], worker_id=worker_id):
                handler = get_handler(job["job_type"])
                outcome = handler.run(conn, job)
                duration = int(time.monotonic() - start)
                with transaction(conn, immediate=True):
                    result = handler.persist(conn, job, outcome)
                    jobs.succeed(job["id"], worker_id, tokens_spent=result.tokens_spent, duration_seconds=duration)
                    consume(conn, "global", "compute_seconds", "day", duration)
                    consume(conn, "global", "tokens", "day", result.tokens_spent)
                    if job["job_type"] == "EVALUATE":
                        consume(conn, "global", "experiments", "day", 1)
            return _EXIT_OK
        except LeaseLost:
            log.warning("lease lost mid-job; another worker owns it now", extra={"job_uid": job["uid"]})
            return _EXIT_LEASE_LOST_MID_JOB
        except Exception as exc:  # noqa: BLE001 - every failure must be classified and recorded
            trace = traceback.format_exc()
            message = str(exc) or type(exc).__name__
            try:
                with transaction(conn, immediate=True):
                    current = jobs.get(job["id"])
                    handle_job_failure(
                        conn, current, worker_id, error_message=message, error_trace=trace, exc=exc
                    )
            except LeaseLost:
                log.warning("lease lost while recording failure", extra={"job_uid": job["uid"]})
            return _EXIT_HANDLER_FAILED
        finally:
            stop_event.set()
            heartbeat.join(timeout=5)
    finally:
        conn.close()


def _parse_args(argv: list[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(prog="python -m aqrl.orchestration.worker")
    parser.add_argument("--job-uid", required=True)
    parser.add_argument("--worker-id", default=None)
    parser.add_argument("--lease-seconds", type=int, default=None)
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> NoReturn:
    args = _parse_args(argv)
    sys.exit(run_job(args.job_uid, worker_id=args.worker_id, lease_seconds=args.lease_seconds))


if __name__ == "__main__":
    main()

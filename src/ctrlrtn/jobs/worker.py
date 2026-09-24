"""Worker runtime for claiming and executing one durable job at a time."""

from __future__ import annotations

import socket
import threading
import time
import uuid
from collections.abc import Mapping

from ctrlrtn.jobs.context import JobCancelled, JobContext, JobHandler


class Worker:
    """Claims and runs one durable job at a time.

    Handlers are injected so the queue is independent of replay/training job
    implementations. A missing handler fails visibly instead of leaving a job
    stuck in ``running``.
    """

    def __init__(
        self,
        store,
        handlers: Mapping[str, JobHandler],
        *,
        worker_id: str | None = None,
        stale_after: float = 120.0,
        heartbeat_interval: float | None = None,
    ) -> None:
        self.store = store
        self.handlers = dict(handlers)
        self.worker_id = (
            worker_id or f"{socket.gethostname()}:{uuid.uuid4().hex[:8]}"
        )
        self.stale_after = stale_after
        self.heartbeat_interval = heartbeat_interval or min(
            30.0, stale_after / 3
        )

    def run_once(self) -> bool:
        job = self.store.claim_job(
            self.worker_id, stale_before=time.time() - self.stale_after
        )
        if job is None:
            return False
        handler = self.handlers.get(job.kind)
        if handler is None:
            self.store.fail_job(
                job.job_id,
                self.worker_id,
                f"no worker handler registered for job kind {job.kind!r}",
            )
            return True
        context = JobContext(self.store, job, self.worker_id)
        stopped = threading.Event()

        def heartbeat() -> None:
            while not stopped.wait(self.heartbeat_interval):
                if not self.store.heartbeat_job(job.job_id, self.worker_id):
                    return

        heartbeat_thread = threading.Thread(
            target=heartbeat,
            name=f"ctrlrtn-heartbeat-{job.job_id}",
            daemon=True,
        )
        heartbeat_thread.start()
        try:
            result = handler(context, job.config)
        except JobCancelled:
            self.store.cancel_claimed_job(job.job_id, self.worker_id)
        except Exception as exc:  # noqa: BLE001 - persisted worker boundary
            self.store.fail_job(job.job_id, self.worker_id, str(exc))
        else:
            try:
                context.checkpoint()
            except JobCancelled:
                self.store.cancel_claimed_job(job.job_id, self.worker_id)
            else:
                self.store.complete_job(
                    job.job_id, self.worker_id, result or {}
                )
        finally:
            stopped.set()
            heartbeat_thread.join()
        return True

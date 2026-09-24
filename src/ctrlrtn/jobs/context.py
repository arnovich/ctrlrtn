"""Cooperative progress and cancellation context for job handlers."""

from __future__ import annotations

from collections.abc import Callable

from ctrlrtn.jobs.models import Job


class JobCancelled(Exception):
    """Raised cooperatively when cancellation is requested."""


class JobContext:
    def __init__(self, store, job: Job, worker_id: str) -> None:
        self.store = store
        self.job = job
        self.worker_id = worker_id

    def progress(
        self, current: int, total: int | None = None, message: str | None = None
    ) -> None:
        if not self.store.update_job_progress(
            self.job.job_id,
            self.worker_id,
            current=current,
            total=total,
            message=message,
        ):
            raise JobCancelled()

    def checkpoint(self) -> None:
        if not self.store.heartbeat_job(self.job.job_id, self.worker_id):
            raise JobCancelled()


JobHandler = Callable[[JobContext, dict], dict | None]

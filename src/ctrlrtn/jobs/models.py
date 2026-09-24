"""Durable job state and lifecycle constants."""

from __future__ import annotations

import time
import uuid
from dataclasses import dataclass, field

QUEUED = "queued"
RUNNING = "running"
SUCCEEDED = "succeeded"
FAILED = "failed"
CANCELLED = "cancelled"
TERMINAL = frozenset({SUCCEEDED, FAILED, CANCELLED})


def new_job_id() -> str:
    return f"job:{uuid.uuid4().hex[:12]}"


@dataclass(frozen=True)
class Job:
    """One durable job record: its kind and config, lifecycle status, lease
    and progress. Immutable; every transition is a new value produced by the
    store, and ``status`` is restricted to the module's constants."""

    kind: str
    config: dict
    job_id: str = field(default_factory=new_job_id)
    status: str = QUEUED
    created_at: float = field(default_factory=time.time)
    updated_at: float = field(default_factory=time.time)
    started_at: float | None = None
    finished_at: float | None = None
    worker_id: str | None = None
    heartbeat_at: float | None = None
    progress_current: int = 0
    progress_total: int | None = None
    progress_message: str | None = None
    result: dict | None = None
    error: str | None = None
    cancel_requested: bool = False
    attempt: int = 0

    def __post_init__(self) -> None:
        if not self.kind.strip():
            raise ValueError("job kind must not be empty")
        if not self.job_id.strip():
            raise ValueError("job id must not be empty")
        if self.status not in {QUEUED, RUNNING, *TERMINAL}:
            raise ValueError(f"invalid job status: {self.status!r}")
        if self.progress_current < 0:
            raise ValueError("job progress must not be negative")
        if self.progress_total is not None and self.progress_total < 0:
            raise ValueError("job progress total must not be negative")

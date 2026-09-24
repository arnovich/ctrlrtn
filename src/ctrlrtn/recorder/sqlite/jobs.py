"""SQLite durable-job queue persistence."""

from __future__ import annotations

import json
import sqlite3
import time

from ctrlrtn.jobs import Job

from .connection import SqliteCapability
from .queries import _SELECT_JOBS, _row_to_job


class JobSqliteMixin(SqliteCapability):
    """Create, claim, update, and complete durable jobs."""

    def create_job(self, job: Job) -> None:
        with self._lock:
            self._conn.execute(
                """INSERT INTO jobs (
                    job_id, kind, config_json, status, created_at, updated_at,
                    progress_current, progress_total, progress_message,
                    cancel_requested, attempt
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    job.job_id,
                    job.kind,
                    json.dumps(job.config, sort_keys=True),
                    job.status,
                    job.created_at,
                    job.updated_at,
                    job.progress_current,
                    job.progress_total,
                    job.progress_message,
                    int(job.cancel_requested),
                    job.attempt,
                ),
            )
            self._conn.commit()

    def jobs(self, limit: int = 100, *, offset: int = 0) -> list[Job]:
        """Newest jobs first. ``offset`` skips that many, for paging past the
        first ``limit``."""
        with self._lock:
            try:
                rows = self._conn.execute(
                    _SELECT_JOBS
                    + " ORDER BY created_at DESC, rowid DESC LIMIT ? OFFSET ?",
                    (limit, offset),
                ).fetchall()
            except sqlite3.OperationalError as exc:
                if "no such table" in str(exc):
                    return []
                raise
        return [_row_to_job(row) for row in rows]

    def latest_job(self, kind: str, *, status: str | None = None) -> Job | None:
        """The newest job of one kind (optionally in one status), found
        however far down the queue it sits — a paged jobs list must not be
        able to hide it."""
        query = _SELECT_JOBS + " WHERE kind = ?"
        params: list = [kind]
        if status is not None:
            query += " AND status = ?"
            params.append(status)
        query += " ORDER BY created_at DESC, rowid DESC LIMIT 1"
        with self._lock:
            try:
                row = self._conn.execute(query, tuple(params)).fetchone()
            except sqlite3.OperationalError as exc:
                if "no such table" in str(exc):
                    return None
                raise
        return _row_to_job(row) if row else None

    def job(self, job_id: str) -> Job | None:
        with self._lock:
            row = self._conn.execute(
                _SELECT_JOBS + " WHERE job_id = ?", (job_id,)
            ).fetchone()
        return _row_to_job(row) if row else None

    def claim_job(self, worker_id: str, *, stale_before: float) -> Job | None:
        now = time.time()
        with self._lock:
            self._conn.execute("BEGIN IMMEDIATE")
            row = self._conn.execute(
                """SELECT job_id FROM jobs
                   WHERE cancel_requested = 0 AND (
                       status = 'queued' OR
                       (status = 'running' AND heartbeat_at < ?)
                   )
                   ORDER BY created_at, rowid LIMIT 1""",
                (stale_before,),
            ).fetchone()
            if row is None:
                self._conn.commit()
                return None
            job_id = row[0]
            self._conn.execute(
                """UPDATE jobs SET status = 'running', worker_id = ?,
                   started_at = COALESCE(started_at, ?), heartbeat_at = ?,
                   updated_at = ?, attempt = attempt + 1, error = NULL
                   WHERE job_id = ?""",
                (worker_id, now, now, now, job_id),
            )
            self._conn.commit()
            claimed = self._conn.execute(
                _SELECT_JOBS + " WHERE job_id = ?", (job_id,)
            ).fetchone()
        return _row_to_job(claimed)

    def request_job_cancel(self, job_id: str) -> bool:
        now = time.time()
        with self._lock:
            cur = self._conn.execute(
                """UPDATE jobs SET cancel_requested = 1, updated_at = ?,
                   status = CASE WHEN status = 'queued' THEN 'cancelled'
                                 ELSE status END,
                   finished_at = CASE WHEN status = 'queued' THEN ?
                                      ELSE finished_at END
                   WHERE job_id = ? AND status IN ('queued', 'running')""",
                (now, now, job_id),
            )
            self._conn.commit()
        return cur.rowcount > 0

    def heartbeat_job(self, job_id: str, worker_id: str) -> bool:
        now = time.time()
        with self._lock:
            cur = self._conn.execute(
                """UPDATE jobs SET heartbeat_at = ?, updated_at = ?
                   WHERE job_id = ? AND worker_id = ? AND status = 'running'
                         AND cancel_requested = 0""",
                (now, now, job_id, worker_id),
            )
            self._conn.commit()
        return cur.rowcount > 0

    def update_job_progress(
        self,
        job_id: str,
        worker_id: str,
        *,
        current: int,
        total: int | None,
        message: str | None,
    ) -> bool:
        if current < 0 or (total is not None and total < 0):
            raise ValueError("job progress must not be negative")
        now = time.time()
        with self._lock:
            cur = self._conn.execute(
                """UPDATE jobs SET progress_current = ?, progress_total = ?,
                   progress_message = ?, heartbeat_at = ?, updated_at = ?
                   WHERE job_id = ? AND worker_id = ? AND status = 'running'
                         AND cancel_requested = 0""",
                (current, total, message, now, now, job_id, worker_id),
            )
            self._conn.commit()
        return cur.rowcount > 0

    def _finish_job(
        self,
        job_id: str,
        worker_id: str,
        status: str,
        *,
        result: dict | None = None,
        error: str | None = None,
    ) -> bool:
        now = time.time()
        with self._lock:
            cur = self._conn.execute(
                """UPDATE jobs SET status = ?, result_json = ?, error = ?,
                   finished_at = ?, heartbeat_at = ?, updated_at = ?
                   WHERE job_id = ? AND worker_id = ? AND status = 'running'""",
                (
                    status,
                    (
                        json.dumps(result, sort_keys=True)
                        if result is not None
                        else None
                    ),
                    error,
                    now,
                    now,
                    now,
                    job_id,
                    worker_id,
                ),
            )
            self._conn.commit()
        return cur.rowcount > 0

    def complete_job(self, job_id: str, worker_id: str, result: dict) -> bool:
        return self._finish_job(job_id, worker_id, "succeeded", result=result)

    def fail_job(self, job_id: str, worker_id: str, error: str) -> bool:
        return self._finish_job(job_id, worker_id, "failed", error=error)

    def cancel_claimed_job(self, job_id: str, worker_id: str) -> bool:
        return self._finish_job(job_id, worker_id, "cancelled")

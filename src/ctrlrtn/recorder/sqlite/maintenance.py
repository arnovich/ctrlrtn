"""SQLite privacy, erasure, retention, and compaction operations."""

from __future__ import annotations

import json
import os
import sqlite3
import time

from ctrlrtn.recorder.redaction import redact_headers, redact_query

from .results import DatabaseCompaction, TracePayloadPrune, WorkflowTaskErasure


class MaintenanceSqliteMixin:
    """Perform explicit maintenance operations on an owned connection."""

    def scrub_credential_headers(self) -> int:
        """Redact credential header values AND credential query params from
        every already-recorded trace (databases written before capture-time
        redaction existed, or before it covered query strings). Returns how
        many traces were rewritten. Idempotent."""
        scrubbed = 0
        with self._lock:
            rows = self._conn.execute(
                "SELECT id, request_headers, response_headers, query "
                "FROM traces"
            ).fetchall()
            for trace_id, req_raw, resp_raw, query in rows:
                changed = False
                cleaned = []
                for raw in (req_raw, resp_raw):
                    headers = json.loads(raw) if raw else {}
                    redacted = redact_headers(headers)
                    if redacted != headers:
                        changed = True
                    cleaned.append(json.dumps(redacted))
                clean_query = redact_query(query or "")
                if clean_query != (query or ""):
                    changed = True
                if changed:
                    self._conn.execute(
                        "UPDATE traces SET request_headers = ?, "
                        "response_headers = ?, query = ? WHERE id = ?",
                        (cleaned[0], cleaned[1], clean_query, trace_id),
                    )
                    scrubbed += 1
            self._conn.commit()
        return scrubbed

    def prune_trace_payloads(
        self, before: float, *, apply: bool = False
    ) -> TracePayloadPrune:
        """Plan or erase payload fields for traces older than ``before``.

        Derived accounting and workflow identity remain available. Exact trace
        payloads frozen by queued/running jobs are protected so retention cannot
        silently alter an offline experiment's sample.
        """
        if not isinstance(before, (int, float)) or not float(before) > 0:
            raise ValueError("prune cutoff must be a positive timestamp")
        before = float(before)
        with self._lock:
            self._conn.execute("BEGIN IMMEDIATE" if apply else "BEGIN")
            try:
                protected: set[int] = set()
                active_jobs = self._conn.execute(
                    "SELECT config_json FROM jobs "
                    "WHERE status IN ('queued', 'running')"
                ).fetchall()
                for (raw_config,) in active_jobs:
                    try:
                        trace_ids = json.loads(raw_config).get("trace_ids", [])
                    except (AttributeError, TypeError, ValueError) as exc:
                        raise ValueError(
                            "active job has invalid trace retention metadata"
                        ) from exc
                    if not isinstance(trace_ids, list) or any(
                        not isinstance(item, int) or isinstance(item, bool)
                        for item in trace_ids
                    ):
                        raise ValueError(
                            "active job has invalid trace retention metadata"
                        )
                    protected.update(trace_ids)

                rows = self._conn.execute(
                    "SELECT id, COALESCE(length(query), 0) + "
                    "COALESCE(length(request_headers), 0) + "
                    "COALESCE(length(request_body), 0) + "
                    "COALESCE(length(response_headers), 0) + "
                    "COALESCE(length(response_body), 0) "
                    "FROM traces WHERE ts < ? AND (query <> '' OR "
                    "request_headers <> '{}' OR request_body IS NOT NULL OR "
                    "response_headers <> '{}' OR response_body IS NOT NULL)",
                    (before,),
                ).fetchall()
                eligible = [(int(row[0]), int(row[1])) for row in rows]
                prune_ids = [
                    trace_id
                    for trace_id, _payload_bytes in eligible
                    if trace_id not in protected
                ]
                inferred_edges = 0
                if prune_ids:
                    placeholders = ",".join("?" for _ in prune_ids)
                    inferred_edges = int(
                        self._conn.execute(
                            "SELECT COUNT(*) FROM inferred_workflow_edges "
                            f"WHERE source_trace_id IN ({placeholders}) OR "
                            f"target_trace_id IN ({placeholders})",
                            (*prune_ids, *prune_ids),
                        ).fetchone()[0]
                    )
                affected_jobs: list[tuple[str, int]] = []
                if prune_ids:
                    pruned_set = set(prune_ids)
                    for job_id, raw_config in self._conn.execute(
                        "SELECT job_id, config_json FROM jobs "
                        "WHERE kind = 'workflow_discovery' "
                        "AND status = 'succeeded'"
                    ).fetchall():
                        try:
                            frozen = set(
                                json.loads(raw_config).get("trace_ids", [])
                            )
                        except (AttributeError, TypeError, ValueError):
                            continue
                        affected = len(pruned_set & frozen)
                        if affected:
                            affected_jobs.append((job_id, affected))
                if apply and prune_ids:
                    self._conn.execute(
                        "DELETE FROM inferred_workflow_edges "
                        f"WHERE source_trace_id IN ({placeholders}) OR "
                        f"target_trace_id IN ({placeholders})",
                        (*prune_ids, *prune_ids),
                    )
                    self._conn.executemany(
                        "UPDATE traces SET query = '', request_headers = '{}', "
                        "request_body = NULL, response_headers = '{}', "
                        "response_body = NULL WHERE id = ?",
                        ((trace_id,) for trace_id in prune_ids),
                    )
                if apply:
                    for job_id, affected in affected_jobs:
                        self._conn.execute(
                            """INSERT INTO workflow_discovery_invalidations (
                                   job_id, invalidated_at, pruned_traces
                               ) VALUES (?, ?, ?)
                               ON CONFLICT(job_id) DO UPDATE SET
                                   invalidated_at = excluded.invalidated_at,
                                   pruned_traces = pruned_traces + excluded.pruned_traces""",
                            (job_id, time.time(), affected),
                        )
                result = TracePayloadPrune(
                    before=before,
                    eligible_traces=len(eligible),
                    protected_traces=sum(
                        trace_id in protected for trace_id, _ in eligible
                    ),
                    pruned_traces=len(prune_ids),
                    invalidated_inferred_edges=inferred_edges,
                    invalidated_discovery_jobs=len(affected_jobs),
                    payload_bytes=sum(
                        size
                        for trace_id, size in eligible
                        if trace_id not in protected
                    ),
                    applied=apply,
                )
                if apply:
                    self._conn.commit()
                else:
                    self._conn.rollback()
                return result
            except Exception:
                self._conn.rollback()
                raise

    def workflow_discovery_invalidation(self, job_id: str) -> dict | None:
        try:
            with self._lock:
                row = self._conn.execute(
                    "SELECT invalidated_at, pruned_traces "
                    "FROM workflow_discovery_invalidations WHERE job_id = ?",
                    (job_id,),
                ).fetchone()
        except sqlite3.OperationalError as exc:
            if "no such table" not in str(exc):
                raise
            return None
        return (
            None
            if row is None
            else {"invalidated_at": row[0], "pruned_traces": row[1]}
        )

    def erase_workflow_task(
        self, task_id: str, *, apply: bool = False
    ) -> WorkflowTaskErasure:
        """Erase a task's source rows and recomputable database projections.

        Queued or running jobs that bind the task or any of its trace IDs block
        applied erasure. Aggregate graphs and metrics are query-time projections,
        so deleting their source rows removes them without retained snapshots.
        """
        if not isinstance(task_id, str) or not task_id.strip():
            raise ValueError("workflow task id must be non-empty")
        with self._lock:
            self._conn.execute("BEGIN IMMEDIATE" if apply else "BEGIN")
            try:
                trace_ids = {
                    int(row[0])
                    for row in self._conn.execute(
                        "SELECT id FROM traces WHERE task_id = ?", (task_id,)
                    ).fetchall()
                }
                protected_jobs = 0
                for (raw_config,) in self._conn.execute(
                    "SELECT config_json FROM jobs "
                    "WHERE status IN ('queued', 'running')"
                ).fetchall():
                    try:
                        config = json.loads(raw_config)
                        job_trace_ids = config.get("trace_ids", [])
                    except (AttributeError, TypeError, ValueError) as exc:
                        raise ValueError(
                            "active job has invalid task retention metadata"
                        ) from exc
                    if not isinstance(job_trace_ids, list) or any(
                        not isinstance(item, int) or isinstance(item, bool)
                        for item in job_trace_ids
                    ):
                        raise ValueError(
                            "active job has invalid task retention metadata"
                        )
                    if config.get(
                        "task_id"
                    ) == task_id or trace_ids.intersection(job_trace_ids):
                        protected_jobs += 1

                def count(table: str) -> int:
                    return int(
                        self._conn.execute(
                            f"SELECT COUNT(*) FROM {table} WHERE task_id = ?",
                            (task_id,),
                        ).fetchone()[0]
                    )

                result = WorkflowTaskErasure(
                    task_id=task_id,
                    traces=count("traces"),
                    workflow_events=count("workflow_events"),
                    tool_operation_events=count("tool_operation_events"),
                    inferred_edges=count("inferred_workflow_edges"),
                    outcomes=count("outcomes"),
                    protected_jobs=protected_jobs,
                    applied=apply,
                )
                if apply and protected_jobs:
                    raise ValueError(
                        "workflow task is protected by an active job"
                    )
                if apply:
                    for table in (
                        "inferred_workflow_edges",
                        "tool_operation_events",
                        "workflow_events",
                        "outcomes",
                        "traces",
                    ):
                        self._conn.execute(
                            f"DELETE FROM {table} WHERE task_id = ?", (task_id,)
                        )
                    self._conn.commit()
                else:
                    self._conn.rollback()
                return result
            except Exception:
                self._conn.rollback()
                raise

    def compact(self) -> DatabaseCompaction:
        """Checkpoint and VACUUM a file store under exclusive maintenance."""
        if not self._maintenance:
            raise ValueError("database compaction requires maintenance mode")
        with self._lock:
            bytes_before = os.path.getsize(self._path)
            pages_before = int(
                self._conn.execute("PRAGMA page_count").fetchone()[0]
            )
            checkpoint = self._conn.execute(
                "PRAGMA wal_checkpoint(TRUNCATE)"
            ).fetchone()
            if checkpoint is None or int(checkpoint[0]) != 0:
                raise sqlite3.OperationalError(
                    "database checkpoint is busy; stop external SQLite users"
                )
            self._conn.execute("VACUUM")
            checkpoint = self._conn.execute(
                "PRAGMA wal_checkpoint(TRUNCATE)"
            ).fetchone()
            if checkpoint is None or int(checkpoint[0]) != 0:
                raise sqlite3.OperationalError(
                    "database checkpoint remained busy after compaction"
                )
            pages_after = int(
                self._conn.execute("PRAGMA page_count").fetchone()[0]
            )
            bytes_after = os.path.getsize(self._path)
        return DatabaseCompaction(
            bytes_before=bytes_before,
            bytes_after=bytes_after,
            pages_before=pages_before,
            pages_after=pages_after,
        )

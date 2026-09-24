"""SQLite connection lifecycle and compatibility handling."""

from __future__ import annotations

import asyncio
import os
import sqlite3
import threading
import time

try:
    import fcntl
except ImportError:  # pragma: no cover - exercised on non-POSIX platforms
    fcntl = None  # type: ignore[assignment]

from ctrlrtn.jobs import Job
from ctrlrtn.policy.experiment import Experiment
from ctrlrtn.recorder.models import Outcome
from ctrlrtn.recorder.trace import Trace
from ctrlrtn.workflow.identity import WorkflowEvent
from ctrlrtn.workflow.inference import InferredWorkflowEdge
from ctrlrtn.workflow.metrics import WorkflowStepMetric
from ctrlrtn.workflow.tool_operation import ToolOperationEvent

from . import schema
from .queries import _SELECT_EXPERIMENTS, _SELECT_ROUTES


class SqliteCapability:
    """Typed contract shared by every SQLite capability mixin.

    ``SqliteConnection`` assigns the attributes; the mixins only read them.
    The cross-capability methods declared below are each implemented by
    exactly one mixin and are listed here so the other mixins (and the
    ``save*`` entry points) can call them on ``self``.
    """

    _conn: sqlite3.Connection
    _lock: threading.Lock
    _path: str
    _maintenance: bool
    _has_experiment_provider: bool
    _has_experiment_scope: bool
    _has_route_provider: bool

    def _has_column(self, table: str, name: str) -> bool:
        return schema.has_column(self._conn, table, name)

    def _experiment_select(self) -> str:
        query = _SELECT_EXPERIMENTS
        if not self._has_experiment_provider:
            query = query.replace(
                "candidate_provider", "NULL AS candidate_provider"
            )
        if not self._has_experiment_scope:
            query = query.replace(
                "workflow, workflow_version, step",
                "NULL AS workflow, NULL AS workflow_version, NULL AS step",
            )
        return query

    def _route_select(self) -> str:
        if self._has_route_provider:
            return _SELECT_ROUTES
        return _SELECT_ROUTES.replace("provider", "NULL AS provider")

    # Implemented by TraceSqliteMixin.
    def _insert(self, trace: Trace) -> None:
        raise NotImplementedError

    def _insert_outcome(self, outcome: Outcome) -> None:
        raise NotImplementedError

    # Implemented by WorkflowEventSqliteMixin.
    def _insert_workflow_event(self, event: WorkflowEvent) -> None:
        raise NotImplementedError

    def _insert_tool_operation_event(self, event: ToolOperationEvent) -> None:
        raise NotImplementedError

    def workflow_events(
        self, task_id: str | None = None
    ) -> list[WorkflowEvent]:
        raise NotImplementedError

    def tool_operation_events(
        self, task_id: str | None = None
    ) -> list[ToolOperationEvent]:
        raise NotImplementedError

    # Implemented by WorkflowReportingSqliteMixin.
    def workflow_step_metrics(
        self,
        workflow: str | None = None,
        workflow_version: str | None = None,
    ) -> list[WorkflowStepMetric]:
        raise NotImplementedError

    # Implemented by WorkflowAnalysisSqliteMixin.
    def inferred_workflow_edges(self) -> list[InferredWorkflowEdge]:
        raise NotImplementedError

    # Implemented by JobSqliteMixin.
    def jobs(self, limit: int = 100, *, offset: int = 0) -> list[Job]:
        raise NotImplementedError

    # Implemented by ExperimentControlSqliteMixin.
    def experiment(self, experiment_id: str) -> Experiment | None:
        raise NotImplementedError


class SqliteConnection(SqliteCapability):
    """Own the one connection and lock shared by all capability mixins."""

    def __init__(
        self,
        path: str = "ctrlrtn.db",
        *,
        read_only: bool = False,
        maintenance: bool = False,
    ) -> None:
        if read_only and maintenance:
            raise ValueError("maintenance mode requires a writable store")
        self._lock = threading.Lock()
        self._path = path
        self._maintenance = maintenance
        self._maintenance_lock_fd: int | None = None
        if read_only:
            # A pure reader (the console monitor): open the existing file with
            # SQLite's read-only URI so it can NEVER create the DB, run DDL, flip
            # journal mode, or take a write lock — a missing path raises instead
            # of silently materializing an empty DB. WAL commits from the live
            # gateway are still visible (each SELECT starts a fresh read txn).
            self._conn = sqlite3.connect(
                f"file:{path}?mode=ro", uri=True, check_same_thread=False
            )
            self._conn.execute("PRAGMA busy_timeout=5000")
            self._has_experiment_provider = self._has_column(
                "experiments", "candidate_provider"
            )
            self._has_experiment_scope = self._has_column(
                "experiments", "workflow"
            )
            self._has_route_provider = self._has_column("routes", "provider")
            return
        if path == ":memory:":
            if maintenance:
                raise ValueError("maintenance mode requires a file database")
        elif fcntl is None:
            if maintenance:
                raise ValueError(
                    "maintenance compaction requires POSIX advisory locks"
                )
        else:
            lock_path = f"{os.path.realpath(path)}.maintenance.lock"
            lock_fd = os.open(lock_path, os.O_CREAT | os.O_RDWR, 0o600)
            lock_kind = fcntl.LOCK_EX if maintenance else fcntl.LOCK_SH
            try:
                fcntl.flock(lock_fd, lock_kind | fcntl.LOCK_NB)
            except BlockingIOError as exc:
                os.close(lock_fd)
                label = (
                    "writer is active"
                    if maintenance
                    else "maintenance is active"
                )
                raise sqlite3.OperationalError(
                    f"cannot open router database: {label}"
                ) from exc
            self._maintenance_lock_fd = lock_fd
        try:
            self._conn = sqlite3.connect(path, check_same_thread=False)
        except Exception:
            self._release_maintenance_lock()
            raise
        try:
            self._conn.execute("PRAGMA busy_timeout=5000")
            with self._lock:
                # Schema initialization takes BEGIN IMMEDIATE and therefore
                # serializes concurrent cold starts before any connection tries
                # to change the journal mode.
                schema.initialize(self._conn)
                self._enable_wal()
        except BaseException:
            # A failure here used to leave the advisory lock held with no
            # object owning it, so a later compaction was refused for a writer
            # that did not exist.
            self._conn.close()
            self._release_maintenance_lock()
            raise
        self._has_experiment_provider = True
        self._has_experiment_scope = True
        self._has_route_provider = True

    def _enable_wal(self) -> None:
        """Enable WAL, retrying the one pragma SQLite does not reliably wait.

        WAL lets the control-plane CLI write experiments while the gateway
        writes traces. Unlike BEGIN IMMEDIATE, PRAGMA journal_mode can still
        report SQLITE_BUSY immediately during concurrent startup despite the
        connection's busy_timeout, so retry it within that same five-second
        bound.
        """
        if self._path == ":memory:":
            return  # SQLite's in-memory journal cannot use WAL.
        deadline = time.monotonic() + 5.0
        while True:
            try:
                row = self._conn.execute("PRAGMA journal_mode=WAL").fetchone()
                if row is not None and str(row[0]).lower() == "wal":
                    return
            except sqlite3.OperationalError as exc:
                if (
                    "locked" not in str(exc).lower()
                    and "busy" not in str(exc).lower()
                ) or time.monotonic() >= deadline:
                    raise
            if time.monotonic() >= deadline:
                raise sqlite3.OperationalError(
                    "could not enable WAL journal mode"
                )
            time.sleep(0.01)

    def _release_maintenance_lock(self) -> None:
        if self._maintenance_lock_fd is not None:
            assert fcntl is not None
            fcntl.flock(self._maintenance_lock_fd, fcntl.LOCK_UN)
            os.close(self._maintenance_lock_fd)
            self._maintenance_lock_fd = None

    async def save(self, trace: Trace) -> None:
        await asyncio.to_thread(self._insert, trace)

    async def save_outcome(self, outcome: Outcome) -> None:
        await asyncio.to_thread(self._insert_outcome, outcome)

    async def save_workflow_event(self, event: WorkflowEvent) -> None:
        await asyncio.to_thread(self._insert_workflow_event, event)

    async def save_tool_operation_event(
        self, event: ToolOperationEvent
    ) -> None:
        await asyncio.to_thread(self._insert_tool_operation_event, event)

    def close(self) -> None:
        try:
            self._conn.close()
        finally:
            self._release_maintenance_lock()

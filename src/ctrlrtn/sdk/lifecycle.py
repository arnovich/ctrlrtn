"""Edition, workflow-step, and tool-operation lifecycle context managers."""

from __future__ import annotations

import contextlib
import threading
import time
import uuid
from collections.abc import Iterator

from ctrlrtn.sdk.context import (
    _route,
    _step_identity,
    _task,
    _workflow,
    logger,
    new_task_id,
)
from ctrlrtn.sdk.dispatch import (
    report_outcome,
    report_tool_operation_event,
    report_workflow_event,
)
from ctrlrtn.workflow.identity import WorkflowEvent, WorkflowIdentity
from ctrlrtn.workflow.tool_operation import (
    ToolOperationEvent,
    ToolOperationIdentity,
)

# Count of editions live in THIS process, so the stamping hook can tell "no
# task because we're legitimately outside any edition" from "no task because
# this call is on a thread the edition context didn't reach" (a silent bug).
_active_editions = 0
_active_lock = threading.Lock()


def _edition_active() -> bool:
    with _active_lock:
        return _active_editions > 0


class Edition:
    """Handle for the running edition: its task id, and its one-shot outcome
    report. Reporting is best-effort — a failed report is logged, never raised
    into the app (an eval signal must not break the run it measures)."""

    def __init__(
        self,
        task_id: str,
        report_to: str | None,
        workflow: str | None = None,
        workflow_version: str | None = None,
    ) -> None:
        self.task_id = task_id
        self._report_to = report_to
        self.workflow = workflow
        self.workflow_version = workflow_version
        self._reported = False

    def step(
        self,
        name: str,
        *,
        step_run_id: str | None = None,
        parent_step_run_id: str | None = None,
        dependencies: tuple[str, ...] | list[str] = (),
        attempt: int = 1,
    ) -> Step:
        if self.workflow is None or self.workflow_version is None:
            raise ValueError(
                "edition() requires workflow and workflow_version before step()"
            )
        current = _step_identity.get()
        parent = parent_step_run_id
        if parent is None and current is not None:
            parent = current.step_run_id
        identity = WorkflowIdentity(
            task_id=self.task_id,
            workflow=self.workflow,
            workflow_version=self.workflow_version,
            step=name,
            step_run_id=step_run_id or uuid.uuid4().hex,
            parent_step_run_id=parent,
            dependency_step_run_ids=tuple(dependencies),
            attempt=attempt,
        )
        return Step(identity, self._report_to)

    def report(
        self, *, success: bool | None = None, score: float | None = None
    ) -> None:
        if success is None and score is None:
            raise ValueError("provide success and/or score")
        self._reported = True
        if not self._report_to:
            logger.warning(
                "ctrlrtn: report() called for task %s but the edition has no "
                "report_to; the outcome was dropped (pass report_to=GATEWAY).",
                self.task_id,
            )
            return
        try:
            report_outcome(
                self._report_to,
                self.task_id,
                success=success,
                score=score,
            )
        except Exception:  # an outcome report must never break the edition
            logger.warning(
                "ctrlrtn: failed to report outcome for task %s",
                self.task_id,
                exc_info=True,
            )

    def _finalize(self, failed: bool) -> None:
        # Auto-report a crash as a failure — but only when reporting is wired;
        # an app that set no report_to opted out, so stay silent on that path.
        if failed and not self._reported and self._report_to:
            self.report(success=False)


@contextlib.contextmanager
def edition(
    task_id: str | None = None,
    *,
    default_route: str | None = None,
    report_to: str | None = None,
    workflow: str | None = None,
    workflow_version: str | None = None,
) -> Iterator[Edition]:
    """Open an edition: bind a task id (generated if not given) for every stamped
    call in this block. ``default_route`` sets ``x-ctrlrtn-route`` for the whole
    edition (override per sub-agent with ``route(...)``). ``report_to`` (the
    gateway base URL) enables outcome reporting via the yielded handle; an
    uncaught exception auto-reports failure."""
    global _active_editions
    tid = task_id or new_task_id()
    if (workflow is None) != (workflow_version is None):
        raise ValueError(
            "workflow and workflow_version must be provided together"
        )
    if workflow is not None:
        WorkflowIdentity(
            tid, workflow, workflow_version, "step", uuid.uuid4().hex
        )
    handle = Edition(tid, report_to, workflow, workflow_version)
    with _active_lock:
        _active_editions += 1
    task_token = _task.set(tid)
    route_token = _route.set(default_route)
    workflow_token = _workflow.set(
        (workflow, workflow_version) if workflow is not None else None
    )
    failed = False
    try:
        yield handle
    except BaseException:
        failed = True
        raise
    finally:
        _route.reset(route_token)
        _workflow.reset(workflow_token)
        _task.reset(task_token)
        with _active_lock:
            _active_editions -= 1
        handle._finalize(failed)


class Step:
    """Context manager for one workflow-step invocation."""

    def __init__(
        self, identity: WorkflowIdentity, report_to: str | None
    ) -> None:
        self.identity = identity
        self.step_run_id = identity.step_run_id
        self._report_to = report_to
        self._token = None
        self._terminal = False

    def _emit(
        self,
        status: str,
        *,
        success: bool | None = None,
        score: float | None = None,
        error_code: str | None = None,
    ) -> None:
        if not self._report_to:
            return
        event = WorkflowEvent(
            identity=self.identity,
            status=status,
            success=success,
            score=score,
            error_code=error_code,
        )
        try:
            report_workflow_event(self._report_to, event)
        except Exception:
            logger.warning(
                "ctrlrtn: failed to report workflow event", exc_info=True
            )

    def tool(
        self,
        operation: str,
        *,
        operation_id: str,
        attempt_id: str | None = None,
        attempt: int = 1,
        effect: str = "unknown",
    ) -> ToolOperation:
        """Declare one explicit tool attempt inside this exact step run."""
        identity = ToolOperationIdentity(
            self.identity,
            operation,
            operation_id,
            attempt_id or uuid.uuid4().hex,
            attempt,
            effect,
        )
        return ToolOperation(identity, self._report_to)

    def __enter__(self) -> Step:
        self._token = _step_identity.set(self.identity)
        self._emit("started")
        return self

    def report(
        self,
        *,
        status: str = "completed",
        success: bool | None = None,
        score: float | None = None,
        error_code: str | None = None,
    ) -> None:
        if self._terminal:
            raise RuntimeError("step already has a terminal event")
        if status not in {"completed", "failed", "cancelled", "skipped"}:
            raise ValueError("step report status must be terminal")
        self._emit(status, success=success, score=score, error_code=error_code)
        self._terminal = True

    def __exit__(self, exc_type, exc, traceback) -> bool:
        try:
            if not self._terminal:
                self._emit("failed" if exc_type else "completed")
        finally:
            if self._token is not None:
                _step_identity.reset(self._token)
        return False


class ToolOperation:
    """Context manager that reports one authoritative tool attempt outcome."""

    def __init__(
        self, identity: ToolOperationIdentity, report_to: str | None
    ) -> None:
        self.identity = identity
        self._report_to = report_to
        self._terminal = False
        self._started = 0.0

    def _emit(self, status: str, **outcome) -> None:
        if not self._report_to:
            return
        try:
            report_tool_operation_event(
                self._report_to,
                ToolOperationEvent(self.identity, status, **outcome),
            )
        except Exception:
            logger.warning(
                "ctrlrtn: failed to report tool operation event", exc_info=True
            )

    def __enter__(self) -> ToolOperation:
        self._started = time.monotonic()
        self._emit("started")
        return self

    def report(
        self,
        *,
        status: str = "completed",
        success: bool = True,
        error_code: str | None = None,
        latency_ms: float | None = None,
        cost_usd: float | None = None,
    ) -> None:
        if self._terminal:
            raise RuntimeError("tool attempt already has a terminal event")
        if status not in {"completed", "failed", "cancelled"}:
            raise ValueError("tool report status must be terminal")
        measured = (time.monotonic() - self._started) * 1000
        self._emit(
            status,
            success=success,
            error_code=error_code,
            latency_ms=measured if latency_ms is None else latency_ms,
            cost_usd=cost_usd,
        )
        self._terminal = True

    def __exit__(self, exc_type, exc, traceback) -> bool:
        if not self._terminal:
            self.report(
                status="failed" if exc_type else "completed",
                success=exc_type is None,
                error_code=(type(exc).__name__ if exc is not None else None),
            )
        return False

"""Context propagation and explicit header stamping for the public SDK."""

from __future__ import annotations

import contextlib
import contextvars
import functools
import logging
import os
import uuid
from collections.abc import Callable, Iterator, Mapping

from ctrlrtn.workflow.identity import WorkflowIdentity

logger = logging.getLogger("ctrlrtn.sdk")

_TASK_HEADER = "x-ctrlrtn-task"
_ROUTE_HEADER = "x-ctrlrtn-route"
_STRICT_ENV = "CTRLRTN_STRICT"

# Edition id (per run) and use-case route (per sub-agent). Separate because one
# edition's calls share a task id but may span several use-cases.
_task: contextvars.ContextVar[str | None] = contextvars.ContextVar(
    "ctrlrtn_task", default=None
)
_route: contextvars.ContextVar[str | None] = contextvars.ContextVar(
    "ctrlrtn_route", default=None
)
_workflow: contextvars.ContextVar[tuple[str, str] | None] = (
    contextvars.ContextVar("ctrlrtn_workflow", default=None)
)
_step_identity: contextvars.ContextVar[WorkflowIdentity | None] = (
    contextvars.ContextVar("ctrlrtn_step_identity", default=None)
)


def _strict() -> bool:
    return os.getenv(_STRICT_ENV, "").strip().lower() in (
        "1",
        "true",
        "yes",
        "on",
    )


def new_task_id() -> str:
    """A fresh globally-unique edition id. Use unique ids per edition — a reused
    id merges two runs into one task and corrupts assignment/analysis. (Reuse
    rejection is the gateway's job; this only *generates* unique defaults.)"""
    return uuid.uuid4().hex


def current_task_id() -> str | None:
    """The task id of the active edition, or ``None`` outside one (including
    on a thread or subprocess the edition context did not reach)."""
    return _task.get()


def current_route() -> str | None:
    """The use-case route in effect here: the innermost ``route(...)``, else
    the edition's ``default_route``, else ``None`` (the gateway then keys the
    use-case by request fingerprint)."""
    return _route.get()


def bind(func: Callable) -> Callable:
    """Wrap ``func`` so it runs in a COPY of the current edition context, taken
    now. Pass the result across a thread/process boundary so stamping survives
    it: ``executor.submit(sdk.bind(work), arg)``. Call ``bind`` inside the
    ``edition(...)`` block so it captures the live task id."""
    ctx = contextvars.copy_context()

    @functools.wraps(func)
    def _run(*args, **kwargs):
        return ctx.run(func, *args, **kwargs)

    return _run


def stamp(
    headers: dict[str, str],
    *,
    task_id: str | None = None,
    route: str | None = None,
    workflow_identity: WorkflowIdentity | Mapping | None = None,
) -> dict[str, str]:
    """Add the ctrlrtn headers to ``headers`` from the given values or the current
    edition context. Mutates and returns ``headers`` (the explicit-threading path
    for calls that don't go through a ctrlrtn httpx client)."""
    tid = task_id or _task.get()
    if tid:
        headers[_TASK_HEADER] = tid
    rt = route or _route.get()
    if rt:
        headers.setdefault(_ROUTE_HEADER, rt)
    identity = workflow_identity or _step_identity.get()
    if identity is not None:
        if not isinstance(identity, WorkflowIdentity):
            identity = WorkflowIdentity.from_carrier(identity)
        if tid and identity.task_id != tid:
            raise ValueError(
                "workflow identity task_id does not match stamped task"
            )
        headers.update(identity.headers())
    return headers


@contextlib.contextmanager
def route(name: str) -> Iterator[None]:
    """Override the use-case route (``x-ctrlrtn-route``) for calls in this block —
    e.g. one per sub-agent. Optional: without it the gateway keys the use-case by
    request fingerprint."""
    token = _route.set(name)
    try:
        yield
    finally:
        _route.reset(token)


def export_carrier() -> dict:
    """The active workflow step's identity as a JSON-safe dict, to hand to a
    subprocess or remote worker that will ``import_carrier`` it. Requires an
    open ``Edition.step(...)`` block; raises ``ValueError`` outside one."""
    identity = _step_identity.get()
    if identity is None:
        raise ValueError("no active workflow step to export")
    return identity.carrier()


@contextlib.contextmanager
def import_carrier(carrier: Mapping) -> Iterator[WorkflowIdentity]:
    """Adopt a workflow identity exported by ``export_carrier`` for the calls
    in this block, so stamped requests from a subprocess or remote worker
    continue the originating step run under the same task id. Yields the
    parsed ``WorkflowIdentity``; a malformed carrier raises
    ``WorkflowIdentityError`` before anything is bound."""
    identity = WorkflowIdentity.from_carrier(carrier)
    task_token = _task.set(identity.task_id)
    workflow_token = _workflow.set(
        (identity.workflow, identity.workflow_version)
    )
    step_token = _step_identity.set(identity)
    try:
        yield identity
    finally:
        _step_identity.reset(step_token)
        _workflow.reset(workflow_token)
        _task.reset(task_token)

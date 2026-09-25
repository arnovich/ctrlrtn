"""HTTP transport for SDK outcomes and lifecycle events."""

from __future__ import annotations

import httpx

from ctrlrtn.sdk.context import _task
from ctrlrtn.workflow.identity import WorkflowEvent
from ctrlrtn.workflow.tool_operation import ToolOperationEvent

_OUTCOME_PATH = "/ctrlrtn/outcome"
_WORKFLOW_EVENT_PATH = "/ctrlrtn/workflow-events"
_TOOL_OPERATION_EVENT_PATH = "/ctrlrtn/tool-operation-events"
_OUTCOME_TIMEOUT = 5.0  # cap the outcome POST so an unwind can't hang on it


def _payload(task_id: str, success: bool | None, score: float | None) -> dict:
    if success is None and score is None:
        raise ValueError("provide success and/or score")
    payload: dict = {"task_id": task_id}
    if success is not None:
        payload["success"] = success
    if score is not None:
        payload["score"] = score
    return payload


def _resolve_task_id(task_id: str | None) -> str:
    tid = task_id or _task.get()
    if not tid:
        raise ValueError(
            "no task_id — call inside task() or pass task_id explicitly"
        )
    return tid


def report_outcome(
    base_url: str,
    task_id: str | None = None,
    *,
    success: bool | None = None,
    score: float | None = None,
    client: httpx.Client | None = None,
) -> None:
    """POST a task's result to ``{base_url}/ctrlrtn/outcome``. ``task_id``
    defaults to the current task. Raises on a transport/HTTP error (the
    ``Task.report`` path swallows and logs; a direct caller may want to know).
    """
    tid = _resolve_task_id(task_id)
    url = base_url.rstrip("/") + _OUTCOME_PATH
    payload = _payload(tid, success, score)
    owned = client is None
    client = client or httpx.Client(timeout=_OUTCOME_TIMEOUT)
    try:
        resp = client.post(
            url, json=payload, headers={"content-type": "application/json"}
        )
        resp.raise_for_status()
    finally:
        if owned:
            client.close()


async def areport_outcome(
    base_url: str,
    task_id: str | None = None,
    *,
    success: bool | None = None,
    score: float | None = None,
    client: httpx.AsyncClient | None = None,
) -> None:
    """Async ``report_outcome``."""
    tid = _resolve_task_id(task_id)
    url = base_url.rstrip("/") + _OUTCOME_PATH
    payload = _payload(tid, success, score)
    owned = client is None
    client = client or httpx.AsyncClient(timeout=_OUTCOME_TIMEOUT)
    try:
        resp = await client.post(
            url, json=payload, headers={"content-type": "application/json"}
        )
        resp.raise_for_status()
    finally:
        if owned:
            await client.aclose()


def report_workflow_event(
    base_url: str,
    event: WorkflowEvent,
    *,
    client: httpx.Client | None = None,
) -> None:
    """POST one idempotent workflow lifecycle event to the local gateway."""
    url = base_url.rstrip("/") + _WORKFLOW_EVENT_PATH
    owned = client is None
    client = client or httpx.Client(timeout=_OUTCOME_TIMEOUT)
    try:
        response = client.post(
            url,
            json=event.payload(),
            headers={"content-type": "application/json"},
        )
        response.raise_for_status()
    finally:
        if owned:
            client.close()


def report_tool_operation_event(
    base_url: str,
    event: ToolOperationEvent,
    *,
    client: httpx.Client | None = None,
) -> None:
    """POST one idempotent authoritative tool lifecycle event."""
    url = base_url.rstrip("/") + _TOOL_OPERATION_EVENT_PATH
    owned = client is None
    client = client or httpx.Client(timeout=_OUTCOME_TIMEOUT)
    try:
        response = client.post(
            url,
            json=event.payload(),
            headers={"content-type": "application/json"},
        )
        response.raise_for_status()
    finally:
        if owned:
            client.close()

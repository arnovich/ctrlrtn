"""Stable tool identities and explicit outcomes remain authoritative facts."""

import httpx
import pytest

from ctrlrtn import sdk
from ctrlrtn.recorder.sqlite.store import SqliteTraceStore
from ctrlrtn.workflow.identity import (
    WorkflowIdentity,
    WorkflowIdentityError,
)
from ctrlrtn.workflow.step_detail import render_workflow_step_detail
from ctrlrtn.workflow.tool_operation import (
    ToolOperationEvent,
    ToolOperationIdentity,
)


def _workflow() -> WorkflowIdentity:
    return WorkflowIdentity(
        "task-1",
        "pipeline",
        "v1",
        "research",
        "step-run-1",
        attempt=2,
    )


def test_tool_event_payload_round_trip_requires_explicit_terminal_outcome():
    identity = ToolOperationIdentity(
        _workflow(), "search", "search:query-7", "attempt-2", 2, "idempotent"
    )
    event = ToolOperationEvent(
        identity,
        "completed",
        event_id="event-1",
        success=True,
        latency_ms=12.5,
        cost_usd=0.02,
        ts=10,
    )
    assert ToolOperationEvent.from_payload(event.payload()) == event
    with pytest.raises(WorkflowIdentityError, match="explicit success"):
        ToolOperationEvent(identity, "failed")
    with pytest.raises(WorkflowIdentityError, match="effect contract"):
        ToolOperationIdentity(
            _workflow(), "search", "operation", "attempt", effect="writes-ish"
        )


def test_store_persists_tool_identity_attempt_effect_and_outcome(tmp_path):
    store = SqliteTraceStore(str(tmp_path / "tools.db"))
    identity = ToolOperationIdentity(
        _workflow(), "search", "search:query-7", "attempt-2", 2, "idempotent"
    )
    store._insert_tool_operation_event(
        ToolOperationEvent(identity, "started", event_id="start", ts=1)
    )
    store._insert_tool_operation_event(
        ToolOperationEvent(
            identity,
            "completed",
            event_id="finish",
            success=True,
            latency_ms=25,
            cost_usd=0.01,
            ts=2,
        )
    )
    assert store.tool_operation_events("task-1")[1].identity == identity
    detail = store.workflow_step_detail("task-1", "step-run-1")
    assert "operation=search:query-7" in render_workflow_step_detail(detail)
    store.close()


def test_sdk_tool_context_reports_started_and_terminal_events(monkeypatch):
    payloads = []

    def report(_base_url, event):
        payloads.append(event.payload())

    monkeypatch.setattr(sdk, "report_tool_operation_event", report)
    step = sdk.Step(_workflow(), "http://router")
    with step.tool(
        "search",
        operation_id="search:query-7",
        attempt_id="attempt-1",
        effect="idempotent",
    ):
        pass
    assert [payload["status"] for payload in payloads] == [
        "started",
        "completed",
    ]
    assert payloads[1]["success"] is True
    assert payloads[1]["latency_ms"] >= 0


def test_direct_report_uses_dedicated_control_endpoint():
    requests = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200)

    identity = ToolOperationIdentity(
        _workflow(), "search", "operation", "attempt", effect="unknown"
    )
    event = ToolOperationEvent(identity, "started")
    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        sdk.report_tool_operation_event("http://router", event, client=client)
    assert requests[0].url.path == "/ctrlrtn/tool-operation-events"

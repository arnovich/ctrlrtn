"""Workflow identities stay explicit, portable, durable, and off upstreams."""

from __future__ import annotations

import sqlite3

import httpx
import pytest

from ctrlrtn import sdk
from ctrlrtn.config import Settings
from ctrlrtn.gateway.app import create_app
from ctrlrtn.gateway.proxy import _forward_request_headers
from ctrlrtn.recorder.recorder import Recorder
from ctrlrtn.recorder.store import InMemoryTraceStore, SqliteTraceStore
from ctrlrtn.recorder.trace import Trace
from ctrlrtn.telemetry.enrich import enrich_trace
from ctrlrtn.workflow.identity import (
    WorkflowEvent,
    WorkflowIdentity,
    WorkflowIdentityError,
    identity_from_headers,
)


def _identity(**overrides) -> WorkflowIdentity:
    values = {
        "task_id": "task-1",
        "workflow": "article-pipeline",
        "workflow_version": "git:abc123",
        "step": "draft",
        "step_run_id": "run-1",
    }
    values.update(overrides)
    return WorkflowIdentity(**values)


def _trace(headers: dict[str, str]) -> Trace:
    return Trace(
        method="POST",
        path="/v1/messages",
        query="",
        request_headers=headers,
        request_body=b"{}",
        status_code=200,
        response_headers={},
        response_body=b"{}",
        latency_ms=1.0,
    )


def test_identity_round_trips_headers_and_carrier():
    identity = _identity(
        parent_step_run_id="run-parent",
        dependency_step_run_ids=("run-a", "run-b"),
        attempt=2,
    )
    parsed, error = identity_from_headers(identity.headers())
    assert error is None and parsed == identity
    assert WorkflowIdentity.from_carrier(identity.carrier()) == identity


def test_partial_and_malformed_identity_are_diagnostics_not_identity():
    identity, error = identity_from_headers(
        {"x-ctrlrtn-task": "task-1", "x-ctrlrtn-workflow": "workflow"}
    )
    assert identity is None and "partial workflow identity" in error
    with pytest.raises(WorkflowIdentityError, match="invalid characters"):
        _identity(step="unsafe,step")
    with pytest.raises(WorkflowIdentityError, match="depend on itself"):
        _identity(dependency_step_run_ids=("run-1",))


def test_sdk_step_stamps_complete_identity_and_carrier(monkeypatch):
    captured: dict = {}
    events: list[WorkflowEvent] = []
    monkeypatch.setattr(
        sdk, "report_workflow_event", lambda base, event: events.append(event)
    )

    def handler(request: httpx.Request) -> httpx.Response:
        captured.update(request.headers)
        return httpx.Response(200, json={})

    with (
        sdk.task(
            task_id="task-1",
            workflow="article-pipeline",
            workflow_version="git:abc123",
            report_to="http://gateway",
        ) as run,
        run.step("draft", step_run_id="run-1"),
    ):
        carrier = sdk.export_carrier()
        client = sdk.http_client(transport=httpx.MockTransport(handler))
        client.post("http://gateway/v1/messages")
        client.close()
    assert captured["x-ctrlrtn-step"] == "draft"
    assert captured["x-ctrlrtn-workflow-version"] == "git:abc123"
    assert carrier["step_run_id"] == "run-1"
    assert [event.status for event in events] == ["started", "completed"]


def test_imported_carrier_and_bound_thread_preserve_complete_context():
    identity = _identity()
    with sdk.import_carrier(identity.carrier()):
        assert sdk.export_carrier() == identity.carrier()
        headers = sdk.stamp({})
        assert headers["x-ctrlrtn-task"] == "task-1"
        assert headers["x-ctrlrtn-step-run"] == "run-1"


def test_step_failure_and_explicit_terminal_do_not_double_report(monkeypatch):
    events: list[WorkflowEvent] = []
    monkeypatch.setattr(
        sdk, "report_workflow_event", lambda base, event: events.append(event)
    )
    with sdk.task(
        task_id="task-1", workflow="wf", workflow_version="v1", report_to="gw"
    ) as run:
        with run.step("ok") as step:
            step.report(status="completed", success=True, score=0.8)
        with pytest.raises(RuntimeError), run.step("bad"):
            raise RuntimeError("boom")
    assert [event.status for event in events] == [
        "started",
        "completed",
        "started",
        "failed",
    ]


def test_enrichment_keeps_valid_identity_and_marks_partial_identity():
    valid = _trace(_identity().headers())
    enrich_trace(valid)
    assert valid.workflow == "article-pipeline"
    assert valid.step_run_id == "run-1"
    assert valid.workflow_identity_error is None

    partial = _trace(
        {"x-ctrlrtn-task": "task-1", "x-ctrlrtn-workflow": "article-pipeline"}
    )
    enrich_trace(partial)
    assert partial.workflow is None
    assert "partial workflow identity" in partial.workflow_identity_error


async def test_sqlite_persists_identity_events_idempotently_and_diagnostics(
    tmp_path,
):
    store = SqliteTraceStore(str(tmp_path / "workflow.db"))
    identity = _identity(dependency_step_run_ids=("run-a",), attempt=2)
    trace = _trace(identity.headers())
    enrich_trace(trace)
    await store.save(trace)
    started = WorkflowEvent(identity, "started", event_id="event-1")
    await store.save_workflow_event(started)
    await store.save_workflow_event(started)
    await store.save_workflow_event(
        WorkflowEvent(identity, "completed", event_id="event-2", success=True)
    )
    try:
        row = store.get(1)
        assert row["workflow"] == "article-pipeline"
        assert row["dependency_step_run_ids"] == ("run-a",)
        assert row["step_attempt"] == 2
        assert [event.status for event in store.workflow_events("task-1")] == [
            "started",
            "completed",
        ]
        assert store.workflow_diagnostics()["valid_traces"] == 1
    finally:
        store.close()


async def test_sqlite_migrates_legacy_trace_table_additively(tmp_path):
    path = tmp_path / "legacy.db"
    connection = sqlite3.connect(path)
    connection.execute("""CREATE TABLE traces (
            id INTEGER PRIMARY KEY AUTOINCREMENT, ts REAL NOT NULL,
            method TEXT NOT NULL, path TEXT NOT NULL, query TEXT NOT NULL,
            status_code INTEGER NOT NULL, latency_ms REAL NOT NULL, model TEXT,
            input_tokens INTEGER, output_tokens INTEGER, use_case_key TEXT,
            request_headers TEXT NOT NULL, request_body BLOB,
            response_headers TEXT NOT NULL, response_body BLOB
        )""")
    connection.commit()
    connection.close()
    store = SqliteTraceStore(str(path))
    try:
        trace = _trace(_identity().headers())
        enrich_trace(trace)
        await store.save(trace)
        assert store.get(1)["step_run_id"] == "run-1"
    finally:
        store.close()


@pytest.mark.parametrize(
    "make_store",
    [InMemoryTraceStore, lambda: SqliteTraceStore(":memory:")],
    ids=["memory", "sqlite"],
)
async def test_diagnostics_flag_reused_runs_and_conflicting_terminals(
    make_store,
):
    store = make_store()
    first = _identity()
    reused_first = _identity(step_run_id="run-reused")
    reused_second = _identity(task_id="task-2", step_run_id="run-reused")
    cross_task_dependency = _identity(
        task_id="task-2",
        step_run_id="run-2",
        dependency_step_run_ids=("run-1",),
    )
    await store.save_workflow_event(
        WorkflowEvent(first, "completed", event_id="e1")
    )
    await store.save_workflow_event(
        WorkflowEvent(first, "failed", event_id="e2")
    )
    await store.save_workflow_event(
        WorkflowEvent(reused_first, "started", event_id="e3")
    )
    await store.save_workflow_event(
        WorkflowEvent(reused_second, "started", event_id="e4")
    )
    await store.save_workflow_event(
        WorkflowEvent(cross_task_dependency, "started", event_id="e5")
    )
    try:
        diagnostics = store.workflow_diagnostics()
        assert diagnostics["reused_step_run_ids"] == 1
        assert diagnostics["conflicting_terminal_runs"] == 1
        assert diagnostics["cross_task_dependencies"] == 1
    finally:
        if isinstance(store, SqliteTraceStore):
            store.close()


async def test_workflow_event_endpoint_validates_and_is_idempotent():
    store = InMemoryTraceStore()
    app = create_app(
        Settings(upstream_base_url="http://upstream"), recorder=Recorder(store)
    )
    event = WorkflowEvent(_identity(), "started", event_id="event-1")
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://localhost"
    ) as client:
        first = await client.post(
            "/ctrlrtn/workflow-events", json=event.payload()
        )
        second = await client.post(
            "/ctrlrtn/workflow-events", json=event.payload()
        )
        bad = await client.post(
            "/ctrlrtn/workflow-events", json={"task_id": "partial"}
        )
    assert first.status_code == second.status_code == 200
    assert bad.status_code == 400
    assert len(store.workflow_events()) == 1


def test_all_ctrlrtn_control_headers_are_removed_before_upstream():
    forwarded = _forward_request_headers(
        httpx.Headers(
            {
                "authorization": "Bearer client-key",
                "x-ctrlrtn-task": "task-1",
                "x-ctrlrtn-workflow": "wf",
                "x-ctrlrtn-custom-future-field": "private",
                "x-app-header": "kept",
            }
        )
    )
    assert forwarded["authorization"] == "Bearer client-key"
    assert forwarded["x-app-header"] == "kept"
    assert not any(key.lower().startswith("x-ctrlrtn-") for key in forwarded)


def test_event_payload_rejects_unknown_fields_and_non_finite_score():
    payload = WorkflowEvent(_identity(), "completed").payload()
    payload["unknown"] = True
    with pytest.raises(WorkflowIdentityError, match="unknown event fields"):
        WorkflowEvent.from_payload(payload)
    with pytest.raises(WorkflowIdentityError, match="finite"):
        WorkflowEvent(_identity(), "completed", score=float("nan"))

"""Step metrics use explicit step facts and never inherit task outcomes."""

from __future__ import annotations

import pytest

from ctrlrtn.cli import commands as cli
from ctrlrtn.recorder.models import Outcome
from ctrlrtn.recorder.sqlite.store import SqliteTraceStore
from ctrlrtn.recorder.trace import Trace
from ctrlrtn.workflow.identity import WorkflowEvent, WorkflowIdentity
from ctrlrtn.workflow.metrics import build_workflow_step_metrics


def _identity(
    run: str,
    *,
    step: str = "draft",
    task: str = "task-1",
    version: str = "v1",
) -> WorkflowIdentity:
    return WorkflowIdentity(
        task_id=task,
        workflow="article-pipeline",
        workflow_version=version,
        step=step,
        step_run_id=run,
    )


def _row(
    run: str,
    *,
    status=200,
    cost=0.1,
    latency=10.0,
    step="draft",
    task="task-1",
    version="v1",
):
    return {
        "task_id": task,
        "workflow": "article-pipeline",
        "workflow_version": version,
        "step": step,
        "step_run_id": run,
        "status_code": status,
        "cost_usd": cost,
        "input_tokens": 10,
        "output_tokens": 2,
        "latency_ms": latency,
    }


def test_metrics_aggregate_calls_duration_and_explicit_outcomes():
    identity = _identity("run-1")
    events = [
        WorkflowEvent(identity, "started", event_id="e1", ts=1.0),
        WorkflowEvent(
            identity,
            "completed",
            event_id="e2",
            ts=1.25,
            success=True,
            score=0.8,
        ),
    ]
    rows = build_workflow_step_metrics(
        [_row("run-1"), _row("run-1", status=500, latency=30, cost=0.2)],
        events,
    )
    metric = rows[0]
    assert metric.runs == 1 and metric.calls == 2 and metric.call_errors == 1
    assert metric.cost_usd == pytest.approx(0.3)
    assert metric.avg_call_latency_ms == 20
    assert metric.avg_run_duration_ms == 250
    assert metric.completed == 1
    assert metric.successful_outcomes == 1
    assert metric.avg_score == 0.8
    assert metric.unreported_runs == 0


def test_event_only_skipped_step_is_visible_without_calls():
    identity = _identity("run-skip", step="publish")
    rows = build_workflow_step_metrics(
        [], [WorkflowEvent(identity, "skipped", success=False)]
    )
    assert rows[0].calls == 0 and rows[0].skipped == 1
    assert rows[0].failed_outcomes == 1


def test_active_and_conflicting_runs_are_not_presented_as_success():
    active = _identity("active")
    conflict = _identity("conflict")
    rows = build_workflow_step_metrics(
        [_row("active"), _row("conflict")],
        [
            WorkflowEvent(active, "started", event_id="a"),
            WorkflowEvent(conflict, "completed", event_id="c1", success=True),
            WorkflowEvent(conflict, "failed", event_id="c2", success=False),
        ],
    )
    metric = rows[0]
    assert metric.active == 1 and metric.inconsistent == 1
    assert metric.completed == metric.failed == 0
    assert metric.reported_outcomes == 0
    assert metric.unreported_runs == 2


def _trace(identity: WorkflowIdentity, cost=0.1) -> Trace:
    return Trace(
        method="POST",
        path="/v1/messages",
        query="",
        request_headers={},
        request_body=b"{}",
        status_code=200,
        response_headers={},
        response_body=b"{}",
        latency_ms=10,
        input_tokens=10,
        output_tokens=2,
        cost_usd=cost,
        task_id=identity.task_id,
        workflow=identity.workflow,
        workflow_version=identity.workflow_version,
        step=identity.step,
        step_run_id=identity.step_run_id,
        step_attempt=1,
    )


def test_sqlite_cli_filters_versions_and_does_not_inherit_task_outcome(
    tmp_path, monkeypatch, capsys
):
    path = str(tmp_path / "router.db")
    store = SqliteTraceStore(path)
    v1 = _identity("run-v1")
    v2 = _identity("run-v2", version="v2")
    store._insert(_trace(v1))
    store._insert(_trace(v2))
    store._insert_workflow_event(
        WorkflowEvent(v1, "completed", event_id="v1-terminal")
    )
    store._insert_workflow_event(
        WorkflowEvent(v2, "completed", event_id="v2-terminal", success=True)
    )
    store._insert_outcome(Outcome("task-1", success=True, score=1.0))
    try:
        rows = store.workflow_step_metrics("article-pipeline", "v1")
        assert len(rows) == 1 and rows[0].workflow_version == "v1"
        assert rows[0].reported_outcomes == 0
        assert rows[0].unreported_runs == 1
    finally:
        store.close()

    monkeypatch.setenv("CTRLRTN_DB", path)
    cli.main(
        [
            "workflow",
            "steps",
            "--workflow",
            "article-pipeline",
            "--version",
            "v1",
        ]
    )
    output = capsys.readouterr().out
    assert "article-pipeline@v1" in output
    assert "task outcomes are not inherited" in output


def test_identity_collision_is_excluded_and_diagnosed(tmp_path):
    store = SqliteTraceStore(str(tmp_path / "router.db"))
    first = _identity("shared", step="draft")
    second = _identity("shared", step="review")
    store._insert(_trace(first))
    store._insert(_trace(second))
    try:
        assert store.workflow_step_metrics() == []
        assert store.workflow_diagnostics()["reused_step_run_ids"] == 1
    finally:
        store.close()

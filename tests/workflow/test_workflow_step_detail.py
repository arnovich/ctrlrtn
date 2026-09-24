from ctrlrtn.cli import commands as cli
from ctrlrtn.jobs import Job
from ctrlrtn.recorder.sqlite.store import SqliteTraceStore
from ctrlrtn.recorder.trace import Trace
from ctrlrtn.workflow.identity import WorkflowEvent, WorkflowIdentity
from ctrlrtn.workflow.step_detail import render_workflow_step_detail


def test_step_detail_connects_events_traces_routes_experiments_and_jobs(
    tmp_path, monkeypatch, capsys
):
    path = str(tmp_path / "router.db")
    store = SqliteTraceStore(path)
    identity = WorkflowIdentity(
        "task-1", "pipeline", "v1", "draft", "run-draft"
    )
    trace = Trace(
        method="POST",
        path="/v1/messages",
        query="",
        request_headers={},
        request_body=b"secret body",
        status_code=200,
        response_headers={},
        response_body=b"secret response",
        latency_ms=12,
        cost_usd=0.25,
        provider="anthropic",
        served_model="opus",
        task_id="task-1",
        workflow="pipeline",
        workflow_version="v1",
        step="draft",
        step_run_id="run-draft",
        step_attempt=1,
        experiment_id="exp:one",
        arm="candidate",
        route_rule_scope="step",
        route_rule_key="pipeline@v1/draft",
        control_revision="abc123",
    )
    store._insert(trace)
    store._insert_workflow_event(
        WorkflowEvent(identity, "started", event_id="event-1", ts=1)
    )
    store._insert_workflow_event(
        WorkflowEvent(
            identity,
            "completed",
            event_id="event-2",
            ts=2,
            success=True,
            score=0.9,
        )
    )
    store.create_job(
        Job(
            "replay_eval",
            {"workflow": "pipeline", "workflow_version": "v1", "step": "draft"},
            job_id="job:step",
        )
    )
    detail = store.workflow_step_detail("task-1", "run-draft")
    text = render_workflow_step_detail(detail)
    assert "success=True score=0.9" in text
    assert "trace #1 status=200 provider=anthropic model=opus" in text
    assert "experiment=exp:one role=candidate" in text
    assert "route=step:pipeline@v1/draft revision=abc123" in text
    assert "job:step replay_eval queued" in text
    assert "secret body" not in text and "secret response" not in text
    store.close()

    monkeypatch.setattr(cli, "_db_path", lambda: path)
    cli.main(["workflow", "step-detail", "task-1", "run-draft"])
    assert "step run: run-draft" in capsys.readouterr().out


def test_missing_step_detail_is_none(tmp_path):
    store = SqliteTraceStore(tmp_path / "router.db")
    assert store.workflow_step_detail("missing", "missing") is None
    store.close()

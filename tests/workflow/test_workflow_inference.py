"""Offline workflow inference is exact-ID, measurable, and analysis-only."""

from __future__ import annotations

import json

from ctrlrtn.cli import commands as cli
from ctrlrtn.recorder.sqlite.store import SqliteTraceStore
from ctrlrtn.recorder.trace import Trace
from ctrlrtn.workflow.identity import WorkflowEvent, WorkflowIdentity
from ctrlrtn.workflow.inference import ALGORITHM, infer_workflow_edges


def _row(
    trace_id: int,
    run: str,
    *,
    request: dict | None = None,
    response: dict | None = None,
    task: str = "task-1",
) -> dict:
    return {
        "id": trace_id,
        "task_id": task,
        "workflow": "article-pipeline",
        "workflow_version": "v1",
        "step_run_id": run,
        "request_body": json.dumps(request or {}).encode(),
        "response_body": json.dumps(response or {}).encode(),
    }


def _anthropic_producer(trace_id=1, run="research", tool_id="tool-1"):
    return _row(
        trace_id,
        run,
        response={
            "content": [{"type": "tool_use", "id": tool_id, "name": "search"}]
        },
    )


def _anthropic_consumer(
    trace_id=2, run="draft", tool_id="tool-1", task="task-1"
):
    return _row(
        trace_id,
        run,
        request={
            "messages": [
                {
                    "role": "user",
                    "content": [
                        {"type": "tool_result", "tool_use_id": tool_id}
                    ],
                }
            ]
        },
        task=task,
    )


def test_exact_anthropic_tool_id_link_is_confirmed_and_scored():
    explicit = {("task-1", "research", "draft")}
    report = infer_workflow_edges(
        [_anthropic_producer(), _anthropic_consumer()], explicit
    )
    assert len(report.edges) == 1
    edge = report.edges[0]
    assert (edge.source_step_run_id, edge.target_step_run_id) == (
        "research",
        "draft",
    )
    assert edge.confirmation == "confirmed"
    assert len(edge.evidence_hash) == 64 and "tool-1" not in edge.evidence_hash
    assert report.precision == report.recall == 1.0


def test_openai_tool_ids_are_supported_and_uninstrumented_is_unverified():
    producer = _row(
        1,
        "plan",
        response={"choices": [{"message": {"tool_calls": [{"id": "call-1"}]}}]},
    )
    consumer = _row(
        2,
        "execute",
        request={"messages": [{"role": "tool", "tool_call_id": "call-1"}]},
    )
    report = infer_workflow_edges([producer, consumer], set())
    assert report.edges[0].confirmation == "unverified"
    assert report.precision is None and report.recall is None


def test_ambiguous_or_cross_task_evidence_is_not_inferred():
    traces = [
        _anthropic_producer(1, "a"),
        _anthropic_producer(2, "b"),
        _anthropic_consumer(3, "c"),
        _anthropic_consumer(4, "other", task="task-2"),
    ]
    report = infer_workflow_edges(traces, set())
    assert report.edges == ()
    assert report.ambiguous_evidence == 1


def test_accumulated_history_links_only_the_first_tool_result_occurrence():
    report = infer_workflow_edges(
        [
            _anthropic_producer(),
            _anthropic_consumer(2, "draft"),
            _anthropic_consumer(3, "review"),
        ],
        set(),
    )
    assert len(report.edges) == 1
    assert report.edges[0].target_step_run_id == "draft"


def test_explicit_target_with_no_dependencies_contradicts_inferred_edge():
    report = infer_workflow_edges(
        [_anthropic_producer(), _anthropic_consumer()],
        set(),
        {("task-1", "draft")},
    )
    assert report.edges[0].confirmation == "contradicted"
    assert report.false_positive == 1


def test_missing_and_wrong_explicit_edges_affect_recall_and_precision():
    explicit = {
        ("task-1", "other", "draft"),
        ("task-1", "research", "publish"),
    }
    report = infer_workflow_edges(
        [_anthropic_producer(), _anthropic_consumer()], explicit
    )
    assert report.edges[0].confirmation == "contradicted"
    assert report.true_positive == 0
    assert report.false_positive == 1
    assert report.false_negative == 2


def _trace(run: str, request: dict, response: dict) -> Trace:
    return Trace(
        method="POST",
        path="/v1/messages",
        query="",
        request_headers={},
        request_body=json.dumps(request).encode(),
        status_code=200,
        response_headers={},
        response_body=json.dumps(response).encode(),
        latency_ms=1,
        task_id="task-1",
        workflow="article-pipeline",
        workflow_version="v1",
        step=run,
        step_run_id=run,
        step_attempt=1,
    )


def _identity(run: str, dependencies=()) -> WorkflowIdentity:
    return WorkflowIdentity(
        task_id="task-1",
        workflow="article-pipeline",
        workflow_version="v1",
        step=run,
        step_run_id=run,
        dependency_step_run_ids=tuple(dependencies),
    )


def test_sqlite_and_cli_rebuild_persisted_inference(
    tmp_path, monkeypatch, capsys
):
    path = str(tmp_path / "router.db")
    store = SqliteTraceStore(path)
    store._insert(
        _trace(
            "research",
            {},
            {"content": [{"type": "tool_use", "id": "tool-1"}]},
        )
    )
    store._insert(
        _trace(
            "draft",
            {
                "messages": [
                    {
                        "content": [
                            {
                                "type": "tool_result",
                                "tool_use_id": "tool-1",
                            }
                        ]
                    }
                ]
            },
            {},
        )
    )
    store._insert_workflow_event(
        WorkflowEvent(_identity("research"), "started", event_id="event-1")
    )
    store._insert_workflow_event(
        WorkflowEvent(
            _identity("draft", ("research",)),
            "started",
            event_id="event-2",
        )
    )
    store.close()

    monkeypatch.setenv("CTRLRTN_DB", path)
    cli.main(["workflow", "infer"])
    output = capsys.readouterr().out
    assert "precision=100.0% recall=100.0%" in output
    assert "analysis-only" in output

    reader = SqliteTraceStore(path, read_only=True)
    try:
        edges = reader.inferred_workflow_edges()
    finally:
        reader.close()
    assert len(edges) == 1
    assert edges[0].algorithm == ALGORITHM
    assert edges[0].confirmation == "confirmed"

    cli.main(["workflow", "inferred"])
    assert "research" in capsys.readouterr().out
    cli.main(["workflow", "diagnostics"])
    assert "Lifecycle consistency" in capsys.readouterr().out

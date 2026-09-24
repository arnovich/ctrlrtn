"""Workflow recommendations are evidence-labelled and never executable."""

from ctrlrtn.cli import commands as cli
from ctrlrtn.control_config import (
    WorkflowDefinition,
    WorkflowStepDefinition,
)
from ctrlrtn.recorder.sqlite.store import SqliteTraceStore
from ctrlrtn.recorder.trace import Trace
from ctrlrtn.workflow.graph import WorkflowGraph, WorkflowGraphNode
from ctrlrtn.workflow.identity import WorkflowEvent, WorkflowIdentity
from ctrlrtn.workflow.metrics import WorkflowStepMetric
from ctrlrtn.workflow.recommend import (
    build_workflow_recommendations,
    render_workflow_recommendations,
)


def _metric(
    step: str,
    *,
    runs: int = 12,
    calls: int = 12,
    cost: float = 1.0,
    duration: float | None = 500.0,
    inconsistent: int = 0,
) -> WorkflowStepMetric:
    return WorkflowStepMetric(
        workflow="pipeline",
        workflow_version="v1",
        step=step,
        runs=runs,
        calls=calls,
        call_errors=0,
        cost_usd=cost,
        input_tokens=1000,
        output_tokens=100,
        avg_call_latency_ms=100.0,
        avg_run_duration_ms=duration,
        completed=runs,
        failed=0,
        cancelled=0,
        skipped=0,
        active=0,
        inconsistent=inconsistent,
        reported_outcomes=runs,
        successful_outcomes=runs,
        failed_outcomes=0,
        avg_score=0.9,
    )


def test_expensive_and_repeated_call_recommendations_show_assumptions():
    rows = [_metric("draft", calls=30, cost=3.0), _metric("review", cost=0.2)]
    recommendations = build_workflow_recommendations(rows, [])

    expensive = next(
        row for row in recommendations if row.kind == "expensive_step"
    )
    repeated = next(
        row for row in recommendations if row.kind == "repeated_calls"
    )
    assert expensive.confidence == "medium"
    assert "25%" in expensive.expected_benefit
    assert "scenario, not a forecast" in expensive.hazards[0]
    assert "tool loops" in repeated.hazards[1]


def test_declared_siblings_are_only_parallelization_investigation():
    definition = WorkflowDefinition(
        "pipeline",
        "v1",
        (
            WorkflowStepDefinition("plan"),
            WorkflowStepDefinition("draft", ("plan",)),
            WorkflowStepDefinition("research", ("plan",)),
        ),
    )
    recommendations = build_workflow_recommendations(
        [_metric("draft", duration=700), _metric("research", duration=300)],
        [definition],
    )

    parallel = next(
        row for row in recommendations if row.kind == "parallel_candidate"
    )
    assert parallel.expected_benefit.endswith(
        "~300ms saved per run if these siblings are truly independent."
    )
    assert any(
        "do not prove independence" in hazard for hazard in parallel.hazards
    )
    assert any("must not schedule" in hazard for hazard in parallel.hazards)


def test_sparse_or_inconsistent_evidence_never_gets_high_confidence():
    sparse = build_workflow_recommendations([_metric("draft", runs=2)], [])
    inconsistent = build_workflow_recommendations(
        [_metric("draft", inconsistent=1)], []
    )
    assert sparse == []
    assert {row.confidence for row in inconsistent} == {"low"}


def test_single_successor_chain_can_only_be_low_confidence_fusion_prompt():
    definition = WorkflowDefinition(
        "pipeline",
        "v1",
        (
            WorkflowStepDefinition("draft"),
            WorkflowStepDefinition("review", ("draft",)),
        ),
    )
    recommendations = build_workflow_recommendations(
        [_metric("draft"), _metric("review", cost=0.5)], [definition]
    )
    fusion = next(
        row for row in recommendations if row.kind == "fusion_candidate"
    )
    assert fusion.confidence == "low"
    assert "ceiling" in fusion.expected_benefit
    assert any("external consumer" in hazard for hazard in fusion.hazards)
    assert any("side-effect contract" in hazard for hazard in fusion.hazards)


def test_renderer_marks_output_read_only_and_lists_hazards():
    recommendation = build_workflow_recommendations(
        [_metric("draft", calls=24)], []
    )
    text = render_workflow_recommendations(recommendation)
    assert "read-only; never executable" in text
    assert "hazards:" in text


def test_empty_renderer_explains_minimum_evidence():
    assert "at least 3 explicit step runs" in render_workflow_recommendations(
        []
    )


def _graph(task: str, *, failed_review: bool = False, retry: bool = False):
    steps = ["plan", "draft", "review"]
    if retry:
        steps.extend(["review", "review"])
    nodes = tuple(
        WorkflowGraphNode(
            f"{task}:{index}",
            step,
            index + 1,
            "failed" if failed_review and step == "review" else "completed",
            float(index),
            float(index + 1),
            1,
            0.1,
            100.0,
            ("openai",),
            ("model",),
        )
        for index, step in enumerate(steps)
    )
    return WorkflowGraph(task, "pipeline", "v1", nodes, ())


def test_path_recommendations_expose_denominators_and_hazards():
    graphs = [
        _graph(f"task-{index}", failed_review=True, retry=True)
        for index in range(10)
    ]
    recommendations = build_workflow_recommendations([], [], graphs)
    kinds = {row.kind for row in recommendations}
    assert {
        "failure_branch",
        "retry_amplification",
        "expensive_common_path",
        "sequential_critical_path",
    } <= kinds
    failure = next(
        row for row in recommendations if row.kind == "failure_branch"
    )
    assert "30/30" in failure.evidence
    retry = next(
        row for row in recommendations if row.kind == "retry_amplification"
    )
    assert "3.00 runs/task" in retry.evidence
    common = next(
        row for row in recommendations if row.kind == "expensive_common_path"
    )
    assert "10/10 tasks (100.0%)" in common.evidence
    assert any("does not prove" in hazard for hazard in common.hazards)


def test_path_recommendations_keep_versions_and_sparse_paths_separate():
    sparse = [_graph("one"), _graph("two")]
    assert build_workflow_recommendations([], [], sparse) == []
    mixed = _graph("other")
    mixed = WorkflowGraph(
        mixed.task_id, mixed.workflow, "v2", mixed.nodes, mixed.edges
    )
    recommendations = build_workflow_recommendations(
        [], [], [_graph("a"), _graph("b"), _graph("c"), mixed]
    )
    assert {row.workflow_version for row in recommendations} == {"v1"}


def test_cli_reads_persisted_explicit_metrics(tmp_path, monkeypatch, capsys):
    path = str(tmp_path / "recommend.db")
    store = SqliteTraceStore(path)
    for index in range(3):
        identity = WorkflowIdentity(
            f"task-{index}", "pipeline", "v1", "draft", f"run-{index}"
        )
        store._insert(
            Trace(
                method="POST",
                path="/v1/messages",
                query="",
                request_headers=identity.headers(),
                request_body=b"{}",
                status_code=200,
                response_headers={},
                response_body=b"{}",
                latency_ms=100,
                cost_usd=1.0,
                task_id=identity.task_id,
                workflow=identity.workflow,
                workflow_version=identity.workflow_version,
                step=identity.step,
                step_run_id=identity.step_run_id,
            )
        )
        store._insert_workflow_event(
            WorkflowEvent(identity, "completed", success=True)
        )
    store.close()
    monkeypatch.setenv("CTRLRTN_DB", path)

    cli.main(["workflow", "recommendations", "--workflow", "pipeline"])
    output = capsys.readouterr().out
    assert "expensive_step" in output
    assert "never executable" in output

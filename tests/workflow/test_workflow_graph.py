"""Workflow graphs preserve provenance in console and Mermaid projections."""

from ctrlrtn.cli import commands as cli
from ctrlrtn.recorder.store import SqliteTraceStore
from ctrlrtn.workflow.graph import (
    WorkflowGraph,
    WorkflowGraphEdge,
    WorkflowGraphNode,
    build_workflow_flow,
    build_workflow_graph,
    render_workflow_flow,
    render_workflow_mermaid,
    render_workflow_sankey,
    render_workflow_structure,
    render_workflow_timeline,
)
from ctrlrtn.workflow.identity import WorkflowEvent, WorkflowIdentity
from ctrlrtn.workflow.inference import InferredWorkflowEdge


def _identity(step: str, *, parent: str | None = None) -> WorkflowIdentity:
    return WorkflowIdentity(
        task_id="task-1",
        workflow="article-pipeline",
        workflow_version="v1",
        step=step,
        step_run_id=f"run-{step}",
        parent_step_run_id=parent,
    )


def test_graph_renders_explicit_and_inferred_edges_distinctly():
    events = [
        WorkflowEvent(_identity("research"), "completed", event_id="e1", ts=1),
        WorkflowEvent(
            _identity("draft", parent="run-research"),
            "started",
            event_id="e2",
            ts=2,
        ),
    ]
    inferred = [
        InferredWorkflowEdge(
            "edge-1",
            "task-1",
            "article-pipeline",
            "v1",
            "run-research",
            "run-draft",
            1,
            2,
            "hash",
            1.0,
            "confirmed",
        )
    ]
    graph = build_workflow_graph("task-1", [], events, inferred)

    assert graph is not None
    assert {(edge.kind, edge.provenance) for edge in graph.edges} == {
        ("parent", "explicit"),
        ("tool-result", "inferred"),
    }
    timeline = render_workflow_timeline(graph)
    assert "run-research -> (parent)" in timeline
    assert "run-research ~> (inferred:confirmed)" in timeline
    assert "analysis-only inference" in timeline
    mermaid = render_workflow_mermaid(graph)
    assert "n0 -->|parent| n1" in mermaid
    assert "n0 -.->|inferred:confirmed| n1" in mermaid
    assert "Generated projection only; never import" in mermaid


def test_graph_marks_reused_step_run_identity_inconsistent():
    events = [
        WorkflowEvent(_identity("research"), "started", event_id="e1", ts=1),
        WorkflowEvent(_identity("draft"), "completed", event_id="e2", ts=2),
    ]
    graph = build_workflow_graph("task-1", [], events, [])

    assert graph is not None
    # Both helpers intentionally produce their step-specific run ID, so force
    # an identity collision using the immutable carrier fields explicitly.
    collision = WorkflowIdentity(
        "task-1", "article-pipeline", "v1", "draft", "run-research"
    )
    graph = build_workflow_graph(
        "task-1", [], [events[0], WorkflowEvent(collision, "completed")], []
    )
    assert graph.nodes[0].status == "inconsistent"


def test_workflow_runs_page_by_offset_newest_first(tmp_path):
    store = SqliteTraceStore(str(tmp_path / "runs.db"))
    try:
        for index in range(4):
            identity = WorkflowIdentity(
                task_id=f"task-{index}",
                workflow="article-pipeline",
                workflow_version="v1",
                step="write",
                step_run_id=f"run-{index}",
            )
            store._insert_workflow_event(
                WorkflowEvent(
                    identity, "completed", event_id=f"e{index}", ts=index
                )
            )
        newest_first = [row["task_id"] for row in store.workflow_runs()]
        assert newest_first == ["task-3", "task-2", "task-1", "task-0"]
        assert [row["task_id"] for row in store.workflow_runs(limit=2)] == [
            "task-3",
            "task-2",
        ]
        assert [
            row["task_id"] for row in store.workflow_runs(limit=2, offset=2)
        ] == ["task-1", "task-0"]
        assert store.workflow_runs(limit=2, offset=99) == []
    finally:
        store.close()


def test_store_lists_event_only_runs_and_cli_exports_mermaid(
    tmp_path, monkeypatch, capsys
):
    path = str(tmp_path / "workflow.db")
    store = SqliteTraceStore(path)
    store._insert_workflow_event(
        WorkflowEvent(_identity("research"), "completed", event_id="e1", ts=1)
    )
    try:
        assert store.workflow_runs()[0]["step_runs"] == 1
        assert store.workflow_graph("task-1") is not None
    finally:
        store.close()

    monkeypatch.setattr(cli, "_db_path", lambda: path)
    cli.main(["workflow", "diagram", "task-1"])
    assert "flowchart TD" in capsys.readouterr().out

    output = tmp_path / "workflow.mmd"
    cli.main(["workflow", "diagram", "task-1", "--output", str(output)])
    assert "never import as workflow authority" in output.read_text()

    sankey = tmp_path / "workflow-flow.mmd"
    cli.main(
        [
            "workflow",
            "flow",
            "article-pipeline",
            "v1",
            "--output",
            str(sankey),
        ]
    )
    captured = capsys.readouterr().out
    assert "aggregate workflow: article-pipeline@v1" in captured
    assert sankey.read_text().startswith("sankey-beta")


def _flow_graph(
    task: str, *, branch: bool, inferred: bool = False
) -> WorkflowGraph:
    def node(run: str, step: str, status: str = "completed"):
        return WorkflowGraphNode(
            run, step, 1, status, 1, 2, 1, 0.1, 10, (), ("m",)
        )

    nodes = [node(f"{task}-start", "start"), node(f"{task}-finish", "finish")]
    edges = [
        WorkflowGraphEdge(
            nodes[0].step_run_id, nodes[1].step_run_id, "dependency", "explicit"
        )
    ]
    if branch:
        nodes.append(node(f"{task}-review", "review", "failed"))
        edges.append(
            WorkflowGraphEdge(
                nodes[0].step_run_id,
                nodes[2].step_run_id,
                "parent",
                "inferred" if inferred else "explicit",
                "confirmed" if inferred else None,
            )
        )
    return WorkflowGraph(task, "newsroom", "v1", tuple(nodes), tuple(edges))


def test_aggregate_flow_uses_task_denominators_and_separates_inference():
    flow = build_workflow_flow(
        [
            _flow_graph("task-1", branch=True),
            _flow_graph("task-2", branch=False),
            _flow_graph("task-3", branch=True, inferred=True),
        ]
    )
    assert flow.tasks == 3
    finish = next(edge for edge in flow.edges if edge.target == "finish")
    assert (finish.tasks, finish.source_tasks, finish.branch_rate) == (
        3,
        3,
        1.0,
    )
    explicit_review = next(
        edge
        for edge in flow.edges
        if edge.target == "review" and edge.provenance == "explicit"
    )
    inferred_review = next(
        edge
        for edge in flow.edges
        if edge.target == "review" and edge.provenance == "inferred"
    )
    assert explicit_review.tasks == inferred_review.tasks == 1
    text = render_workflow_flow(flow)
    assert "1/3 source tasks (33.3%)" in text
    sankey = render_workflow_sankey(flow)
    assert "start,finish,3" in sankey and "start,review,1" in sankey
    assert "inferred edges are intentionally omitted" in sankey


def test_aggregate_flow_refuses_version_mixing():
    other = WorkflowGraph("task-x", "newsroom", "v2", (), ())
    import pytest

    with pytest.raises(ValueError, match="one exact workflow version"):
        build_workflow_flow([_flow_graph("task-1", branch=False), other])


def test_structure_summary_marks_branches_joins_retries_and_ignores_inference():
    nodes = (
        WorkflowGraphNode("a", "plan", 1, "completed", 1, 2, 1, 0, 10, (), ()),
        WorkflowGraphNode("b", "draft", 1, "completed", 2, 3, 1, 0, 10, (), ()),
        WorkflowGraphNode(
            "c", "review", 1, "completed", 2, 3, 1, 0, 10, (), ()
        ),
        WorkflowGraphNode(
            "d", "review", 2, "completed", 4, 5, 1, 0, 10, (), ()
        ),
        WorkflowGraphNode(
            "e", "publish", 1, "completed", 6, 7, 1, 0, 10, (), ()
        ),
    )
    edges = (
        WorkflowGraphEdge("a", "b", "dependency", "explicit"),
        WorkflowGraphEdge("a", "c", "dependency", "explicit"),
        WorkflowGraphEdge("b", "e", "dependency", "explicit"),
        WorkflowGraphEdge("d", "e", "dependency", "explicit"),
        WorkflowGraphEdge("e", "a", "tool-result", "inferred", "confirmed"),
    )
    text = render_workflow_structure(
        WorkflowGraph("task", "flow", "v1", nodes, edges)
    )
    assert "branches:  plan" in text
    assert "joins:     publish" in text
    assert "retries:   review×2" in text
    assert "back-edges:  none" in text
    assert "Inferred edges never create structural claims" in text

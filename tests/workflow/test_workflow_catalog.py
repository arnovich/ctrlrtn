from types import SimpleNamespace

import pytest

from ctrlrtn.workflow.catalog import (
    build_workflow_catalog,
    render_workflow_catalog,
    render_workflow_comparison,
)
from ctrlrtn.workflow.graph import WorkflowGraph, WorkflowGraphNode


def _graph(task, version, *, model, provider, latency):
    node = WorkflowGraphNode(
        f"run-{task}",
        "draft",
        1,
        "completed",
        1,
        2,
        2,
        0.3,
        latency,
        (provider,),
        (model,),
    )
    return WorkflowGraph(task, "newsroom", version, (node,), ())


class Store:
    def workflow_runs(self, limit):
        return [
            {
                "task_id": "a",
                "workflow": "newsroom",
                "workflow_version": "v1",
                "calls": 2,
                "cost_usd": 0.3,
            },
            {
                "task_id": "b",
                "workflow": "newsroom",
                "workflow_version": "v1",
                "calls": 1,
                "cost_usd": 0.2,
            },
            {
                "task_id": "c",
                "workflow": "newsroom",
                "workflow_version": "v2",
                "calls": 2,
                "cost_usd": 0.1,
            },
        ]

    def tasks(self, limit):
        return [
            SimpleNamespace(task_id="a", success=True),
            SimpleNamespace(task_id="b", success=None),
            SimpleNamespace(task_id="c", success=False),
        ]

    def workflow_routes(self):
        return [SimpleNamespace(workflow="newsroom", workflow_version="v2")]

    def experiments(self, limit):
        return [
            SimpleNamespace(
                status="running", workflow="newsroom", workflow_version="v2"
            )
        ]

    def shadow_experiments(self, limit):
        return [
            SimpleNamespace(
                status="stopped", workflow="newsroom", workflow_version="v1"
            )
        ]

    def workflow_graphs(self, workflow, version, limit):
        if version == "v1":
            return [
                _graph(
                    "a", "v1", model="opus", provider="anthropic", latency=20
                ),
                _graph(
                    "b", "v1", model="opus", provider="anthropic", latency=10
                ),
            ]
        return [
            _graph("c", "v2", model="haiku", provider="anthropic", latency=8)
        ]


def test_catalog_separates_versions_outcomes_controls_and_models():
    rows = build_workflow_catalog(Store())
    first, second = rows
    assert (first.version, first.tasks, first.successful, first.unreported) == (
        "v1",
        2,
        1,
        1,
    )
    assert first.avg_latency_ms == 7.5
    assert (
        second.version,
        second.failed,
        second.active_routes,
        second.active_experiments,
    ) == ("v2", 1, 1, 1)
    text = render_workflow_catalog(rows)
    assert "newsroom@v1" in text and "opus / anthropic" in text
    comparison = render_workflow_comparison(first, second)
    assert "v1 -> v2" in comparison
    assert "descriptive, not causal A/B evidence" in comparison


def test_comparison_refuses_different_workflows():
    rows = build_workflow_catalog(Store())
    other = SimpleNamespace(**{**rows[1].__dict__, "workflow": "other"})
    with pytest.raises(ValueError, match="one workflow"):
        render_workflow_comparison(rows[0], other)

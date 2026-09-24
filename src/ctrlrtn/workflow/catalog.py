"""Read-only workflow-version catalog and comparison projections."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class WorkflowCatalogRow:
    """One exact workflow version summarized across its recorded task runs.

    Outcome counts are task-level ``success`` reports, so ``unreported``
    means tasks with no outcome at all, not failures. ``active_routes`` and
    ``active_experiments`` say how much live control currently targets this
    version. Rows are read-only projections, never routing input.
    """

    workflow: str
    version: str
    tasks: int
    calls: int
    cost_usd: float
    avg_latency_ms: float
    successful: int
    failed: int
    unreported: int
    models: tuple[str, ...]
    providers: tuple[str, ...]
    active_routes: int
    active_experiments: int


def build_workflow_catalog(
    store, workflow: str | None = None
) -> list[WorkflowCatalogRow]:
    runs = store.workflow_runs(limit=100000)
    task_outcomes = {
        task.task_id: task.success for task in store.tasks(limit=100000)
    }
    routes = store.workflow_routes()
    experiments = store.experiments(limit=100000)
    shadows = store.shadow_experiments(limit=100000)
    grouped: dict[tuple[str, str], list[dict]] = {}
    for run in runs:
        if workflow is None or run["workflow"] == workflow:
            grouped.setdefault(
                (run["workflow"], run["workflow_version"]), []
            ).append(run)
    result = []
    for (name, version), version_runs in sorted(grouped.items()):
        graphs = store.workflow_graphs(name, version, 100000)
        nodes = [node for graph in graphs for node in graph.nodes]
        outcomes = [task_outcomes.get(run["task_id"]) for run in version_runs]
        active_experiments = sum(
            item.status == "running"
            and item.workflow == name
            and item.workflow_version == version
            for item in [*experiments, *shadows]
        )
        result.append(
            WorkflowCatalogRow(
                name,
                version,
                len(version_runs),
                sum(run["calls"] for run in version_runs),
                sum(run["cost_usd"] for run in version_runs),
                (
                    sum(node.latency_ms for node in nodes)
                    / sum(node.calls for node in nodes)
                    if sum(node.calls for node in nodes)
                    else 0.0
                ),
                sum(value is True for value in outcomes),
                sum(value is False for value in outcomes),
                sum(value is None for value in outcomes),
                tuple(
                    sorted({model for node in nodes for model in node.models})
                ),
                tuple(
                    sorted(
                        {
                            provider
                            for node in nodes
                            for provider in node.providers
                        }
                    )
                ),
                sum(
                    route.workflow == name and route.workflow_version == version
                    for route in routes
                ),
                active_experiments,
            )
        )
    return result


def render_workflow_catalog(rows: list[WorkflowCatalogRow]) -> str:
    if not rows:
        return "No workflow versions recorded."
    lines = [
        "workflow@version             tasks calls outcomes(+/-/?) cost       avg-ms routes exps models/providers"
    ]
    for row in rows:
        identity = f"{row.workflow}@{row.version}"[:28]
        labels = (
            f"{','.join(row.models) or '-'} / {','.join(row.providers) or '-'}"
        )
        lines.append(
            f"{identity:<28} {row.tasks:>5} {row.calls:>5} "
            f"{row.successful}/{row.failed}/{row.unreported:<7} ${row.cost_usd:<9.4f} "
            f"{row.avg_latency_ms:>6.0f} {row.active_routes:>6} {row.active_experiments:>4} {labels}"
        )
    return "\n".join(lines)


def render_workflow_comparison(
    left: WorkflowCatalogRow, right: WorkflowCatalogRow
) -> str:
    if left.workflow != right.workflow:
        raise ValueError("workflow comparison requires one workflow")

    def delta(value: float) -> str:
        return f"{value:+.4f}"

    return "\n".join(
        [
            f"workflow comparison: {left.workflow}",
            f"  versions       {left.version} -> {right.version}",
            f"  tasks          {left.tasks} -> {right.tasks} ({right.tasks-left.tasks:+d})",
            f"  calls          {left.calls} -> {right.calls} ({right.calls-left.calls:+d})",
            f"  cost           ${left.cost_usd:.4f} -> ${right.cost_usd:.4f} ({delta(right.cost_usd-left.cost_usd)})",
            f"  avg latency    {left.avg_latency_ms:.0f}ms -> {right.avg_latency_ms:.0f}ms ({right.avg_latency_ms-left.avg_latency_ms:+.0f}ms)",
            f"  outcomes       {left.successful}/{left.failed}/{left.unreported} -> {right.successful}/{right.failed}/{right.unreported}",
            "Task volumes and outcome coverage may differ; deltas are descriptive, not causal A/B evidence.",
        ]
    )

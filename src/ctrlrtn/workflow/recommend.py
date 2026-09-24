"""Read-only, evidence-labelled workflow optimization opportunities."""

from __future__ import annotations

from dataclasses import dataclass

from ctrlrtn.control_config import WorkflowDefinition
from ctrlrtn.workflow.graph import WorkflowGraph
from ctrlrtn.workflow.metrics import WorkflowStepMetric

_MIN_RUNS = 3
_MEDIUM_RUNS = 10
_MIN_COST = 0.01


@dataclass(frozen=True)
class WorkflowRecommendation:
    """One read-only optimization prompt for operator review.

    ``kind`` names the pattern (expensive step, repeated calls, parallel or
    fusion candidate, failure branch, retry amplification, common path),
    ``confidence`` is ``low`` or ``medium`` from run counts, ``evidence``
    cites the measurements, and ``hazards`` list why the router must not
    act on it. Nothing here is executable policy.
    """

    kind: str
    workflow: str
    workflow_version: str
    steps: tuple[str, ...]
    summary: str
    expected_benefit: str
    confidence: str
    evidence: str
    hazards: tuple[str, ...]


def build_workflow_recommendations(
    metrics: list[WorkflowStepMetric],
    definitions: list[WorkflowDefinition],
    graphs: list[WorkflowGraph] | None = None,
) -> list[WorkflowRecommendation]:
    """Generate review prompts only; nothing returned is executable policy."""
    rows = {
        (row.workflow, row.workflow_version, row.step): row for row in metrics
    }
    recommendations: list[WorkflowRecommendation] = []
    by_workflow: dict[tuple[str, str], list[WorkflowStepMetric]] = {}
    for row in metrics:
        by_workflow.setdefault((row.workflow, row.workflow_version), []).append(
            row
        )

    for workflow_rows in by_workflow.values():
        total_cost = sum(row.cost_usd for row in workflow_rows)
        for row in workflow_rows:
            if row.runs < _MIN_RUNS:
                continue
            if total_cost >= _MIN_COST and row.cost_usd / total_cost >= 0.30:
                saving = row.cost_usd * 0.25
                recommendations.append(
                    WorkflowRecommendation(
                        "expensive_step",
                        row.workflow,
                        row.workflow_version,
                        (row.step,),
                        f"Evaluate a cheaper specialist for {row.step}.",
                        f"A measured 25% step-cost reduction would save about ${saving:.4f} over this sample.",
                        _confidence(row.runs, row.inconsistent),
                        f"{row.runs} runs, {row.calls} calls, ${row.cost_usd:.4f}; {row.cost_usd / total_cost:.0%} of version cost.",
                        (
                            "The 25% figure is a scenario, not a forecast.",
                            "Require a step-scoped replay, shadow, and guarded split before routing.",
                            "Step evidence does not prove whole-workflow equivalence.",
                        ),
                    )
                )
            calls_per_run = row.calls / row.runs
            if calls_per_run >= 2.0:
                avoidable = row.cost_usd / row.calls if row.calls else 0.0
                recommendations.append(
                    WorkflowRecommendation(
                        "repeated_calls",
                        row.workflow,
                        row.workflow_version,
                        (row.step,),
                        f"Inspect repeated model calls in {row.step}.",
                        f"Removing one call per run would avoid up to ~${avoidable * row.runs:.4f} and ~{row.avg_call_latency_ms:.0f}ms per run at the observed mean.",
                        _confidence(row.runs, row.inconsistent),
                        f"{row.calls} calls across {row.runs} runs ({calls_per_run:.2f} calls/run).",
                        (
                            "Retries may be required for correctness or recovery.",
                            "Do not collapse critique/tool loops without outcome-equivalence tests.",
                            "Tool calls may be stateful and non-idempotent.",
                        ),
                    )
                )

    for definition in definitions:
        recommendations.extend(_parallel_candidates(definition, rows))
        recommendations.extend(_fusion_candidates(definition, rows))
    recommendations.extend(_path_recommendations(graphs or []))

    return sorted(
        recommendations,
        key=lambda row: (
            row.workflow,
            row.workflow_version,
            {
                "expensive_step": 0,
                "repeated_calls": 1,
                "parallel_candidate": 2,
                "fusion_candidate": 3,
                "failure_branch": 4,
                "retry_amplification": 5,
                "expensive_common_path": 6,
                "sequential_critical_path": 7,
            }.get(row.kind, 9),
            row.steps,
        ),
    )


def _path_recommendations(
    graphs: list[WorkflowGraph],
) -> list[WorkflowRecommendation]:
    """Recommend investigations from complete exact-version task graphs."""
    compatible = [
        graph
        for graph in graphs
        if graph.workflow != "(mixed)" and graph.workflow_version != "(mixed)"
    ]
    by_version: dict[tuple[str, str], list[WorkflowGraph]] = {}
    for graph in compatible:
        by_version.setdefault(
            (graph.workflow, graph.workflow_version), []
        ).append(graph)
    result: list[WorkflowRecommendation] = []
    for (workflow, version), workflow_graphs in by_version.items():
        if len(workflow_graphs) < _MIN_RUNS:
            continue
        step_tasks: dict[str, set[str]] = {}
        step_runs: dict[str, int] = {}
        step_failures: dict[str, int] = {}
        paths: dict[tuple[str, ...], list[WorkflowGraph]] = {}
        for graph in workflow_graphs:
            ordered = tuple(node.step for node in graph.nodes)
            paths.setdefault(ordered, []).append(graph)
            for node in graph.nodes:
                step_tasks.setdefault(node.step, set()).add(graph.task_id)
                step_runs[node.step] = step_runs.get(node.step, 0) + 1
                if node.status in {"failed", "cancelled", "inconsistent"}:
                    step_failures[node.step] = (
                        step_failures.get(node.step, 0) + 1
                    )
        for step, tasks in step_tasks.items():
            task_count = len(tasks)
            failures = step_failures.get(step, 0)
            if failures >= _MIN_RUNS and failures / step_runs[step] >= 0.20:
                result.append(
                    WorkflowRecommendation(
                        "failure_branch",
                        workflow,
                        version,
                        (step,),
                        f"Investigate the failure concentration at {step}.",
                        "Reducing this branch's failures would recover at most "
                        f"{failures} observed step runs.",
                        _confidence(task_count, 0),
                        f"{failures}/{step_runs[step]} runs failed, cancelled, or were inconsistent across {task_count} tasks.",
                        (
                            "Failure correlation does not establish root cause.",
                            "Separate expected cancellation and policy rejection before changing routing.",
                            "Preserve the original retry and compensation boundaries.",
                        ),
                    )
                )
            retries = step_runs[step] - task_count
            if retries >= _MIN_RUNS and step_runs[step] / task_count >= 1.5:
                result.append(
                    WorkflowRecommendation(
                        "retry_amplification",
                        workflow,
                        version,
                        (step,),
                        f"Inspect retry amplification at {step}.",
                        f"Eliminating unnecessary repeats would avoid at most {retries} observed step runs.",
                        _confidence(task_count, 0),
                        f"{step_runs[step]} runs across {task_count} tasks ({step_runs[step] / task_count:.2f} runs/task).",
                        (
                            "Attempts may be required for correctness and transient recovery.",
                            "Distinguish retries from loops and independently requested work.",
                            "Never collapse stateful work without idempotency evidence.",
                        ),
                    )
                )
        for path, path_graphs in paths.items():
            if not path or len(path_graphs) < _MIN_RUNS:
                continue
            share = len(path_graphs) / len(workflow_graphs)
            if share < 0.30:
                continue
            cost = sum(
                node.cost_usd for graph in path_graphs for node in graph.nodes
            )
            latency = sum(
                node.latency_ms for graph in path_graphs for node in graph.nodes
            )
            result.append(
                WorkflowRecommendation(
                    "expensive_common_path",
                    workflow,
                    version,
                    path,
                    "Review the cost and repeated work on this common path.",
                    f"This path accounts for ${cost:.4f} and {latency:.0f}ms of provider latency in the observed sample.",
                    _confidence(len(path_graphs), 0),
                    f"{len(path_graphs)}/{len(workflow_graphs)} tasks ({share:.1%}) followed the exact ordered path.",
                    (
                        "Order by observed start time does not prove every step is sequentially dependent.",
                        "Path frequency can change with inputs, versions, and experiment arms.",
                        "Optimize only with complete-trajectory outcome evidence.",
                    ),
                )
            )
            if len(path) >= 3 and latency > 0:
                result.append(
                    WorkflowRecommendation(
                        "sequential_critical_path",
                        workflow,
                        version,
                        path,
                        "Measure dependency-bound wall time along this frequent path.",
                        f"The provider-latency ceiling across these tasks is {latency / len(path_graphs):.0f}ms per task.",
                        "low",
                        f"{len(path_graphs)} exact task paths contain {len(path)} observed step runs in order.",
                        (
                            "Summed provider latency is not end-to-end critical-path duration.",
                            "Explicit dependency and effect contracts gate parallelization.",
                            "Joins, human waits, tools, and hidden application work may dominate.",
                        ),
                    )
                )
    return result


def _parallel_candidates(
    definition: WorkflowDefinition,
    rows: dict[tuple[str, str, str], WorkflowStepMetric],
) -> list[WorkflowRecommendation]:
    groups: dict[tuple[str, ...], list[str]] = {}
    for step in definition.steps:
        if step.predecessors:
            groups.setdefault(step.predecessors, []).append(step.name)
    result = []
    for predecessors, names in groups.items():
        if len(names) < 2:
            continue
        candidates = [
            rows.get((definition.workflow, definition.workflow_version, name))
            for name in names
        ]
        eligible = [
            row
            for row in candidates
            if row is not None and row.runs >= _MIN_RUNS
        ]
        if len(eligible) < 2 or any(
            row.avg_run_duration_ms is None for row in eligible
        ):
            continue
        benefit_ms = min(row.avg_run_duration_ms or 0.0 for row in eligible)
        runs = min(row.runs for row in eligible)
        result.append(
            WorkflowRecommendation(
                "parallel_candidate",
                definition.workflow,
                definition.workflow_version,
                tuple(row.step for row in eligible),
                f"Investigate parallel execution after {', '.join(predecessors)}.",
                f"The observed duration ceiling is ~{benefit_ms:.0f}ms saved per run if these siblings are truly independent.",
                "medium" if runs >= _MEDIUM_RUNS else "low",
                f"{len(eligible)} declared siblings share the same predecessors; at least {runs} observed runs each.",
                (
                    "Shared predecessors do not prove independence.",
                    "Check side effects, hidden shared state, credentials, rate limits, ordering, cancellation, and human approvals.",
                    "This is analysis only; the router must not schedule these steps.",
                ),
            )
        )
    return result


def _confidence(runs: int, inconsistent: int) -> str:
    if inconsistent:
        return "low"
    return "medium" if runs >= _MEDIUM_RUNS else "low"


def _fusion_candidates(
    definition: WorkflowDefinition,
    rows: dict[tuple[str, str, str], WorkflowStepMetric],
) -> list[WorkflowRecommendation]:
    successors: dict[str, list[str]] = {}
    steps = {step.name: step for step in definition.steps}
    for successor in definition.steps:
        for source in successor.predecessors:
            successors.setdefault(source, []).append(successor.name)
    result = []
    for source, targets in successors.items():
        if len(targets) != 1:
            continue
        target = targets[0]
        target_definition = steps[target]
        if (
            target_definition.predecessors != (source,)
            or target_definition.condition
        ):
            continue
        source_row = rows.get(
            (definition.workflow, definition.workflow_version, source)
        )
        target_row = rows.get(
            (definition.workflow, definition.workflow_version, target)
        )
        if (
            source_row is None
            or target_row is None
            or min(source_row.runs, target_row.runs) < _MIN_RUNS
            or not source_row.calls
            or not target_row.calls
        ):
            continue
        runs = min(source_row.runs, target_row.runs)
        cost_ceiling = target_row.cost_usd / target_row.runs * runs
        result.append(
            WorkflowRecommendation(
                "fusion_candidate",
                definition.workflow,
                definition.workflow_version,
                (source, target),
                f"Investigate whether {source} and {target} can be fused.",
                f"If fusion removed one {target} model call per run, the observed ceiling is ~${cost_ceiling:.4f} and ~{target_row.avg_call_latency_ms:.0f}ms per run.",
                "low",
                f"Declared single-successor chain with at least {runs} observed runs per step.",
                (
                    "Graph shape does not prove the intermediate output has no external consumer.",
                    "Fusion changes context, prompts, caching, evaluation units, retry behavior, and failure boundaries.",
                    "Require an explicit side-effect contract and offline end-to-end equivalence test before any execution experiment.",
                ),
            )
        )
    return result


def render_workflow_recommendations(rows: list[WorkflowRecommendation]) -> str:
    if not rows:
        return "No workflow optimization recommendations yet; collect at least 3 explicit step runs."
    lines = [
        "WORKFLOW OPTIMIZATION RECOMMENDATIONS (read-only; never executable)",
        "",
    ]
    for row in rows:
        lines.extend(
            [
                f"[{row.confidence}] {row.kind}: {row.workflow}@{row.workflow_version}/{' + '.join(row.steps)}",
                f"  {row.summary}",
                f"  expected: {row.expected_benefit}",
                f"  evidence: {row.evidence}",
                "  hazards:",
                *[f"    - {hazard}" for hazard in row.hazards],
                "",
            ]
        )
    return "\n".join(lines).rstrip()

"""Read-only projections of explicit and inferred workflow graphs."""

from __future__ import annotations

from dataclasses import dataclass

from ctrlrtn.workflow.identity import TERMINAL_STATUSES, WorkflowEvent
from ctrlrtn.workflow.inference import InferredWorkflowEdge


@dataclass(frozen=True)
class WorkflowGraphNode:
    """One step run in a task graph, merged from traces and lifecycle events.

    ``status`` is the run's single terminal lifecycle status, ``active`` if
    only ``started`` was seen, ``observed`` if only traces exist, or
    ``inconsistent`` when terminal statuses, identities or attempts
    conflict. Calls, cost and latency are provider-call totals; ``attempt``
    is 0 when it could not be determined.
    """

    step_run_id: str
    step: str
    attempt: int
    status: str
    first_ts: float
    last_ts: float
    calls: int
    cost_usd: float
    latency_ms: float
    providers: tuple[str, ...]
    models: tuple[str, ...]


@dataclass(frozen=True)
class WorkflowGraphEdge:
    """A directed link between two step runs, labelled by its provenance.

    ``provenance`` is ``"explicit"`` for application-declared ``parent``
    and ``dependency`` facts, or ``"inferred"`` for analysis-only
    ``tool-result`` links, which also carry a ``confirmation`` of
    ``confirmed``, ``contradicted`` or ``unverified``. Inferred edges never
    create structural claims.
    """

    source_step_run_id: str
    target_step_run_id: str
    kind: str
    provenance: str
    confirmation: str | None = None


@dataclass(frozen=True)
class WorkflowGraph:
    """Read-only per-task projection of step runs and their edges.

    ``workflow`` and ``workflow_version`` are ``"(mixed)"`` when the runs
    disagree on their definition. It is a display and export projection,
    never an orchestration definition or an import format.
    """

    task_id: str
    workflow: str
    workflow_version: str
    nodes: tuple[WorkflowGraphNode, ...]
    edges: tuple[WorkflowGraphEdge, ...]


@dataclass(frozen=True)
class WorkflowFlowNode:
    """One stable step aggregated over many task graphs of one version.

    ``tasks`` counts distinct tasks that ran the step, ``runs`` its step
    runs, and ``failed_runs`` runs that failed, were cancelled, or were
    inconsistent.
    """

    step: str
    tasks: int
    runs: int
    calls: int
    cost_usd: float
    latency_ms: float
    failed_runs: int


@dataclass(frozen=True)
class WorkflowFlowEdge:
    """One step-to-step link aggregated over task graphs of one version.

    ``tasks`` counts tasks containing the link and ``source_tasks`` tasks
    containing the source step, so ``branch_rate`` is the observed share of
    source tasks that continued along this edge. Explicit and inferred
    provenance are kept separate rather than summed.
    """

    source: str
    target: str
    kind: str
    provenance: str
    tasks: int
    source_tasks: int

    @property
    def branch_rate(self) -> float:
        return self.tasks / self.source_tasks if self.source_tasks else 0.0


@dataclass(frozen=True)
class WorkflowFlow:
    """Aggregate step/edge volumes for exactly one workflow version.

    Graphs of differing or ``"(mixed)"`` versions are never combined. The
    flow is an analysis-only projection; inferred edges are excluded from
    explicit flow volume.
    """

    workflow: str
    workflow_version: str
    tasks: int
    nodes: tuple[WorkflowFlowNode, ...]
    edges: tuple[WorkflowFlowEdge, ...]


def build_workflow_graph(
    task_id: str,
    traces: list[dict],
    events: list[WorkflowEvent],
    inferred_edges: list[InferredWorkflowEdge],
) -> WorkflowGraph | None:
    runs: dict[str, dict] = {}

    def run(run_id: str) -> dict:
        return runs.setdefault(
            run_id,
            {
                "identities": set(),
                "attempts": set(),
                "times": [],
                "events": [],
                "calls": 0,
                "cost": 0.0,
                "latency": 0.0,
                "providers": set(),
                "models": set(),
            },
        )

    for trace in traces:
        run_id = trace.get("step_run_id")
        if not run_id:
            continue
        row = run(run_id)
        row["identities"].add(
            (trace["workflow"], trace["workflow_version"], trace["step"])
        )
        if trace.get("step_attempt") is not None:
            row["attempts"].add(int(trace["step_attempt"]))
        row["times"].append(float(trace["ts"]))
        row["calls"] += 1
        row["cost"] += trace.get("cost_usd") or 0.0
        row["latency"] += trace.get("latency_ms") or 0.0
        if trace.get("provider"):
            row["providers"].add(trace["provider"])
        model = trace.get("served_model") or trace.get("model")
        if model:
            row["models"].add(model)

    explicit_edges: set[tuple[str, str, str]] = set()
    for event in events:
        identity = event.identity
        row = run(identity.step_run_id)
        row["identities"].add(
            (identity.workflow, identity.workflow_version, identity.step)
        )
        row["attempts"].add(identity.attempt)
        row["times"].append(event.ts)
        row["events"].append(event)
        if identity.parent_step_run_id:
            explicit_edges.add(
                (identity.parent_step_run_id, identity.step_run_id, "parent")
            )
        explicit_edges.update(
            (dependency, identity.step_run_id, "dependency")
            for dependency in identity.dependency_step_run_ids
        )
    if not runs:
        return None

    all_identities = {
        identity for row in runs.values() for identity in row["identities"]
    }
    workflows = {(identity[0], identity[1]) for identity in all_identities}
    workflow, version = (
        next(iter(workflows)) if len(workflows) == 1 else ("(mixed)", "(mixed)")
    )
    nodes = []
    for run_id, row in runs.items():
        identities = row["identities"]
        step = (
            next(iter(identities))[2]
            if len(identities) == 1
            else "(identity collision)"
        )
        attempts = row["attempts"]
        terminal = {
            event.status
            for event in row["events"]
            if event.status in TERMINAL_STATUSES
        }
        status = (
            "inconsistent"
            if len(terminal) > 1 or len(identities) > 1 or len(attempts) > 1
            else (
                next(iter(terminal))
                if terminal
                else (
                    "active"
                    if any(event.status == "started" for event in row["events"])
                    else "observed"
                )
            )
        )
        times = row["times"] or [0.0]
        nodes.append(
            WorkflowGraphNode(
                step_run_id=run_id,
                step=step,
                attempt=next(iter(attempts)) if len(attempts) == 1 else 0,
                status=status,
                first_ts=min(times),
                last_ts=max(times),
                calls=row["calls"],
                cost_usd=row["cost"],
                latency_ms=row["latency"],
                providers=tuple(sorted(row["providers"])),
                models=tuple(sorted(row["models"])),
            )
        )
    known = set(runs)
    edges = [
        WorkflowGraphEdge(source, target, kind, "explicit")
        for source, target, kind in sorted(explicit_edges)
        if source in known and target in known
    ]
    edges.extend(
        WorkflowGraphEdge(
            edge.source_step_run_id,
            edge.target_step_run_id,
            "tool-result",
            "inferred",
            edge.confirmation,
        )
        for edge in inferred_edges
        if edge.task_id == task_id
        and edge.source_step_run_id in known
        and edge.target_step_run_id in known
    )
    return WorkflowGraph(
        task_id=task_id,
        workflow=workflow,
        workflow_version=version,
        nodes=tuple(
            sorted(nodes, key=lambda node: (node.first_ts, node.step_run_id))
        ),
        edges=tuple(edges),
    )


def render_workflow_timeline(graph: WorkflowGraph) -> str:
    incoming: dict[str, list[WorkflowGraphEdge]] = {}
    for edge in graph.edges:
        incoming.setdefault(edge.target_step_run_id, []).append(edge)
    lines = [
        f"workflow task: {graph.task_id}",
        f"definition: {graph.workflow}@{graph.workflow_version}",
        "",
        "timeline:",
    ]
    for node in graph.nodes:
        edges = incoming.get(node.step_run_id, [])
        edge_text = (
            ", ".join(
                (
                    f"{edge.source_step_run_id} -> ({edge.kind})"
                    if edge.provenance == "explicit"
                    else f"{edge.source_step_run_id} ~> (inferred:{edge.confirmation})"
                )
                for edge in edges
            )
            or "root/no recorded dependency"
        )
        models = ",".join(node.models) or "-"
        lines.extend(
            [
                f"  [{node.status}] {node.step}  run={node.step_run_id} "
                f"attempt={node.attempt or '?'}",
                f"    from: {edge_text}",
                f"    calls={node.calls} cost=${node.cost_usd:.4f} "
                f"provider-latency={node.latency_ms:.0f}ms models={models}",
            ]
        )
    lines.extend(
        [
            "",
            "Solid `->` links are explicit application facts; `~>` links are "
            "analysis-only inference.",
        ]
    )
    return "\n".join(lines)


def render_workflow_structure(graph: WorkflowGraph) -> str:
    """Compact structural cues for interactive inspection, never authority."""
    nodes = {node.step_run_id: node for node in graph.nodes}
    explicit = [edge for edge in graph.edges if edge.provenance == "explicit"]
    incoming: dict[str, set[str]] = {}
    outgoing: dict[str, set[str]] = {}
    loops = []
    for edge in explicit:
        incoming.setdefault(edge.target_step_run_id, set()).add(
            edge.source_step_run_id
        )
        outgoing.setdefault(edge.source_step_run_id, set()).add(
            edge.target_step_run_id
        )
        source = nodes.get(edge.source_step_run_id)
        target = nodes.get(edge.target_step_run_id)
        if source and target and target.first_ts <= source.first_ts:
            loops.append(f"{source.step}->{target.step}")
    branches = [
        nodes[run_id].step
        for run_id, targets in outgoing.items()
        if len(targets) > 1 and run_id in nodes
    ]
    joins = [
        nodes[run_id].step
        for run_id, sources in incoming.items()
        if len(sources) > 1 and run_id in nodes
    ]
    counts: dict[str, int] = {}
    for node in graph.nodes:
        counts[node.step] = counts.get(node.step, 0) + 1
    retries = [f"{step}×{count}" for step, count in counts.items() if count > 1]
    linked = {
        (edge.source_step_run_id, edge.target_step_run_id) for edge in explicit
    }
    overlaps = []
    for index, left in enumerate(graph.nodes):
        for right in graph.nodes[index + 1 :]:
            if (
                left.first_ts < right.last_ts
                and right.first_ts < left.last_ts
                and (left.step_run_id, right.step_run_id) not in linked
                and (right.step_run_id, left.step_run_id) not in linked
            ):
                overlaps.append(f"{left.step}|{right.step}")
    models = sorted({model for node in graph.nodes for model in node.models})
    providers = sorted(
        {provider for node in graph.nodes for provider in node.providers}
    )

    def values(items: list[str]) -> str:
        return ", ".join(sorted(items)) if items else "none"

    return "\n".join(
        [
            "structure (explicit edges; observed timing only):",
            f"  branches:  {values(branches)}",
            f"  joins:     {values(joins)}",
            f"  retries:   {values(retries)}",
            f"  back-edges:{' ' if loops else '  '}{values(loops)}",
            f"  overlaps:  {values(overlaps)} (timing observation, not independence)",
            f"  models:    {values(models)}",
            f"  providers: {values(providers)}",
            "  Inferred edges never create structural claims.",
        ]
    )


def _label(value: str) -> str:
    return value.replace("\\", "\\\\").replace('"', "&quot;").replace("\n", " ")


def render_workflow_mermaid(graph: WorkflowGraph) -> str:
    """Deterministic, non-importable Mermaid projection with escaped labels."""
    ids = {
        node.step_run_id: f"n{index}" for index, node in enumerate(graph.nodes)
    }
    lines = [
        "flowchart TD",
        f"  %% task={graph.task_id} workflow={graph.workflow}@{graph.workflow_version}",
    ]
    for node in graph.nodes:
        label = _label(
            f"{node.step}#{node.attempt or '?'}<br/>{node.status}<br/>"
            f"{node.calls} calls · ${node.cost_usd:.4f}"
        )
        lines.append(f'  {ids[node.step_run_id]}["{label}"]')
    for edge in graph.edges:
        source = ids[edge.source_step_run_id]
        target = ids[edge.target_step_run_id]
        if edge.provenance == "explicit":
            lines.append(f"  {source} -->|{edge.kind}| {target}")
        else:
            label = _label(f"inferred:{edge.confirmation}")
            lines.append(f"  {source} -.->|{label}| {target}")
    lines.append(
        "  %% Generated projection only; never import as workflow authority."
    )
    return "\n".join(lines)


def build_workflow_flow(graphs: list[WorkflowGraph]) -> WorkflowFlow | None:
    """Aggregate compatible task graphs without mixing versions or provenance."""
    if not graphs:
        return None
    identities = {(graph.workflow, graph.workflow_version) for graph in graphs}
    if len(identities) != 1 or "(mixed)" in next(iter(identities)):
        raise ValueError(
            "aggregate workflow flow requires one exact workflow version"
        )
    workflow, version = next(iter(identities))
    node_data: dict[str, dict] = {}
    edge_tasks: dict[tuple[str, str, str, str], set[str]] = {}
    source_tasks: dict[str, set[str]] = {}
    for graph in graphs:
        by_run = {node.step_run_id: node for node in graph.nodes}
        task_steps: set[str] = set()
        for node in graph.nodes:
            data = node_data.setdefault(
                node.step,
                {
                    "tasks": set(),
                    "runs": 0,
                    "calls": 0,
                    "cost": 0.0,
                    "latency": 0.0,
                    "failed": 0,
                },
            )
            data["tasks"].add(graph.task_id)
            data["runs"] += 1
            data["calls"] += node.calls
            data["cost"] += node.cost_usd
            data["latency"] += node.latency_ms
            data["failed"] += node.status in {
                "failed",
                "cancelled",
                "inconsistent",
            }
            task_steps.add(node.step)
        for step in task_steps:
            source_tasks.setdefault(step, set()).add(graph.task_id)
        for edge in graph.edges:
            source = by_run.get(edge.source_step_run_id)
            target = by_run.get(edge.target_step_run_id)
            if source is None or target is None:
                continue
            edge_tasks.setdefault(
                (source.step, target.step, edge.kind, edge.provenance), set()
            ).add(graph.task_id)
    nodes = tuple(
        WorkflowFlowNode(
            step,
            len(data["tasks"]),
            data["runs"],
            data["calls"],
            data["cost"],
            data["latency"],
            data["failed"],
        )
        for step, data in sorted(node_data.items())
    )
    edges = tuple(
        WorkflowFlowEdge(
            source,
            target,
            kind,
            provenance,
            len(tasks),
            len(source_tasks[source]),
        )
        for (source, target, kind, provenance), tasks in sorted(
            edge_tasks.items()
        )
    )
    return WorkflowFlow(
        workflow,
        version,
        len({graph.task_id for graph in graphs}),
        nodes,
        edges,
    )


def render_workflow_flow(flow: WorkflowFlow) -> str:
    lines = [
        f"aggregate workflow: {flow.workflow}@{flow.workflow_version}",
        f"tasks: {flow.tasks}",
        "",
        "steps:",
        "  step                     tasks runs calls failures cost       latency",
    ]
    for node in flow.nodes:
        lines.append(
            f"  {node.step[:24]:<24} {node.tasks:>5} {node.runs:>4} {node.calls:>5} "
            f"{node.failed_runs:>8} ${node.cost_usd:<9.4f} {node.latency_ms:>7.0f}ms"
        )
    for provenance in ("explicit", "inferred"):
        lines.extend(["", f"{provenance} edges:"])
        selected = [
            edge for edge in flow.edges if edge.provenance == provenance
        ]
        if not selected:
            lines.append("  none")
        for edge in selected:
            marker = "->" if provenance == "explicit" else "~>"
            lines.append(
                f"  {edge.source} {marker} {edge.target} ({edge.kind}): "
                f"{edge.tasks}/{edge.source_tasks} source tasks ({edge.branch_rate:.1%})"
            )
    lines.extend(
        [
            "",
            "Inferred edges are analysis-only and excluded from explicit flow volume.",
        ]
    )
    return "\n".join(lines)


def render_workflow_sankey(flow: WorkflowFlow) -> str:
    """Render explicit task counts as a deterministic Mermaid Sankey projection."""

    def label(value: str) -> str:
        return value.replace(",", " ").replace("\n", " ").replace('"', "'")

    lines = [
        "sankey-beta",
        f"%% workflow={flow.workflow}@{flow.workflow_version} tasks={flow.tasks}",
    ]
    explicit = [edge for edge in flow.edges if edge.provenance == "explicit"]
    lines.extend(
        f"{label(edge.source)},{label(edge.target)},{edge.tasks}"
        for edge in explicit
    )
    lines.append(
        "%% Explicit application facts only; inferred edges are intentionally omitted."
    )
    lines.append(
        "%% Generated projection only; never import as workflow authority."
    )
    return "\n".join(lines)

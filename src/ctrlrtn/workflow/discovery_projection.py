"""Privacy-safe visual projections for passively discovered workflow families."""

from __future__ import annotations

import json
from collections import Counter, defaultdict
from dataclasses import asdict, dataclass

from ctrlrtn.workflow.discovery import (
    WorkflowDiscoveryReport,
    build_observed_task_structures,
    correlate_unscoped_traces,
)


@dataclass(frozen=True)
class DiscoveredPath:
    labels: tuple[str, ...]
    tasks: int


@dataclass(frozen=True)
class DiscoveredTaskProjection:
    task_digest: str
    timeline: tuple[str, ...]
    calls: int
    input_tokens: int
    output_tokens: int
    cost_usd: float
    latency_ms: float
    http_failures: int
    outcome: str
    ambiguous: bool


@dataclass(frozen=True)
class DiscoveredFamilyProjection:
    family_id: str
    tasks: int
    calls: int
    input_tokens: int
    output_tokens: int
    cost_usd: float
    latency_ms: float
    http_failures: int
    unknown_outcomes: int
    providers: tuple[tuple[str, int], ...]
    models: tuple[tuple[str, int], ...]
    tool_operations: tuple[tuple[str, int], ...]
    paths: tuple[DiscoveredPath, ...]
    branches: tuple[str, ...]
    joins: tuple[str, ...]
    retries: tuple[tuple[str, int], ...]
    loops: tuple[tuple[str, str, int], ...]
    representative_timeline: tuple[str, ...]
    ambiguous_tasks: int
    task_timelines: tuple[DiscoveredTaskProjection, ...]


def build_discovered_family_projections(
    traces: list[dict], report: WorkflowDiscoveryReport
) -> tuple[DiscoveredFamilyProjection, ...]:
    """Aggregate frozen observations without retaining raw task identities."""
    correlated, _ = correlate_unscoped_traces(traces)
    structures, _, _ = build_observed_task_structures(correlated)
    rows = {int(row["id"]): row for row in correlated}
    structures_by_digest = {item.task_digest: item for item in structures}
    assignments = {
        item.task_digest: item
        for item in report.assignments
        if item.family_id is not None
    }
    projections = []
    for family in report.families:
        members = [
            structures_by_digest[digest]
            for digest in family.member_task_digests
            if digest in structures_by_digest
        ]
        paths: Counter[tuple[str, ...]] = Counter()
        providers: Counter[str] = Counter()
        models: Counter[str] = Counter()
        tools: Counter[str] = Counter()
        retries: Counter[str] = Counter()
        calls = input_tokens = output_tokens = failures = 0
        cost = latency = 0.0
        task_timelines = []
        for member in members:
            ordered_rows = [rows[trace_id] for trace_id in member.trace_ids]
            label_counts = Counter(dict(member.nodes))
            timeline = member.chronological_labels
            paths[timeline] += 1
            retries.update(
                {
                    label: count - 1
                    for label, count in label_counts.items()
                    if count > 1
                }
            )
            task_input = task_output = task_failures = 0
            task_cost = task_latency = 0.0
            for row in ordered_rows:
                calls += 1
                input_tokens += int(row.get("input_tokens") or 0)
                output_tokens += int(row.get("output_tokens") or 0)
                cost += float(row.get("cost_usd") or 0.0)
                latency += float(row.get("latency_ms") or 0.0)
                status = row.get("status_code")
                failures += isinstance(status, int) and status >= 400
                task_input += int(row.get("input_tokens") or 0)
                task_output += int(row.get("output_tokens") or 0)
                task_cost += float(row.get("cost_usd") or 0.0)
                task_latency += float(row.get("latency_ms") or 0.0)
                task_failures += isinstance(status, int) and status >= 400
                if row.get("provider"):
                    providers[str(row["provider"])] += 1
                model = row.get("served_model") or row.get("model")
                if model:
                    models[str(model)] += 1
            assignment = assignments.get(member.task_digest)
            task_timelines.append(
                DiscoveredTaskProjection(
                    member.task_digest,
                    timeline,
                    len(ordered_rows),
                    task_input,
                    task_output,
                    task_cost,
                    task_latency,
                    task_failures,
                    "unknown",
                    bool(assignment and assignment.ambiguous),
                )
            )
            for label, count in member.nodes:
                if "[" in label and label.endswith("]"):
                    for tool in label.rsplit("[", 1)[1][:-1].split(","):
                        if tool:
                            tools[tool] += count
        outgoing: Counter[str] = Counter()
        incoming: Counter[str] = Counter()
        loop_counts: Counter[tuple[str, str]] = Counter()
        for edge in family.edges:
            outgoing[edge.source] += 1
            incoming[edge.target] += 1
            if edge.source == edge.target:
                loop_counts[(edge.source, edge.target)] += edge.occurrences
        representative = structures_by_digest.get(
            family.representative_task_digest
        )
        representative_timeline = (
            representative.chronological_labels if representative else ()
        )
        projections.append(
            DiscoveredFamilyProjection(
                family.family_id,
                family.support,
                calls,
                input_tokens,
                output_tokens,
                cost,
                latency,
                failures,
                len(members),
                tuple(sorted(providers.items())),
                tuple(sorted(models.items())),
                tuple(sorted(tools.items())),
                tuple(
                    DiscoveredPath(labels, count)
                    for labels, count in sorted(
                        paths.items(), key=lambda item: (-item[1], item[0])
                    )
                ),
                tuple(
                    sorted(
                        label for label, count in outgoing.items() if count > 1
                    )
                ),
                tuple(
                    sorted(
                        label for label, count in incoming.items() if count > 1
                    )
                ),
                tuple(sorted(retries.items())),
                tuple(
                    (*edge, count)
                    for edge, count in sorted(loop_counts.items())
                ),
                representative_timeline,
                sum(
                    bool(
                        assignments.get(member.task_digest)
                        and assignments[member.task_digest].ambiguous
                    )
                    for member in members
                ),
                tuple(
                    sorted(task_timelines, key=lambda item: item.task_digest)
                ),
            )
        )
    return tuple(projections)


def projection_document(projection: DiscoveredFamilyProjection) -> dict:
    return asdict(projection)


def projection_from_document(value: dict) -> DiscoveredFamilyProjection:
    return DiscoveredFamilyProjection(
        family_id=value["family_id"],
        tasks=value["tasks"],
        calls=value["calls"],
        input_tokens=value["input_tokens"],
        output_tokens=value["output_tokens"],
        cost_usd=value["cost_usd"],
        latency_ms=value["latency_ms"],
        http_failures=value["http_failures"],
        unknown_outcomes=value["unknown_outcomes"],
        providers=tuple(tuple(item) for item in value["providers"]),
        models=tuple(tuple(item) for item in value["models"]),
        tool_operations=tuple(tuple(item) for item in value["tool_operations"]),
        paths=tuple(
            DiscoveredPath(tuple(item["labels"]), item["tasks"])
            for item in value["paths"]
        ),
        branches=tuple(value["branches"]),
        joins=tuple(value["joins"]),
        retries=tuple(tuple(item) for item in value["retries"]),
        loops=tuple(tuple(item) for item in value["loops"]),
        representative_timeline=tuple(value["representative_timeline"]),
        ambiguous_tasks=value["ambiguous_tasks"],
        task_timelines=tuple(
            DiscoveredTaskProjection(
                task_digest=item["task_digest"],
                timeline=tuple(item["timeline"]),
                calls=item["calls"],
                input_tokens=item["input_tokens"],
                output_tokens=item["output_tokens"],
                cost_usd=item["cost_usd"],
                latency_ms=item["latency_ms"],
                http_failures=item["http_failures"],
                outcome=item["outcome"],
                ambiguous=item["ambiguous"],
            )
            for item in value.get("task_timelines", [])
        ),
    )


def render_discovered_family_projection(
    projection: DiscoveredFamilyProjection,
) -> str:
    def counts(items: tuple[tuple[str, int], ...]) -> str:
        return ", ".join(f"{name} ({count})" for name, count in items) or "none"

    lines = [
        f"discovered family projection: {projection.family_id}",
        "analysis-only inferred identity; never workflow or routing authority",
        "",
        f"tasks={projection.tasks} calls={projection.calls} ambiguous={projection.ambiguous_tasks}",
        f"tokens={projection.input_tokens}/{projection.output_tokens} cost=${projection.cost_usd:.4f} latency={projection.latency_ms:.0f}ms",
        f"HTTP failures={projection.http_failures} unknown outcomes={projection.unknown_outcomes}",
        f"providers: {counts(projection.providers)}",
        f"models: {counts(projection.models)}",
        f"tool operations: {counts(projection.tool_operations)}",
        "",
        "representative timeline:",
        "  " + " -> ".join(projection.representative_timeline),
        "",
        "path frequencies:",
    ]
    lines.extend(
        f"  {path.tasks}/{projection.tasks}: " + " -> ".join(path.labels)
        for path in projection.paths
    )
    lines.extend(
        [
            "",
            "structure:",
            "  branches: " + (", ".join(projection.branches) or "none"),
            "  joins: " + (", ".join(projection.joins) or "none"),
            "  retries: " + counts(projection.retries),
            "  loops: "
            + (
                ", ".join(
                    f"{a}->{b} ({count})" for a, b, count in projection.loops
                )
                or "none"
            ),
        ]
    )
    lines.extend(["", "task timelines (digests only):"])
    lines.extend(
        f"  {task.task_digest}: "
        + " -> ".join(task.timeline)
        + f" · {task.calls} calls · ${task.cost_usd:.4f} · {task.latency_ms:.0f}ms"
        + (" · AMBIGUOUS" if task.ambiguous else "")
        for task in projection.task_timelines[:50]
    )
    if len(projection.task_timelines) > 50:
        lines.append(
            f"  ... {len(projection.task_timelines) - 50} more in artifact"
        )
    return "\n".join(lines)


def render_discovered_family_dag(
    projection: DiscoveredFamilyProjection,
) -> str:
    """Render an inferred family as a compact, terminal-native weighted DAG."""

    def short(label: str) -> str:
        if "[" in label and label.endswith("]"):
            tools = label.rsplit("[", 1)[1][:-1]
            return tools or label.removeprefix("tag:")
        return "finish" if label.startswith("tag:") else label

    visits: Counter[str] = Counter()
    edges: Counter[tuple[str, str]] = Counter()
    for path in projection.paths:
        visits.update(dict.fromkeys(path.labels, path.tasks))
        edges.update(
            dict.fromkeys(
                zip(path.labels, path.labels[1:], strict=False), path.tasks
            )
        )

    order: list[str] = []
    for label in projection.representative_timeline:
        if label not in order:
            order.append(label)
    for path in projection.paths:
        for label in path.labels:
            if label not in order:
                order.append(label)
    node_ids = {label: index + 1 for index, label in enumerate(order)}
    outgoing: dict[str, list[tuple[str, int]]] = defaultdict(list)
    for (source, target), count in edges.items():
        outgoing[source].append((target, count))

    lines = [
        f"INFERRED WORKFLOW DAG · {projection.family_id}",
        f"{projection.tasks} tasks · {projection.calls} calls · "
        f"${projection.cost_usd:.4f} · {projection.http_failures} HTTP failures",
        "Analysis-only inference; no execution or routing authority.",
        "",
    ]
    retry_labels = {label for label, _ in projection.retries}
    for label in order:
        flags = []
        if label in projection.branches:
            flags.append("branch")
        if label in projection.joins:
            flags.append("join")
        if label in retry_labels:
            flags.append("retry")
        suffix = f" · {', '.join(flags)}" if flags else ""
        title = f"{node_ids[label]:02d}  {short(label)}"
        seen = f"seen in {visits[label]}/{projection.tasks} tasks{suffix}"
        width = max(len(title), len(seen)) + 2
        lines.extend(
            [
                "┌" + "─" * width + "┐",
                f"│ {title:<{width - 1}}│",
                f"│ {seen:<{width - 1}}│",
                "└" + "─" * width + "┘",
            ]
        )
        destinations = sorted(
            outgoing.get(label, ()),
            key=lambda item: (-item[1], node_ids.get(item[0], 0)),
        )
        for index, (target, count) in enumerate(destinations):
            elbow = "└" if index == len(destinations) - 1 else "├"
            symbol = "↻" if target == label else "→"
            lines.append(
                f" {elbow}─ {count}/{projection.tasks} tasks {symbol} "
                f"{node_ids[target]:02d} {short(target)}"
            )
        lines.append("")

    lines.append("OBSERVED VARIANTS")
    for path in projection.paths:
        lane = " ─ ".join(f"{node_ids[label]:02d}" for label in path.labels)
        lines.append(f"  {path.tasks}×  {lane}")
    return "\n".join(lines)


def render_discovered_family_mermaid(
    projection: DiscoveredFamilyProjection, family_edges: tuple
) -> str:
    labels = sorted(
        {label for path in projection.paths for label in path.labels}
    )
    ids = {label: f"n{index}" for index, label in enumerate(labels)}
    lines = [
        "flowchart TD",
        f"  %% inferred-family={projection.family_id} tasks={projection.tasks}",
    ]
    for label in labels:
        safe = label.replace('"', "&quot;").replace("\n", " ")
        lines.append(f'  {ids[label]}["{safe}"]')
    for edge in family_edges:
        if edge.source in ids and edge.target in ids:
            lines.append(
                f"  {ids[edge.source]} -.->|{edge.tasks}/{projection.tasks} tasks| {ids[edge.target]}"
            )
    lines.append(
        "  %% Analysis-only inferred projection; never import as authority."
    )
    return "\n".join(lines)


def render_discovered_family_json(
    projection: DiscoveredFamilyProjection,
) -> str:
    document = {
        "version": 1,
        "kind": "discovered-workflow-family-projection",
        "authority": "analysis_only_no_routing_or_execution",
        "projection": projection_document(projection),
    }
    return json.dumps(document, indent=2, sort_keys=True) + "\n"

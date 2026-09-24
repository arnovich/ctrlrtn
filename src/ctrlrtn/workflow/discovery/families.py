"""Deterministic clustering and family report assembly."""

from __future__ import annotations

from collections import Counter

from ctrlrtn.workflow.discovery.correlation import (
    correlate_unscoped_traces,
)
from ctrlrtn.workflow.discovery.models import (
    DiscoveredFamilyEdge,
    DiscoveredFamilyNode,
    DiscoveredWorkflowFamily,
    DiscoveryProgress,
    WorkflowDiscoveryReport,
    WorkflowFamilyAssignment,
)
from ctrlrtn.workflow.discovery.structures import (
    _counter,
    build_observed_task_structures,
    structure_similarity,
)


def discover_workflow_families(
    traces: list[dict],
    *,
    min_support: int = 2,
    similarity: float = 0.75,
    progress: DiscoveryProgress | None = None,
) -> WorkflowDiscoveryReport:
    """Cluster recurring task structures; results never grant workflow authority."""
    if (
        isinstance(min_support, bool)
        or not isinstance(min_support, int)
        or min_support < 2
    ):
        raise ValueError("workflow family min_support must be at least 2")
    if (
        isinstance(similarity, bool)
        or not isinstance(similarity, (int, float))
        or not 0 < similarity <= 1
    ):
        raise ValueError("workflow family similarity must be in (0, 1]")
    correlated, correlation = correlate_unscoped_traces(traces)
    structures, total_tasks, ambiguous = build_observed_task_structures(
        correlated
    )
    eligible = tuple(item for item in structures if len(item.trace_ids) >= 2)
    comparisons = len(eligible) * (len(eligible) - 1) // 2
    total_work = max(1, comparisons + len(eligible))
    done = 0
    if progress is not None:
        progress(done, total_work, "correlated inputs")
    adjacency = {index: set() for index in range(len(eligible))}
    for left in range(len(eligible)):
        for right in range(left + 1, len(eligible)):
            if (
                structure_similarity(eligible[left], eligible[right])
                >= similarity
            ):
                adjacency[left].add(right)
                adjacency[right].add(left)
            done += 1
            if progress is not None and (
                done == comparisons or done % 128 == 0
            ):
                progress(done, total_work, "comparing task structures")
    components = []
    unseen = set(adjacency)
    while unseen:
        pending = [min(unseen)]
        component = set()
        while pending:
            index = pending.pop()
            if index in component:
                continue
            component.add(index)
            pending.extend(sorted(adjacency[index] - component, reverse=True))
        unseen -= component
        components.append(tuple(eligible[index] for index in sorted(component)))

    families = []
    assignments = []
    clustered = 0
    for members in components:
        done += len(members)
        if progress is not None:
            progress(
                min(done, total_work), total_work, "building workflow families"
            )
        if len(members) < min_support:
            assignments.extend(
                WorkflowFamilyAssignment(
                    member.task_digest,
                    None,
                    "unclustered",
                    None,
                    member.fingerprint,
                    len(member.trace_ids),
                    member.identity_source,
                    member.correlation_ambiguous,
                    "component below minimum support",
                )
                for member in members
            )
            continue
        clustered += len(members)
        scores = {
            member.task_digest: sum(
                structure_similarity(member, other) for other in members
            )
            / len(members)
            for member in members
        }
        representative = min(
            members,
            key=lambda member: (
                -scores[member.task_digest],
                member.task_digest,
            ),
        )
        node_tasks: Counter[str] = Counter()
        node_runs: Counter[str] = Counter()
        edge_tasks: Counter[tuple[str, str, str]] = Counter()
        edge_runs: Counter[tuple[str, str, str]] = Counter()
        linked = 0
        for member in members:
            member_nodes = _counter(member, "nodes")
            member_edges = _counter(member, "edges")
            node_tasks.update(member_nodes.keys())
            node_runs.update(member_nodes)
            edge_tasks.update(member_edges.keys())
            edge_runs.update(member_edges)
            linked += bool(member_edges)
        family_id = "family:" + representative.fingerprint[:16]
        families.append(
            DiscoveredWorkflowFamily(
                family_id,
                len(members),
                len({member.fingerprint for member in members}),
                sum(scores.values()) / len(scores),
                linked / len(members),
                representative.task_digest,
                tuple(sorted(member.task_digest for member in members)),
                tuple(
                    DiscoveredFamilyNode(
                        label, node_tasks[label], node_runs[label]
                    )
                    for label in sorted(node_tasks)
                ),
                tuple(
                    DiscoveredFamilyEdge(
                        *edge, edge_tasks[edge], edge_runs[edge]
                    )
                    for edge in sorted(edge_tasks)
                ),
            )
        )
        assignments.extend(
            WorkflowFamilyAssignment(
                member.task_digest,
                family_id,
                "assigned",
                structure_similarity(member, representative),
                member.fingerprint,
                len(member.trace_ids),
                member.identity_source,
                member.correlation_ambiguous,
            )
            for member in members
        )
    report = WorkflowDiscoveryReport(
        total_tasks,
        len(eligible),
        tuple(
            sorted(families, key=lambda item: (-item.support, item.family_id))
        ),
        len(eligible) - clustered,
        ambiguous,
        correlation.tasks,
        correlation.traces,
        correlation.uncorrelated_traces,
        correlation.ambiguous,
        tuple(sorted(assignments, key=lambda item: item.task_digest)),
    )
    if progress is not None:
        progress(total_work, total_work, "discovery complete")
    return report

"""Deterministic structural comparison of discovery snapshots."""

from __future__ import annotations

from ctrlrtn.workflow.discovery import DiscoveredWorkflowFamily
from ctrlrtn.workflow.discovery_job.artifacts import (
    report_from_workflow_discovery_artifact,
    scope_from_workflow_discovery_artifact,
    verify_workflow_discovery_artifact,
)
from ctrlrtn.workflow.discovery_job.models import (
    WorkflowDiscoveryComparison,
    WorkflowDiscoveryJobError,
    WorkflowFamilySnapshotMatch,
)
from ctrlrtn.workflow.discovery_job.scope import (
    _render_scope,
    _scope_relationship,
)


def _family_similarity(
    previous: DiscoveredWorkflowFamily, current: DiscoveredWorkflowFamily
) -> float:
    previous_nodes = {node.label for node in previous.nodes}
    current_nodes = {node.label for node in current.nodes}
    node_union = previous_nodes | current_nodes
    node_score = (
        len(previous_nodes & current_nodes) / len(node_union)
        if node_union
        else 1.0
    )
    previous_edges = {
        (edge.source, edge.target, edge.kind) for edge in previous.edges
    }
    current_edges = {
        (edge.source, edge.target, edge.kind) for edge in current.edges
    }
    edge_union = previous_edges | current_edges
    if not edge_union:
        return node_score
    edge_score = len(previous_edges & current_edges) / len(edge_union)
    return 0.6 * node_score + 0.4 * edge_score


def compare_workflow_discovery_artifacts(
    previous_value: object,
    current_value: object,
    *,
    min_similarity: float = 0.6,
    allow_unrelated: bool = False,
) -> WorkflowDiscoveryComparison:
    if not 0 < min_similarity <= 1:
        raise WorkflowDiscoveryJobError(
            "workflow snapshot comparison similarity must be in (0, 1]"
        )
    previous_artifact = verify_workflow_discovery_artifact(previous_value)
    current_artifact = verify_workflow_discovery_artifact(current_value)
    previous = report_from_workflow_discovery_artifact(previous_artifact)
    current = report_from_workflow_discovery_artifact(current_artifact)
    previous_scope = scope_from_workflow_discovery_artifact(previous_artifact)
    current_scope = scope_from_workflow_discovery_artifact(current_artifact)
    scope_relationship = _scope_relationship(previous_scope, current_scope)
    if scope_relationship == "unrelated" and not allow_unrelated:
        raise WorkflowDiscoveryJobError(
            "workflow discovery scopes differ across multiple or incompatible "
            "dimensions; pass --allow-unrelated to compare explicitly"
        )
    candidates = sorted(
        (
            (_family_similarity(left, right), left, right)
            for left in previous.families
            for right in current.families
        ),
        key=lambda item: (-item[0], item[1].family_id, item[2].family_id),
    )
    used_previous: set[str] = set()
    used_current: set[str] = set()
    matches = []
    for score, left, right in candidates:
        if score < min_similarity:
            break
        if left.family_id in used_previous or right.family_id in used_current:
            continue
        used_previous.add(left.family_id)
        used_current.add(right.family_id)
        matches.append(
            WorkflowFamilySnapshotMatch(
                left.family_id,
                right.family_id,
                score,
                left.support,
                right.support,
            )
        )
    mapping = {
        item.previous_family_id: item.current_family_id for item in matches
    }
    previous_tasks = {
        item.task_digest: item.family_id
        for item in previous.assignments
        if item.status == "assigned"
    }
    current_tasks = {
        item.task_digest: item.family_id
        for item in current.assignments
        if item.status == "assigned"
    }
    shared = set(previous_tasks) & set(current_tasks)
    retained = sum(
        mapping.get(previous_tasks[task]) == current_tasks[task]
        for task in shared
    )
    return WorkflowDiscoveryComparison(
        previous_artifact["artifact_sha256"],
        current_artifact["artifact_sha256"],
        previous_artifact.get("parameters", {})
        == current_artifact.get("parameters", {})
        and previous.algorithm == current.algorithm,
        scope_relationship,
        previous_scope,
        current_scope,
        tuple(matches),
        tuple(
            sorted(
                family.family_id
                for family in current.families
                if family.family_id not in used_current
            )
        ),
        tuple(
            sorted(
                family.family_id
                for family in previous.families
                if family.family_id not in used_previous
            )
        ),
        retained,
        len(shared) - retained,
        len(set(current_tasks) - set(previous_tasks)),
        len(set(previous_tasks) - set(current_tasks)),
    )


def render_workflow_discovery_comparison(
    comparison: WorkflowDiscoveryComparison,
) -> str:
    lines = [
        "workflow discovery snapshot comparison",
        f"previous: {comparison.previous_sha256[:16]}",
        f"current:  {comparison.current_sha256[:16]}",
        "parameters: "
        + ("compatible" if comparison.compatible_parameters else "DIFFERENT"),
        f"scope relationship: {comparison.scope_relationship}",
        f"previous scope: {_render_scope(comparison.previous_scope)}",
        f"current scope:  {_render_scope(comparison.current_scope)}",
        "",
        "family matches:",
    ]
    lines.extend(
        f"  {item.previous_family_id} -> {item.current_family_id}: "
        f"shape={item.similarity:.1%}, support {item.previous_support} -> {item.current_support}"
        for item in comparison.matches
    )
    if not comparison.matches:
        lines.append("  none")
    lines.extend(
        [
            "",
            f"added families: {', '.join(comparison.added_families) or 'none'}",
            f"removed families: {', '.join(comparison.removed_families) or 'none'}",
            f"tasks: {comparison.retained_tasks} retained · "
            f"{comparison.moved_tasks} moved · {comparison.added_tasks} added · "
            f"{comparison.removed_tasks} removed",
            "",
            "Comparison is structural and analysis-only; it grants no stable workflow identity.",
        ]
    )
    return "\n".join(lines)

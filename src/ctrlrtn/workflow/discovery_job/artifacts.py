"""Versioned artifact codecs for workflow-discovery results."""

from __future__ import annotations

import time
from dataclasses import asdict

from ctrlrtn.workflow.discovery import (
    DiscoveredFamilyEdge,
    DiscoveredFamilyNode,
    DiscoveredWorkflowFamily,
    WorkflowDiscoveryReport,
    WorkflowFamilyAssignment,
)
from ctrlrtn.workflow.discovery_job.models import (
    ARTIFACT_KIND,
    AUTHORITY,
    SUPPORTED_ARTIFACT_VERSIONS,
    VERSION,
    WorkflowDiscoveryJobError,
    WorkflowDiscoveryScope,
)
from ctrlrtn.workflow.discovery_job.scope import (
    _digest,
    _selection_document,
    _validate_scope,
)
from ctrlrtn.workflow.discovery_projection import (
    DiscoveredFamilyProjection,
    projection_document,
    projection_from_document,
)


def _report_document(report: WorkflowDiscoveryReport) -> dict:
    return {
        "algorithm": report.algorithm,
        "total_tasks": report.total_tasks,
        "eligible_tasks": report.eligible_tasks,
        "unclustered_tasks": report.unclustered_tasks,
        "ambiguous_tool_links": report.ambiguous_tool_links,
        "synthetic_tasks": report.synthetic_tasks,
        "synthetic_traces": report.synthetic_traces,
        "uncorrelated_traces": report.uncorrelated_traces,
        "ambiguous_correlations": report.ambiguous_correlations,
        "families": [asdict(family) for family in report.families],
        "assignments": [asdict(item) for item in report.assignments],
    }


def build_workflow_discovery_artifact(
    report: WorkflowDiscoveryReport,
    *,
    input_sha256: str,
    input_traces: int,
    parameters: dict | None = None,
    projections: tuple[DiscoveredFamilyProjection, ...] = (),
    scope: WorkflowDiscoveryScope | None = None,
    selection: dict | None = None,
    created_at: float | None = None,
) -> dict:
    document = {
        "version": VERSION,
        "kind": ARTIFACT_KIND,
        "authority": AUTHORITY,
        "created_at": time.time() if created_at is None else created_at,
        "input": {
            "sha256": input_sha256,
            "traces": input_traces,
        },
        "parameters": parameters or {},
        "report": _report_document(report),
        "projections": [projection_document(item) for item in projections],
        "scope": asdict(scope or WorkflowDiscoveryScope()),
        "selection": _selection_document(selection),
    }
    document["artifact_sha256"] = _digest(document)
    return document


def verify_workflow_discovery_artifact(value: object) -> dict:
    if not isinstance(value, dict):
        raise WorkflowDiscoveryJobError(
            "workflow discovery artifact must be an object"
        )
    if (
        value.get("version") not in SUPPORTED_ARTIFACT_VERSIONS
        or value.get("kind") != ARTIFACT_KIND
    ):
        raise WorkflowDiscoveryJobError(
            "workflow discovery artifact version or kind is invalid"
        )
    if value.get("authority") != AUTHORITY:
        raise WorkflowDiscoveryJobError(
            "workflow discovery artifact authority is invalid"
        )
    digest = value.get("artifact_sha256")
    unsigned = {
        key: item for key, item in value.items() if key != "artifact_sha256"
    }
    if not isinstance(digest, str) or digest != _digest(unsigned):
        raise WorkflowDiscoveryJobError(
            "workflow discovery artifact digest mismatch"
        )
    inputs, report = value.get("input"), value.get("report")
    if not isinstance(inputs, dict) or not isinstance(report, dict):
        raise WorkflowDiscoveryJobError(
            "workflow discovery artifact is incomplete"
        )
    algorithm = report.get("algorithm")
    if (
        not isinstance(algorithm, str)
        or not algorithm.startswith("observed-tool-graph-family/v")
        or not isinstance(report.get("families"), list)
        or not isinstance(inputs.get("sha256"), str)
        or len(inputs["sha256"]) != 64
        or isinstance(inputs.get("traces"), bool)
        or not isinstance(inputs.get("traces"), int)
        or inputs["traces"] < 0
    ):
        raise WorkflowDiscoveryJobError(
            "workflow discovery artifact report is invalid"
        )
    if value.get("version") == VERSION:
        selection = value.get("selection")
        required = {
            "available_explicit_tasks",
            "selected_explicit_tasks",
            "truncated_explicit_tasks",
            "excluded_by_scope_tasks",
            "declared_tasks",
            "available_unscoped_traces",
            "selected_unscoped_traces",
            "truncated_unscoped_traces",
            "excluded_unscoped_traces",
            "selected_traces",
            "pruned_selected_traces",
            "unkeyable_selected_traces",
            "selection_strategy",
        }
        if (
            not isinstance(selection, dict)
            or not required <= set(selection)
            or any(
                isinstance(selection[key], bool)
                or not isinstance(selection[key], int)
                or selection[key] < 0
                for key in required - {"selection_strategy"}
            )
            or selection["selection_strategy"]
            != "most-recent-task-last-call/v1"
        ):
            raise WorkflowDiscoveryJobError(
                "workflow discovery artifact selection diagnostics are invalid"
            )
    return value


def render_workflow_discovery_selection(selection: dict) -> str:
    available_tasks = int(selection.get("available_explicit_tasks", 0))
    selected_tasks = int(selection.get("selected_explicit_tasks", 0))
    coverage = selected_tasks / available_tasks if available_tasks else 0.0
    return "\n".join(
        [
            "selection diagnostics:",
            f"  strategy: {selection.get('selection_strategy', 'unknown')}",
            f"  explicit tasks: {selected_tasks}/{available_tasks} ({coverage:.1%}) selected · "
            f"{selection.get('truncated_explicit_tasks', 0)} truncated · "
            f"{selection.get('excluded_by_scope_tasks', 0)} scope-excluded · "
            f"{selection.get('declared_tasks', 0)} declared-workflow excluded",
            f"  unscoped traces: {selection.get('selected_unscoped_traces', 0)}/"
            f"{selection.get('available_unscoped_traces', 0)} selected · "
            f"{selection.get('truncated_unscoped_traces', 0)} truncated · "
            f"{selection.get('excluded_unscoped_traces', 0)} scope-excluded",
            f"  selected traces: {selection.get('selected_traces', 0)} · "
            f"{selection.get('pruned_selected_traces', 0)} payload-pruned · "
            f"{selection.get('unkeyable_selected_traces', 0)} unkeyable",
        ]
    )


def report_from_workflow_discovery_artifact(
    value: object,
) -> WorkflowDiscoveryReport:
    artifact = verify_workflow_discovery_artifact(value)
    raw = artifact["report"]
    try:
        families = tuple(
            DiscoveredWorkflowFamily(
                family_id=family["family_id"],
                support=family["support"],
                variants=family["variants"],
                cohesion=family["cohesion"],
                linked_task_rate=family["linked_task_rate"],
                representative_task_digest=family["representative_task_digest"],
                member_task_digests=tuple(family["member_task_digests"]),
                nodes=tuple(
                    DiscoveredFamilyNode(**node) for node in family["nodes"]
                ),
                edges=tuple(
                    DiscoveredFamilyEdge(**edge) for edge in family["edges"]
                ),
            )
            for family in raw["families"]
        )
        return WorkflowDiscoveryReport(
            total_tasks=raw["total_tasks"],
            eligible_tasks=raw["eligible_tasks"],
            families=families,
            unclustered_tasks=raw["unclustered_tasks"],
            ambiguous_tool_links=raw["ambiguous_tool_links"],
            synthetic_tasks=raw["synthetic_tasks"],
            synthetic_traces=raw["synthetic_traces"],
            uncorrelated_traces=raw["uncorrelated_traces"],
            ambiguous_correlations=raw["ambiguous_correlations"],
            assignments=tuple(
                WorkflowFamilyAssignment(**item)
                for item in raw.get("assignments", [])
            ),
            algorithm=raw["algorithm"],
        )
    except (KeyError, TypeError, ValueError) as exc:
        raise WorkflowDiscoveryJobError(
            f"workflow discovery artifact report is malformed: {exc}"
        ) from None


def projections_from_workflow_discovery_artifact(
    value: object,
) -> tuple[DiscoveredFamilyProjection, ...]:
    artifact = verify_workflow_discovery_artifact(value)
    try:
        return tuple(
            projection_from_document(item)
            for item in artifact.get("projections", [])
        )
    except (KeyError, TypeError, ValueError) as exc:
        raise WorkflowDiscoveryJobError(
            f"workflow discovery projections are malformed: {exc}"
        ) from None


def scope_from_workflow_discovery_artifact(
    value: object,
) -> WorkflowDiscoveryScope:
    artifact = verify_workflow_discovery_artifact(value)
    try:
        scope = WorkflowDiscoveryScope(**artifact.get("scope", {}))
        _validate_scope(scope)
        return scope
    except (TypeError, ValueError) as exc:
        raise WorkflowDiscoveryJobError(
            f"workflow discovery scope is malformed: {exc}"
        ) from None

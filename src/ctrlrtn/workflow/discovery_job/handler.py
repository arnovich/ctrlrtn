"""Worker execution for frozen-input workflow-discovery jobs."""

from __future__ import annotations

from ctrlrtn.workflow.discovery import (
    ALGORITHM,
    discover_workflow_families,
)
from ctrlrtn.workflow.discovery_job.artifacts import (
    build_workflow_discovery_artifact,
    verify_workflow_discovery_artifact,
)
from ctrlrtn.workflow.discovery_job.models import (
    MAX_TRACES,
    VERSION,
    WorkflowDiscoveryJobError,
    WorkflowDiscoveryScope,
)
from ctrlrtn.workflow.discovery_job.scope import (
    _digest,
    _trace_binding,
    _validate_scope,
)
from ctrlrtn.workflow.discovery_projection import (
    build_discovered_family_projections,
)


def run_workflow_discovery_job(context, config: dict) -> dict:
    if config.get("version") != VERSION or config.get("algorithm") != ALGORITHM:
        raise WorkflowDiscoveryJobError(
            "unsupported workflow discovery job configuration"
        )
    try:
        scope = WorkflowDiscoveryScope(**config.get("scope", {}))
        _validate_scope(scope)
    except (TypeError, ValueError) as exc:
        raise WorkflowDiscoveryJobError(
            f"workflow discovery job scope is malformed: {exc}"
        ) from None
    trace_ids = config.get("trace_ids")
    bindings = config.get("trace_bindings")
    selection = config.get("selection")
    if not isinstance(trace_ids, list) or not isinstance(bindings, list):
        raise WorkflowDiscoveryJobError(
            "workflow discovery job has no frozen inputs"
        )
    if not isinstance(selection, dict) or selection.get(
        "selected_traces"
    ) != len(trace_ids):
        raise WorkflowDiscoveryJobError(
            "workflow discovery job selection diagnostics are invalid"
        )
    if (
        len(trace_ids) != len(bindings)
        or len(trace_ids) > MAX_TRACES
        or len(set(trace_ids)) != len(trace_ids)
    ):
        raise WorkflowDiscoveryJobError(
            "workflow discovery frozen input set is invalid"
        )
    if config.get("input_sha256") != _digest(bindings):
        raise WorkflowDiscoveryJobError(
            "workflow discovery input binding digest mismatch"
        )
    rows = context.store.workflow_discovery_inputs_by_ids(trace_ids)
    if len(rows) != len(trace_ids):
        raise WorkflowDiscoveryJobError(
            "one or more frozen workflow discovery traces are missing"
        )
    expected = {
        item.get("trace_id"): item
        for item in bindings
        if isinstance(item, dict)
    }
    for index, row in enumerate(rows, 1):
        if expected.get(row["id"]) != _trace_binding(row):
            raise WorkflowDiscoveryJobError(
                f"frozen workflow discovery trace {row['id']} changed"
            )
        if index == len(rows) or index % 64 == 0:
            context.progress(index, len(rows), "verifying frozen inputs")
    report = discover_workflow_families(
        rows,
        min_support=config["min_support"],
        similarity=config["similarity"],
        progress=context.progress,
    )
    artifact = build_workflow_discovery_artifact(
        report,
        input_sha256=config["input_sha256"],
        input_traces=len(rows),
        parameters={
            "limit": config["limit"],
            "min_support": config["min_support"],
            "similarity": config["similarity"],
        },
        projections=build_discovered_family_projections(rows, report),
        scope=scope,
        selection=config.get("selection", {}),
    )
    return verify_workflow_discovery_artifact(artifact)

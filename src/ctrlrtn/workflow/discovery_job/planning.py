"""Frozen-input planning for workflow-discovery jobs."""

from __future__ import annotations

from dataclasses import asdict

from ctrlrtn.jobs import Job
from ctrlrtn.workflow.discovery import ALGORITHM
from ctrlrtn.workflow.discovery_job.models import (
    KIND,
    MAX_LIMIT,
    MAX_TRACES,
    VERSION,
    WorkflowDiscoveryJobError,
    WorkflowDiscoveryJobPlan,
    WorkflowDiscoveryScope,
)
from ctrlrtn.workflow.discovery_job.scope import (
    _digest,
    _trace_binding,
    _validate_scope,
)


def prepare_workflow_discovery_job(
    store,
    *,
    limit: int = 1000,
    min_support: int = 2,
    similarity: float = 0.75,
    scope: WorkflowDiscoveryScope | None = None,
) -> WorkflowDiscoveryJobPlan:
    if isinstance(limit, bool) or not isinstance(limit, int) or limit < 1:
        raise WorkflowDiscoveryJobError(
            "workflow discovery limit must be at least 1"
        )
    if limit > MAX_LIMIT:
        raise WorkflowDiscoveryJobError(
            f"workflow discovery job limit must not exceed {MAX_LIMIT}"
        )
    if (
        isinstance(min_support, bool)
        or not isinstance(min_support, int)
        or min_support < 2
    ):
        raise WorkflowDiscoveryJobError(
            "workflow family min_support must be at least 2"
        )
    if (
        isinstance(similarity, bool)
        or not isinstance(similarity, (int, float))
        or not 0 < similarity <= 1
    ):
        raise WorkflowDiscoveryJobError(
            "workflow family similarity must be in (0, 1]"
        )
    scope = scope or WorkflowDiscoveryScope()
    _validate_scope(scope)
    rows = store.workflow_discovery_inputs(limit, **asdict(scope))
    if not rows:
        raise WorkflowDiscoveryJobError(
            "no legacy workflow traffic is available"
        )
    if len(rows) > MAX_TRACES:
        raise WorkflowDiscoveryJobError(
            f"workflow discovery sample exceeds {MAX_TRACES} traces; lower --limit"
        )
    bindings = [_trace_binding(row) for row in rows]
    selection = asdict(
        store.workflow_discovery_input_diagnostics(rows, **asdict(scope))
    )
    explicit = {
        row["task_id"]
        for row in rows
        if isinstance(row.get("task_id"), str) and row["task_id"]
    }
    job = Job(
        KIND,
        {
            "version": VERSION,
            "algorithm": ALGORITHM,
            "limit": limit,
            "min_support": min_support,
            "similarity": similarity,
            "trace_ids": [row["id"] for row in rows],
            "trace_bindings": bindings,
            "input_sha256": _digest(bindings),
            "scope": asdict(scope),
            "selection": selection,
        },
        progress_total=len(rows),
        progress_message="queued",
    )
    return WorkflowDiscoveryJobPlan(
        job,
        len(rows),
        len(explicit),
        sum(row.get("task_id") is None for row in rows),
        selection,
    )

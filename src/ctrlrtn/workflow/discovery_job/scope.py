"""Scope validation, evidence binding, and canonical hashing."""

from __future__ import annotations

import hashlib
import json

from ctrlrtn.workflow.discovery_job.models import (
    WorkflowDiscoveryJobError,
    WorkflowDiscoveryScope,
)


def _canonical(value: object) -> bytes:
    return json.dumps(
        value, sort_keys=True, separators=(",", ":"), allow_nan=False
    ).encode()


def _digest(value: object) -> str:
    return hashlib.sha256(_canonical(value)).hexdigest()


def _selection_document(selection: dict | None = None) -> dict:
    document = {
        "available_explicit_tasks": 0,
        "selected_explicit_tasks": 0,
        "truncated_explicit_tasks": 0,
        "excluded_by_scope_tasks": 0,
        "declared_tasks": 0,
        "available_unscoped_traces": 0,
        "selected_unscoped_traces": 0,
        "truncated_unscoped_traces": 0,
        "excluded_unscoped_traces": 0,
        "selected_traces": 0,
        "pruned_selected_traces": 0,
        "unkeyable_selected_traces": 0,
        "selection_strategy": "most-recent-task-last-call/v1",
    }
    document.update(selection or {})
    return document


def _trace_binding(row: dict) -> dict:
    evidence = {
        "id": row["id"],
        "ts": row["ts"],
        "task_id": row.get("task_id"),
        "use_case_key": row.get("use_case_key"),
        "request_sha256": hashlib.sha256(
            row.get("request_body") or b""
        ).hexdigest(),
        "response_sha256": hashlib.sha256(
            row.get("response_body") or b""
        ).hexdigest(),
        "provider": row.get("provider"),
        "model": row.get("model"),
        "served_model": row.get("served_model"),
        "input_tokens": row.get("input_tokens"),
        "output_tokens": row.get("output_tokens"),
        "cost_usd": row.get("cost_usd"),
        "latency_ms": row.get("latency_ms"),
        "status_code": row.get("status_code"),
        "experiment_id": row.get("experiment_id"),
        "arm": row.get("arm"),
    }
    return {"trace_id": row["id"], "evidence_sha256": _digest(evidence)}


def _validate_scope(scope: WorkflowDiscoveryScope) -> None:
    for name in ("provider", "model", "experiment_id", "arm"):
        value = getattr(scope, name)
        if value is not None and (
            not isinstance(value, str) or not value.strip()
        ):
            raise WorkflowDiscoveryJobError(
                f"workflow discovery scope {name} must be a non-empty string"
            )
    for name in ("since", "until"):
        value = getattr(scope, name)
        if value is not None and (
            isinstance(value, bool) or not isinstance(value, (int, float))
        ):
            raise WorkflowDiscoveryJobError(
                f"workflow discovery scope {name} must be a timestamp"
            )
    if (
        scope.since is not None
        and scope.until is not None
        and scope.since >= scope.until
    ):
        raise WorkflowDiscoveryJobError(
            "workflow discovery scope since must be before until"
        )
    if scope.arm is not None and scope.experiment_id is None:
        raise WorkflowDiscoveryJobError(
            "workflow discovery arm scope requires experiment_id"
        )


def validate_workflow_discovery_scope(scope: WorkflowDiscoveryScope) -> None:
    _validate_scope(scope)


def _scope_relationship(
    previous: WorkflowDiscoveryScope, current: WorkflowDiscoveryScope
) -> str:
    if previous == current:
        return "same-scope"
    changed = {
        name
        for name in (
            "since",
            "until",
            "provider",
            "model",
            "experiment_id",
            "arm",
        )
        if getattr(previous, name) != getattr(current, name)
    }
    if changed <= {"since", "until"}:
        return "time-windows"
    if changed == {"provider"} and previous.provider and current.provider:
        return "providers"
    if changed == {"model"} and previous.model and current.model:
        return "models"
    if (
        changed == {"arm"}
        and previous.experiment_id == current.experiment_id
        and previous.experiment_id is not None
        and previous.arm
        and current.arm
    ):
        return "experiment-arms"
    return "unrelated"


def _render_scope(scope: WorkflowDiscoveryScope) -> str:
    selected = [
        f"{name}={getattr(scope, name)}"
        for name in (
            "since",
            "until",
            "provider",
            "model",
            "experiment_id",
            "arm",
        )
        if getattr(scope, name) is not None
    ]
    return ", ".join(selected) or "all legacy traffic"

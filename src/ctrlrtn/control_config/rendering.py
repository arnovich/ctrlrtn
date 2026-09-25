"""Canonical desired-state serialization and active-versus-desired diffs."""

from __future__ import annotations

import difflib
import json

import yaml

from ctrlrtn.control_config.models import (
    VERSION,
    ControlConfig,
    ControlRevision,
    WorkflowDefinition,
)
from ctrlrtn.policy.experiment import Experiment
from ctrlrtn.policy.route import Route, WorkflowRoute


def canonical_document(config: ControlConfig) -> str:
    data: dict = {
        "version": VERSION,
        "routes": {},
        "experiments": {},
        "workflow_routes": {},
        "workflows": {},
    }
    for route in sorted(config.routes, key=lambda row: row.use_case_key):
        fields: dict[str, object] = {"model": route.model}
        for key in ("provider", "previous_model", "note"):
            value = getattr(route, key)
            if value is not None:
                fields[key] = value
        data["routes"][route.use_case_key] = fields
    for exp in sorted(config.experiments, key=lambda row: row.use_case_key):
        fields = {
            "id": exp.experiment_id,
            "candidate_model": exp.candidate_model,
            "split_pct": exp.split_pct,
            "max_calls_per_task": exp.max_calls_per_task,
        }
        if exp.candidate_provider is not None:
            fields["provider"] = exp.candidate_provider
        for key in ("workflow", "workflow_version", "step"):
            value = getattr(exp, key)
            if value is not None:
                fields[key] = value
        data["experiments"][exp.use_case_key] = fields
    for workflow_route in sorted(
        config.workflow_routes,
        key=lambda row: (row.workflow, row.workflow_version, row.step or ""),
    ):
        version = (
            data["workflow_routes"]
            .setdefault(workflow_route.workflow, {})
            .setdefault(workflow_route.workflow_version, {})
        )
        target = (
            version
            if workflow_route.step is None
            else version.setdefault("steps", {}).setdefault(
                workflow_route.step, {}
            )
        )
        target["model"] = workflow_route.model
        if workflow_route.provider is not None:
            target["provider"] = workflow_route.provider
        if workflow_route.note is not None:
            target["note"] = workflow_route.note
    for workflow in sorted(
        config.workflows, key=lambda row: (row.workflow, row.workflow_version)
    ):
        steps = {}
        for step in workflow.steps:
            fields = {}
            if step.predecessors:
                fields["predecessors"] = list(step.predecessors)
            allows = {}
            if step.fan_out:
                allows["fan_out"] = True
            if step.retry:
                allows["retry"] = True
            if allows:
                fields["allows"] = allows
            if step.condition is not None:
                fields["condition"] = step.condition
            steps[step.name] = fields
        data["workflows"].setdefault(workflow.workflow, {})[
            workflow.workflow_version
        ] = {"steps": steps}
    return yaml.safe_dump(data, sort_keys=False).rstrip() + "\n"


def live_config(
    routes: list[Route],
    experiments: list[Experiment],
    workflow_routes: list[WorkflowRoute] | None = None,
    workflows: list[WorkflowDefinition] | None = None,
) -> ControlConfig:
    return ControlConfig(
        tuple(routes),
        tuple(exp for exp in experiments if exp.is_running),
        tuple(workflow_routes or ()),
        tuple(workflows or ()),
    )


def config_diff(current: ControlConfig, desired: ControlConfig) -> str:
    return "".join(
        difflib.unified_diff(
            canonical_document(current).splitlines(keepends=True),
            canonical_document(desired).splitlines(keepends=True),
            fromfile="active",
            tofile="desired",
        )
    )


def revision_json(revision: ControlRevision) -> str:
    return json.dumps(
        {
            "revision": revision.revision,
            "source_path": revision.source_path,
            "document_sha256": revision.document_sha256,
            "activated_at": revision.activated_at,
        },
        sort_keys=True,
    )

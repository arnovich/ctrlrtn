"""Shared contracts and format constants for workflow-discovery jobs."""

from __future__ import annotations

from dataclasses import dataclass

from ctrlrtn.jobs import Job

KIND = "workflow_discovery"
ARTIFACT_KIND = "workflow-discovery-result"
VERSION = 5
SUPPORTED_ARTIFACT_VERSIONS = frozenset({1, 2, 3, 4, VERSION})
AUTHORITY = "analysis_only_no_routing_or_execution"
MAX_LIMIT = 2000
MAX_TRACES = 20000


class WorkflowDiscoveryJobError(ValueError):
    pass


@dataclass(frozen=True)
class WorkflowDiscoveryJobPlan:
    job: Job
    traces: int
    explicit_tasks: int
    unscoped_traces: int
    selection: dict


@dataclass(frozen=True)
class WorkflowDiscoveryScope:
    since: float | None = None
    until: float | None = None
    provider: str | None = None
    model: str | None = None
    experiment_id: str | None = None
    arm: str | None = None


@dataclass(frozen=True)
class WorkflowFamilySnapshotMatch:
    previous_family_id: str
    current_family_id: str
    similarity: float
    previous_support: int
    current_support: int


@dataclass(frozen=True)
class WorkflowDiscoveryComparison:
    previous_sha256: str
    current_sha256: str
    compatible_parameters: bool
    scope_relationship: str
    previous_scope: WorkflowDiscoveryScope
    current_scope: WorkflowDiscoveryScope
    matches: tuple[WorkflowFamilySnapshotMatch, ...]
    added_families: tuple[str, ...]
    removed_families: tuple[str, ...]
    retained_tasks: int
    moved_tasks: int
    added_tasks: int
    removed_tasks: int

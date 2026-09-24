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
    """Raised when a discovery job's inputs, scope, or artifact are invalid."""

    pass


@dataclass(frozen=True)
class WorkflowDiscoveryJobPlan:
    """A queued discovery job together with its frozen-input summary.

    ``job`` carries the frozen trace IDs, evidence bindings and input digest
    the worker later re-verifies; the remaining fields describe the sample
    so operators can see how many explicit tasks and unscoped traces it
    holds before running it.
    """

    job: Job
    traces: int
    explicit_tasks: int
    unscoped_traces: int
    selection: dict


@dataclass(frozen=True)
class WorkflowDiscoveryScope:
    """Immutable cohort filter frozen into a discovery job and its artifact.

    It bounds tasks by last-observed time, provider, served model,
    experiment and arm. A matching task contributes its whole trajectory
    rather than having non-matching calls cut from its graph; ``arm``
    requires ``experiment_id``, and all-``None`` means all legacy traffic.
    """

    since: float | None = None
    until: float | None = None
    provider: str | None = None
    model: str | None = None
    experiment_id: str | None = None
    arm: str | None = None


@dataclass(frozen=True)
class WorkflowFamilySnapshotMatch:
    """One-to-one pairing of a family across two discovery snapshots.

    Families are matched by normalized node/edge shape, not by ID, because
    representative-derived family IDs may legitimately change between runs.
    """

    previous_family_id: str
    current_family_id: str
    similarity: float
    previous_support: int
    current_support: int


@dataclass(frozen=True)
class WorkflowDiscoveryComparison:
    """Structural diff between two verified discovery artifacts.

    Snapshots are identified by artifact digest. ``compatible_parameters``
    is false when algorithm or parameters differ (never silently
    normalized), ``scope_relationship`` names how the scopes differ, and
    the task counts say which assigned digests stayed in a matched family,
    moved, appeared, or disappeared. The comparison is analysis-only and
    grants no stable workflow identity.
    """

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

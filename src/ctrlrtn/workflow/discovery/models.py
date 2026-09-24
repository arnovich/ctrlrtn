"""Contracts for analysis-only workflow-family discovery."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

ALGORITHM = "observed-tool-graph-family/v4"
DiscoveryProgress = Callable[[int, int, str], None]


@dataclass(frozen=True)
class ObservedTaskStructure:
    task_digest: str
    trace_ids: tuple[int, ...]
    nodes: tuple[tuple[str, int], ...]
    edges: tuple[tuple[str, str, str, int], ...]
    fingerprint: str
    identity_source: str
    correlation_ambiguous: bool
    chronological_labels: tuple[str, ...] = ()


@dataclass(frozen=True)
class DiscoveredFamilyNode:
    label: str
    tasks: int
    runs: int


@dataclass(frozen=True)
class DiscoveredFamilyEdge:
    source: str
    target: str
    kind: str
    tasks: int
    occurrences: int


@dataclass(frozen=True)
class DiscoveredWorkflowFamily:
    family_id: str
    support: int
    variants: int
    cohesion: float
    linked_task_rate: float
    representative_task_digest: str
    member_task_digests: tuple[str, ...]
    nodes: tuple[DiscoveredFamilyNode, ...]
    edges: tuple[DiscoveredFamilyEdge, ...]


@dataclass(frozen=True)
class WorkflowFamilyAssignment:
    task_digest: str
    family_id: str | None
    status: str
    match_score: float | None
    variant_fingerprint: str
    trace_count: int
    identity_source: str
    ambiguous: bool
    reason: str | None = None


@dataclass(frozen=True)
class WorkflowDiscoveryReport:
    total_tasks: int
    eligible_tasks: int
    families: tuple[DiscoveredWorkflowFamily, ...]
    unclustered_tasks: int
    ambiguous_tool_links: int
    synthetic_tasks: int = 0
    synthetic_traces: int = 0
    uncorrelated_traces: int = 0
    ambiguous_correlations: int = 0
    assignments: tuple[WorkflowFamilyAssignment, ...] = ()
    algorithm: str = ALGORITHM


@dataclass(frozen=True)
class SyntheticCorrelation:
    tasks: int
    traces: int
    uncorrelated_traces: int
    ambiguous: int

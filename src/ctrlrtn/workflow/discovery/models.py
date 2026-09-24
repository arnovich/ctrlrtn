"""Contracts for analysis-only workflow-family discovery."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

ALGORITHM = "observed-tool-graph-family/v4"
DiscoveryProgress = Callable[[int, int, str], None]


@dataclass(frozen=True)
class ObservedTaskStructure:
    """One task's observed calls normalized into a node/edge multiset.

    Nodes are ``(label, count)`` pairs built from each call's use-case route
    and produced tool-name shape; edges are exact tool-result links only,
    never timing. The raw task ID is replaced by ``task_digest``, and
    ``identity_source`` records whether the task was explicit or an
    analysis-only synthetic correlation (and on what evidence).
    """

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
    """One recurring call label in a family.

    ``tasks`` counts member tasks containing the label; ``runs`` counts its
    calls across them, so ``runs > tasks`` means repeats within tasks.
    """

    label: str
    tasks: int
    runs: int


@dataclass(frozen=True)
class DiscoveredFamilyEdge:
    """One exact tool-result link recurring across a family's members.

    It is evidence of a data hand-off between two labels, not a causal or
    scheduling claim; ``tasks`` counts members containing the link and
    ``occurrences`` its total repetitions.
    """

    source: str
    target: str
    kind: str
    tasks: int
    occurrences: int


@dataclass(frozen=True)
class DiscoveredWorkflowFamily:
    """A cluster of structurally similar tasks: a hypothesis, not a workflow.

    ``family_id`` derives from the representative member's fingerprint and
    may change as variants arrive. ``cohesion`` is mean pairwise similarity
    (single-link clustering can chain distant variants) and
    ``linked_task_rate`` is the fraction of members with any exact tool
    link; families without links express only a recurring role/tool-shape
    multiset. Members are referenced by task digest, never raw task ID, and
    nothing here grants routing or execution authority.
    """

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
    """Where one eligible task digest landed: in a family or unclustered.

    ``status`` is ``"assigned"`` or ``"unclustered"`` (see ``reason``),
    ``match_score`` is similarity to the family representative, and
    ``identity_source``/``ambiguous`` carry over whether the task identity
    was explicit or an analysis-only synthetic correlation.
    """

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
    """Complete result of one analysis-only family discovery run.

    Counts describe coverage honestly: ``eligible_tasks`` are tasks with two
    or more calls, ``unclustered_tasks`` fell below minimum support, and the
    synthetic/uncorrelated/ambiguous figures expose how much input relied
    on analysis-only correlation rather than explicit task identity. The
    report identifies tasks by digest only and grants no workflow authority.
    """

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
    """Coverage counts from correlating traces that carried no task ID.

    ``tasks`` is the number of synthetic fragments created, ``traces`` how
    many calls were grouped, ``uncorrelated_traces`` how many remained
    singleton fragments, and ``ambiguous`` how many calls matched more than
    one prior fragment and were therefore kept separate.
    """

    tasks: int
    traces: int
    uncorrelated_traces: int
    ambiguous: int

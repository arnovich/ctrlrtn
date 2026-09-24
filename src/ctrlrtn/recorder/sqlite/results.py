"""Result values produced by SQLite maintenance and discovery queries."""

from dataclasses import dataclass


@dataclass(frozen=True)
class TracePayloadPrune:
    """A dry-run or applied trace-payload retention result."""

    before: float
    eligible_traces: int
    protected_traces: int
    pruned_traces: int
    invalidated_inferred_edges: int
    invalidated_discovery_jobs: int
    payload_bytes: int
    applied: bool


@dataclass(frozen=True)
class WorkflowTaskErasure:
    """Dry-run or applied erasure of one task and database-derived records."""

    task_id: str
    traces: int
    workflow_events: int
    tool_operation_events: int
    inferred_edges: int
    outcomes: int
    protected_jobs: int
    applied: bool


@dataclass(frozen=True)
class DatabaseCompaction:
    """Physical database reclamation performed in exclusive maintenance."""

    bytes_before: int
    bytes_after: int
    pages_before: int
    pages_after: int


@dataclass(frozen=True)
class WorkflowDiscoveryInputDiagnostics:
    """How a frozen discovery sample was drawn: the bounded cohort of
    explicit-workflow tasks and unscoped traces that were available, how many
    were selected, truncated or excluded by scope, and how many selected
    traces were payload-pruned or unkeyable. Evidence for the discovery
    report, never an input to serving."""

    available_explicit_tasks: int
    selected_explicit_tasks: int
    truncated_explicit_tasks: int
    excluded_by_scope_tasks: int
    declared_tasks: int
    available_unscoped_traces: int
    selected_unscoped_traces: int
    truncated_unscoped_traces: int
    excluded_unscoped_traces: int
    selected_traces: int
    pruned_selected_traces: int
    unkeyable_selected_traces: int
    selection_strategy: str = "most-recent-task-last-call/v1"

"""Small in-memory store used by tests and local experiments."""

from __future__ import annotations

from ctrlrtn.control_config import ControlRevision
from ctrlrtn.policy.experiment import Experiment
from ctrlrtn.policy.fallback import ApprovedFallback
from ctrlrtn.policy.route import Route, WorkflowRoute
from ctrlrtn.policy.shadow import ShadowExperiment, ShadowStats
from ctrlrtn.recorder.models import (
    Outcome,
)
from ctrlrtn.recorder.trace import Trace
from ctrlrtn.workflow.identity import WorkflowEvent
from ctrlrtn.workflow.tool_operation import ToolOperationEvent


class MemoryStoreCore:
    """Keeps traces in a list. Test/dev only."""

    def __init__(self) -> None:
        self.traces: list[Trace] = []
        self.outcomes: list[Outcome] = []
        self._workflow_events: dict[str, WorkflowEvent] = {}
        self._tool_events: dict[str, ToolOperationEvent] = {}
        self._experiments: list[Experiment] = []
        self._routes: dict[str, Route] = {}
        self._workflow_routes: dict[
            tuple[str, str, str | None], WorkflowRoute
        ] = {}
        self._fallbacks: dict[str, ApprovedFallback] = {}
        self._shadows: list[ShadowExperiment] = []
        self._shadow_stats: dict[str, ShadowStats] = {}

    async def save(self, trace: Trace) -> None:
        self.traces.append(trace)

    async def save_outcome(self, outcome: Outcome) -> None:
        self.outcomes.append(outcome)

    def control_revision(self) -> ControlRevision | None:
        # Git-backed desired state is a SQLite-only capability; an in-memory
        # store has no revision provenance to report.
        return None

    def close(self) -> None:
        """No resources to release; defined so ownership checks can rely on
        every store being closable."""

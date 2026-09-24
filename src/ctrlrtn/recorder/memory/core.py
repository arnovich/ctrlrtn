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


class MemoryState:
    """The attributes the in-memory capability mixins share.

    Declared (not assigned) here so each mixin can be type-checked on its
    own; ``MemoryStoreCore.__init__`` is what actually creates them.
    """

    traces: list[Trace]
    outcomes: list[Outcome]
    _workflow_events: dict[str, WorkflowEvent]
    _tool_events: dict[str, ToolOperationEvent]
    _experiments: list[Experiment]
    _routes: dict[str, Route]
    _workflow_routes: dict[tuple[str, str, str | None], WorkflowRoute]
    _fallbacks: dict[str, ApprovedFallback]
    _shadows: list[ShadowExperiment]
    _shadow_stats: dict[str, ShadowStats]


class MemoryStoreCore(MemoryState):
    """Keeps traces in a list. Test/dev only."""

    def __init__(self) -> None:
        self.traces = []
        self.outcomes = []
        self._workflow_events = {}
        self._tool_events = {}
        self._experiments = []
        self._routes = {}
        self._workflow_routes = {}
        self._fallbacks = {}
        self._shadows = []
        self._shadow_stats = {}

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

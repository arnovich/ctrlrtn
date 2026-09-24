"""Storage contracts named by CAPABILITY, not by implementation.

``protocols.py`` answers "can something other than SQLite back the router?".
This module answers a different question: "how much authority does this caller
actually need?" The two are orthogonal, and only the second stops the hot path
from being handed methods it must never call.

The gateway reads serving policy and must not perform control-plane writes.
Expressing that as ``ServingRepository`` makes it a type error to call
``set_route`` from ``ExperimentRouter``, instead of a runtime failure against
a read-only SQLite connection long after the code shipped.

Each protocol is ``runtime_checkable`` so ``tests/test_repositories.py`` can
assert every adapter satisfies the contracts it claims, and the in-memory and
SQLite stores cannot silently drift apart.
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from ctrlrtn.control_config import ControlRevision
from ctrlrtn.policy.experiment import Experiment
from ctrlrtn.policy.fallback import ApprovedFallback
from ctrlrtn.policy.route import Route, WorkflowRoute
from ctrlrtn.policy.shadow import ShadowExperiment
from ctrlrtn.recorder.models import Outcome, UseCaseRanking
from ctrlrtn.recorder.trace import Trace
from ctrlrtn.workflow.identity import WorkflowEvent
from ctrlrtn.workflow.tool_operation import ToolOperationEvent


@runtime_checkable
class TraceRepository(Protocol):
    """The durable writes the recording service performs, and nothing else."""

    async def save(self, trace: Trace) -> None: ...

    async def save_outcome(self, outcome: Outcome) -> None: ...

    async def save_workflow_event(self, event: WorkflowEvent) -> None: ...

    async def save_tool_operation_event(
        self, event: ToolOperationEvent
    ) -> None: ...


@runtime_checkable
class ServingRepository(Protocol):
    """Serving policy read by the snapshot router. Deliberately read-only.

    Dependency rule 1 says the hot path performs no control-plane writes. This
    protocol is that rule made checkable: it declares every reader
    ``ExperimentRouter.refresh()`` needs and not one mutator.
    """

    def running_experiments(self) -> dict[str, Experiment]: ...

    def routes(self) -> list[Route]: ...

    def workflow_routes(self) -> list[WorkflowRoute]: ...

    def fallbacks(self) -> list[ApprovedFallback]: ...

    def control_revision(self) -> ControlRevision | None: ...


@runtime_checkable
class ShadowRepository(ServingRepository, Protocol):
    """Serving reads plus the mirror path's durable counters.

    The shadow manager is the one gateway component that writes: attrition
    counters must survive a crash to stay countable. It gets its own contract
    rather than widening ServingRepository for everyone.
    """

    def running_shadow_experiments(self) -> dict[str, ShadowExperiment]: ...

    def increment_shadow_stats(
        self,
        shadow_id: str,
        *,
        submitted: int = 0,
        completed: int = 0,
        failed: int = 0,
        dropped: int = 0,
    ) -> None: ...


@runtime_checkable
class ExperimentReader(Protocol):
    """Listing experiments — needed by both the control plane and analysis.

    Factored out so the two protocols below share one declaration and their
    signatures cannot drift apart.
    """

    def experiments(self, limit: int = 50) -> list[Experiment]: ...


@runtime_checkable
class ReportingRepository(ExperimentReader, Protocol):
    """Aggregate reads used by offline analysis.

    Campaign reporting previously depended on the concrete SQLite store — 82
    public methods — to call four of them.
    """

    def use_case_models(self) -> dict[str, str | None]: ...

    def rankings(
        self, *, baseline_only: bool = False
    ) -> list[UseCaseRanking]: ...

    def experiment_task_rows(self, experiment_id: str) -> list[dict]: ...


@runtime_checkable
class ExperimentRepository(ServingRepository, ExperimentReader, Protocol):
    """Control-plane mutations, for the CLI and console only."""

    def create_experiment(self, experiment: Experiment) -> None: ...

    def stop_experiment(self, experiment_id: str) -> bool: ...

    def adopt_experiment(self, experiment_id: str, route: Route) -> bool: ...

    def set_route(self, route: Route) -> None: ...

    def clear_route(self, use_case_key: str) -> bool: ...

    def set_fallback(self, fallback: ApprovedFallback) -> None: ...

    def clear_fallback(self, use_case_key: str) -> bool: ...

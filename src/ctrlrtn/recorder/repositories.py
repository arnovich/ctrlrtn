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

    async def save(self, trace: Trace) -> None:
        """Durably record one trace. Awaited only from the recorder's
        background worker, never on the request path; a blocking store must
        offload the write so the event loop is not held."""
        ...

    async def save_outcome(self, outcome: Outcome) -> None:
        """Durably record an app-reported task outcome; when read back, the
        latest report for a task id wins."""
        ...

    async def save_workflow_event(self, event: WorkflowEvent) -> None:
        """Durably record one workflow lifecycle event. Idempotent on
        ``event_id``: a redelivered event is ignored, not duplicated."""
        ...

    async def save_tool_operation_event(
        self, event: ToolOperationEvent
    ) -> None:
        """Durably record one authoritative tool-attempt event. Idempotent on
        ``event_id``: a redelivered event is ignored, not duplicated."""
        ...


@runtime_checkable
class ServingRepository(Protocol):
    """Serving policy read by the snapshot router. Deliberately read-only.

    Dependency rule 1 says the hot path performs no control-plane writes. This
    protocol is that rule made checkable: it declares every reader
    ``ExperimentRouter.refresh()`` needs and not one mutator.
    """

    def running_experiments(self) -> dict[str, Experiment]:
        """Every experiment in ``running`` status, keyed by use-case key; the
        store guarantees at most one per use-case."""
        ...

    def routes(self) -> list[Route]:
        """All persistent routes; empty when none are configured."""
        ...

    def workflow_routes(self) -> list[WorkflowRoute]:
        """All workflow-scoped routes; empty when none are configured."""
        ...

    def fallbacks(self) -> list[ApprovedFallback]:
        """All approved budget fallbacks; empty when none exist."""
        ...

    def control_revision(self) -> ControlRevision | None:
        """Provenance of the last activated desired-state document, or
        ``None`` when none was ever activated or the store keeps none."""
        ...


@runtime_checkable
class ShadowRepository(ServingRepository, Protocol):
    """Serving reads plus the mirror path's durable counters.

    The shadow manager is the one gateway component that writes: attrition
    counters must survive a crash to stay countable. It gets its own contract
    rather than widening ServingRepository for everyone.
    """

    def running_shadow_experiments(self) -> dict[str, ShadowExperiment]:
        """Every running shadow experiment keyed by use-case key; read on the
        mirror path's periodic snapshot refresh, never per request."""
        ...

    def increment_shadow_stats(
        self,
        shadow_id: str,
        *,
        submitted: int = 0,
        completed: int = 0,
        failed: int = 0,
        dropped: int = 0,
    ) -> None:
        """Add to one shadow's durable attrition counters atomically. Called
        from the mirror workers, never on the client's response path."""
        ...


@runtime_checkable
class ExperimentReader(Protocol):
    """Listing experiments — needed by both the control plane and analysis.

    Factored out so the two protocols below share one declaration and their
    signatures cannot drift apart.
    """

    def experiments(self, limit: int = 50) -> list[Experiment]:
        """The ``limit`` most recently created experiments, running or
        stopped, newest first."""
        ...


@runtime_checkable
class ReportingRepository(ExperimentReader, Protocol):
    """Aggregate reads used by offline analysis.

    Offline analysis needs four aggregate reads, not the whole store; this
    contract names exactly those.
    """

    def use_case_models(self) -> dict[str, str | None]:
        """The model most recently seen per use-case key (from its latest
        call), ``None`` where it could not be extracted; keyed like
        ``rankings``."""
        ...

    def rankings(self, *, baseline_only: bool = False) -> list[UseCaseRanking]:
        """Calls, tokens, latency and spend per use-case over all recorded
        history; ``baseline_only`` leaves candidate-arm calls out."""
        ...

    def experiment_task_rows(self, experiment_id: str) -> list[dict]:
        """Per-task aggregate rows for one experiment — the tripwire's input
        (see ``eval/tripwire.py``); empty for an unknown experiment."""
        ...


@runtime_checkable
class ExperimentRepository(ServingRepository, ExperimentReader, Protocol):
    """Control-plane mutations, for the CLI and console only."""

    def create_experiment(self, experiment: Experiment) -> None:
        """Persist a new experiment. ``ValueError`` if its id is taken or its
        use-case already has a running experiment or shadow."""
        ...

    def stop_experiment(self, experiment_id: str) -> bool:
        """Mark a running experiment stopped; ``False`` if it was not running.
        Stopped rows are kept as evidence, never deleted."""
        ...

    def adopt_experiment(self, experiment_id: str, route: Route) -> bool:
        """Atomically stop the experiment and install ``route`` for its
        use-case. ``False`` for an unknown experiment; ``ValueError`` if the
        route does not match the experiment's candidate."""
        ...

    def set_route(self, route: Route) -> None:
        """Create or replace the persistent route for ``route.use_case_key``.
        A running experiment on that use-case still takes precedence."""
        ...

    def clear_route(self, use_case_key: str) -> bool:
        """Delete the persistent route for a use-case; ``False`` if none."""
        ...

    def set_fallback(self, fallback: ApprovedFallback) -> None:
        """Create or replace a use-case's approved budget fallback."""
        ...

    def clear_fallback(self, use_case_key: str) -> bool:
        """Delete a use-case's approved fallback; ``False`` if none."""
        ...

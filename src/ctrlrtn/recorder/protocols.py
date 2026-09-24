"""Whole-store contracts, composed from the capability protocols.

These are the broad "could something other than SQLite back the router?"
interfaces. They are defined by extending ``repositories.py`` rather than
restating its methods, so the narrow contracts remain the single source of
truth and the two modules cannot drift apart.

Prefer the narrowest protocol a caller can satisfy: a component that only
reads serving policy should ask for ``ServingRepository``, not for
``ExperimentStore`` and its seven mutators.
"""

from __future__ import annotations

from typing import Protocol

from ctrlrtn.recorder.models import (
    SessionSummary,
    TaskSummary,
    UseCaseRanking,
)
from ctrlrtn.recorder.repositories import (
    ExperimentRepository,
    ShadowRepository,
    TraceRepository,
)


class TraceStore(TraceRepository, Protocol):
    """Durable trace writes plus the aggregate reads budget and CLI need."""

    def rankings(
        self, *, baseline_only: bool = False
    ) -> list[UseCaseRanking]: ...

    def use_case_models(self) -> dict[str, str | None]: ...

    def tasks(self, limit: int = 50) -> list[TaskSummary]: ...

    def sessions(self, limit: int = 50) -> list[SessionSummary]: ...

    def spend_since(self, ts: float) -> float: ...

    def spend_breakdown_since(
        self, ts: float
    ) -> tuple[float, dict[str, float]]: ...

    def session_spend_state(self) -> tuple[dict[str, float], set[str]]: ...

    def unpriced_calls_since(self, ts: float) -> int: ...

    def terminal_counts_since(self, ts: float) -> dict[str, int]: ...

    def fallback_calls_since(self, ts: float) -> int: ...


class ExperimentStore(ExperimentRepository, ShadowRepository, Protocol):
    """The live A/B and persistent-route control surface.

    The union of control-plane mutations and the shadow counters — every
    method is inherited, so this class adds breadth, never a new contract.
    """

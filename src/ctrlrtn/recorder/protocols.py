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

    def rankings(self, *, baseline_only: bool = False) -> list[UseCaseRanking]:
        """Calls, tokens, latency and spend per use-case over all recorded
        history; ``baseline_only`` leaves candidate-arm calls out."""
        ...

    def use_case_models(self) -> dict[str, str]:
        """The model most recently seen per use-case key (from its latest
        call); use-cases whose model could not be extracted are omitted.
        Keyed like ``rankings``."""
        ...

    def tasks(self, limit: int = 50) -> list[TaskSummary]:
        """The ``limit`` costliest tasks, each with its latest reported
        outcome attached."""
        ...

    def sessions(self, limit: int = 50) -> list[SessionSummary]:
        """The ``limit`` costliest operator sessions, with how many of their
        calls had no known cost."""
        ...

    def spend_since(self, ts: float) -> float:
        """Total known USD cost of calls recorded at or after ``ts`` (Unix
        time); unpriced calls contribute nothing."""
        ...

    def spend_breakdown_since(
        self, ts: float
    ) -> tuple[float, dict[str, float]]:
        """``(total, by_use_case)`` known USD cost since ``ts``. Seeds the
        budget gate's daily snapshot at startup; never read per request."""
        ...

    def session_spend_state(self) -> tuple[dict[str, float], set[str]]:
        """Lifetime known cost per session id, plus the sessions that had a
        billable call with no known cost. Seeds the budget gate's session
        state at startup; never read per request."""
        ...

    def unpriced_calls_since(self, ts: float) -> int:
        """Calls since ``ts`` that reached a paid provider on a model with no
        price, so their cost is unknown to spend totals and the budget."""
        ...

    def terminal_counts_since(self, ts: float) -> dict[str, int]:
        """Router-terminated calls since ``ts`` counted by ``terminal_reason``
        (for example budget rejections, divergence ceilings and provider
        mismatches)."""
        ...

    def fallback_calls_since(self, ts: float) -> int:
        """Calls since ``ts`` that the budget rewrote to an approved fallback
        and the provider then served (terminated calls excluded)."""
        ...


class ExperimentStore(ExperimentRepository, ShadowRepository, Protocol):
    """The live A/B and persistent-route control surface.

    The union of control-plane mutations and the shadow counters — every
    method is inherited, so this class adds breadth, never a new contract.
    """

"""The small decision contract shared by gateway routing policies."""

from __future__ import annotations

from typing import Protocol, runtime_checkable


@runtime_checkable
class ServingDecision(Protocol):
    """Fields the proxy needs to record and prioritize a serving decision.

    Every member is read-only: the concrete decisions are frozen dataclasses,
    and each may narrow a member's type (a route has no experiment, so its
    ``experiment_id`` is always ``None``; an A/B arm's is always a ``str``).
    """

    @property
    def experiment_id(self) -> str | None: ...

    @property
    def arm(self) -> str | None: ...

    @property
    def served_model(self) -> str: ...

    @property
    def provider(self) -> str | None: ...

    @property
    def is_candidate(self) -> bool: ...

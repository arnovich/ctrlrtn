"""The small decision contract shared by gateway routing policies."""

from __future__ import annotations

from typing import Protocol, runtime_checkable


@runtime_checkable
class ServingDecision(Protocol):
    """Fields the proxy needs to record and prioritize a serving decision."""

    experiment_id: str | None
    arm: str | None
    served_model: str
    provider: str | None

    @property
    def is_candidate(self) -> bool: ...

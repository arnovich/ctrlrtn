"""Small in-memory store used by tests and local experiments."""

from __future__ import annotations

from .memory import (
    ControlMemoryMixin,
    MemoryStoreCore,
    ReportingMemoryMixin,
    WorkflowMemoryMixin,
)


class InMemoryTraceStore(
    WorkflowMemoryMixin,
    ReportingMemoryMixin,
    ControlMemoryMixin,
    MemoryStoreCore,
):
    """Test/development store composed from repository capabilities."""


__all__ = ["InMemoryTraceStore"]

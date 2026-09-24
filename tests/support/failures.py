"""Deterministic failure adapters shared by integration tests."""

from __future__ import annotations

from ctrlrtn.recorder.memory_store import InMemoryTraceStore


class FailFirstTraceStore(InMemoryTraceStore):
    """Raise on the first trace write, then behave normally."""

    def __init__(self) -> None:
        super().__init__()
        self.save_attempts = 0

    async def save(self, trace) -> None:
        self.save_attempts += 1
        if self.save_attempts == 1:
            raise OSError("injected trace persistence failure")
        await super().save(trace)

"""Architecture guards for the composed in-memory store."""

from ctrlrtn.recorder.memory import (
    ControlMemoryMixin,
    MemoryStoreCore,
    ReportingMemoryMixin,
    WorkflowMemoryMixin,
)
from ctrlrtn.recorder.memory_store import InMemoryTraceStore


def test_memory_store_is_a_capability_facade():
    assert InMemoryTraceStore.__bases__ == (
        WorkflowMemoryMixin,
        ReportingMemoryMixin,
        ControlMemoryMixin,
        MemoryStoreCore,
    )
    assert not {
        name
        for name, value in InMemoryTraceStore.__dict__.items()
        if callable(value) and not name.startswith("__")
    }


def test_memory_capabilities_own_their_public_operations():
    assert InMemoryTraceStore.save is MemoryStoreCore.save
    assert (
        InMemoryTraceStore.workflow_events
        is WorkflowMemoryMixin.workflow_events
    )
    assert InMemoryTraceStore.rankings is ReportingMemoryMixin.rankings
    assert (
        InMemoryTraceStore.create_experiment
        is ControlMemoryMixin.create_experiment
    )

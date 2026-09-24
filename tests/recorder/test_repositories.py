"""Every adapter must satisfy the contracts its callers depend on.

Without this, the in-memory and SQLite stores drift silently: `Protocol` is
structural, so nothing checks conformance at import time, and a caller typed
against a protocol only fails when the missing method is finally called at
runtime. That is exactly what happened -- InMemoryTraceStore was missing
`save_tool_operation_event` while `TraceStore` declared it.
"""

from __future__ import annotations

import pytest

from ctrlrtn.recorder.memory_store import InMemoryTraceStore
from ctrlrtn.recorder.repositories import (
    ExperimentRepository,
    ReportingRepository,
    ServingRepository,
    ShadowRepository,
    TraceRepository,
)
from ctrlrtn.recorder.sqlite.store import SqliteTraceStore

# Contracts every adapter must satisfy, whatever its backing.
UNIVERSAL = [
    TraceRepository,
    ServingRepository,
    ShadowRepository,
    ExperimentRepository,
]


@pytest.mark.parametrize("contract", UNIVERSAL, ids=lambda c: c.__name__)
def test_sqlite_store_satisfies(contract, tmp_path):
    store = SqliteTraceStore(str(tmp_path / "router.db"))
    try:
        assert isinstance(store, contract)
    finally:
        store.close()


@pytest.mark.parametrize("contract", UNIVERSAL, ids=lambda c: c.__name__)
def test_memory_store_satisfies(contract):
    assert isinstance(InMemoryTraceStore(), contract)


def test_sqlite_store_satisfies_reporting(tmp_path):
    store = SqliteTraceStore(str(tmp_path / "router.db"))
    try:
        assert isinstance(store, ReportingRepository)
    finally:
        store.close()


def test_memory_store_does_not_claim_reporting():
    """Campaign reporting is a SQLite-only capability.

    Asserted rather than ignored so the gap is a stated boundary: if the
    in-memory store ever grows `experiment_task_rows`, this test fails and
    someone decides deliberately whether it is now a reporting adapter.
    """
    assert not isinstance(InMemoryTraceStore(), ReportingRepository)


def test_serving_contract_exposes_no_mutators():
    """Dependency rule 1: the hot path performs no control-plane writes.

    ExperimentRouter is typed against ServingRepository, so a future edit that
    calls set_route() from the refresh path is a type error rather than a
    runtime failure against a read-only connection.
    """
    forbidden = {
        "create_experiment",
        "stop_experiment",
        "set_route",
        "clear_route",
        "set_fallback",
        "clear_fallback",
        "increment_shadow_stats",
    }
    assert forbidden.isdisjoint(ServingRepository.__protocol_attrs__)


def test_experiment_contract_extends_serving():
    """The control plane can read everything the hot path can."""
    assert ServingRepository.__protocol_attrs__ <= (
        ExperimentRepository.__protocol_attrs__
    )

"""M3 foundation: x-ctrlrtn-task capture and per-task cost grouping."""

from __future__ import annotations

import pytest

from ctrlrtn.recorder.store import InMemoryTraceStore, SqliteTraceStore
from ctrlrtn.recorder.trace import Trace
from ctrlrtn.telemetry.enrich import enrich_trace


def _trace(task_id, use_case, cost, status=200) -> Trace:
    return Trace(
        method="POST",
        path="/v1/messages",
        query="",
        request_headers={},
        request_body=b"{}",
        status_code=status,
        response_headers={},
        response_body=b"{}",
        latency_ms=1.0,
        model="claude-sonnet-4-5",
        input_tokens=100,
        output_tokens=10,
        cost_usd=cost,
        use_case_key=use_case,
        task_id=task_id,
    )


def test_enrich_captures_task_id_from_header():
    trace = Trace(
        method="POST",
        path="/v1/messages",
        query="",
        request_headers={"x-ctrlrtn-task": "edition-7"},
        request_body=b'{"model":"claude-sonnet-4-5","system":"X"}',
        status_code=200,
        response_headers={},
        response_body=b"{}",
        latency_ms=1.0,
    )
    enrich_trace(trace)
    assert trace.task_id == "edition-7"


def test_enrich_task_id_is_none_when_header_absent():
    trace = Trace(
        method="POST",
        path="/v1/messages",
        query="",
        request_headers={"content-type": "application/json"},
        request_body=b'{"model":"claude-sonnet-4-5","system":"X"}',
        status_code=200,
        response_headers={},
        response_body=b"{}",
        latency_ms=1.0,
    )
    enrich_trace(trace)
    assert trace.task_id is None


def test_enrich_normalizes_empty_task_header_to_none():
    # An empty header value must bucket as "(untasked)" identically in both
    # stores; normalizing to None at the source guarantees that parity.
    trace = Trace(
        method="POST",
        path="/v1/messages",
        query="",
        request_headers={"x-ctrlrtn-task": ""},
        request_body=b'{"model":"claude-sonnet-4-5","system":"X"}',
        status_code=200,
        response_headers={},
        response_body=b"{}",
        latency_ms=1.0,
    )
    enrich_trace(trace)
    assert trace.task_id is None


@pytest.mark.parametrize(
    "make_store",
    [InMemoryTraceStore, lambda: SqliteTraceStore(":memory:")],
)
async def test_tasks_group_by_task_and_count_distinct_use_cases(make_store):
    store = make_store()
    try:
        # One task spanning two agent use-cases, plus an untagged call.
        await store.save(_trace("edition-7", "fp:orchestrator", 0.80))
        await store.save(_trace("edition-7", "fp:analyst", 0.10))
        await store.save(_trace(None, "fp:orchestrator", 0.05))

        rows = store.tasks()
        by_id = {r.task_id: r for r in rows}
        assert by_id["edition-7"].calls == 2
        assert by_id["edition-7"].use_cases == 2
        assert abs(by_id["edition-7"].cost_usd - 0.90) < 1e-9
        assert "(untasked)" in by_id
        assert rows[0].task_id == "edition-7"  # ordered by cost
    finally:
        if isinstance(store, SqliteTraceStore):
            store.close()


@pytest.mark.parametrize(
    "make_store",
    [InMemoryTraceStore, lambda: SqliteTraceStore(":memory:")],
)
async def test_tasks_count_non_2xx_as_errors(make_store):
    store = make_store()
    try:
        await store.save(_trace("edition-7", "fp:x", 0.10, status=200))
        await store.save(_trace("edition-7", "fp:x", 0.00, status=500))
        await store.save(_trace("edition-7", "fp:x", 0.00, status=429))
        row = {r.task_id: r for r in store.tasks()}["edition-7"]
        assert row.calls == 3
        assert row.errors == 2  # the 500 and the 429, not the 200
    finally:
        if isinstance(store, SqliteTraceStore):
            store.close()


@pytest.mark.parametrize(
    "make_store",
    [InMemoryTraceStore, lambda: SqliteTraceStore(":memory:")],
)
async def test_tasks_respects_limit(make_store):
    store = make_store()
    try:
        for i, cost in enumerate([0.30, 0.20, 0.10]):
            await store.save(_trace(f"edition-{i}", "fp:x", cost))
        rows = store.tasks(limit=2)
        assert [r.task_id for r in rows] == ["edition-0", "edition-1"]
    finally:
        if isinstance(store, SqliteTraceStore):
            store.close()


async def test_task_id_persists_through_get():
    store = SqliteTraceStore(":memory:")
    try:
        await store.save(_trace("edition-7", "fp:x", 0.1))
        assert store.get(1)["task_id"] == "edition-7"
    finally:
        store.close()

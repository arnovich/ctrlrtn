"""M0: recording captures traces without blocking or altering the response."""

from __future__ import annotations

import asyncio
import sqlite3
import time

import httpx
import pytest

from ctrlrtn.config import Settings
from ctrlrtn.gateway.app import create_app
from ctrlrtn.jobs import Job
from ctrlrtn.recorder.recorder import Recorder
from ctrlrtn.recorder.store import InMemoryTraceStore, SqliteTraceStore
from ctrlrtn.recorder.trace import Trace
from ctrlrtn.workflow.identity import WorkflowIdentity
from ctrlrtn.workflow.tool_operation import (
    ToolOperationEvent,
    ToolOperationIdentity,
)

_EXPECTED_BODY = "data: a\n\ndata: b\n\ndata: [DONE]\n\n"


async def _post(app) -> httpx.Response:
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://router",
    ) as client:
        return await client.post(
            "/v1/chat/completions",
            content=b'{"model":"gpt-4o"}',
            headers={
                "authorization": "Bearer sk-test",
                "content-type": "application/json",
            },
        )


async def test_recording_persists_trace_without_altering_response(
    streaming_upstream,
):
    store = InMemoryTraceStore()
    recorder = Recorder(store)
    recorder.start()

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=streaming_upstream({})),
        base_url="http://upstream",
    ) as upstream_client:
        app = create_app(
            Settings(upstream_base_url="http://upstream"),
            upstream_client=upstream_client,
            recorder=recorder,
        )
        resp = await _post(app)
        await recorder.join()
    await recorder.aclose()

    # Response is byte-identical to plain pass-through.
    assert resp.status_code == 200
    assert resp.text == _EXPECTED_BODY

    # The trace was captured with the full request and response.
    assert len(store.traces) == 1
    trace = store.traces[0]
    assert trace.method == "POST"
    assert trace.path == "/v1/chat/completions"
    assert trace.request_body == b'{"model":"gpt-4o"}'
    assert trace.status_code == 200
    assert trace.response_body == _EXPECTED_BODY.encode()
    assert trace.latency_ms >= 0


async def test_response_completes_even_if_store_blocks(streaming_upstream):
    # A store whose save hangs forever must not stall the response: persistence
    # runs in the worker, never in the request path.
    never = asyncio.Event()

    class BlockingStore:
        async def save(self, trace: Trace) -> None:
            await never.wait()

    recorder = Recorder(BlockingStore())
    recorder.start()

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=streaming_upstream({})),
        base_url="http://upstream",
    ) as upstream_client:
        app = create_app(
            Settings(upstream_base_url="http://upstream"),
            upstream_client=upstream_client,
            recorder=recorder,
        )
        # If the response path awaited the store, this would time out.
        resp = await asyncio.wait_for(_post(app), timeout=2.0)
    await recorder.aclose()

    assert resp.status_code == 200
    assert resp.text == _EXPECTED_BODY


async def test_sqlite_store_roundtrip():
    store = SqliteTraceStore(":memory:")
    try:
        await store.save(
            Trace(
                method="POST",
                path="/v1/chat/completions",
                query="",
                request_headers={},
                request_body=b"{}",
                status_code=200,
                response_headers={},
                response_body=b"ok",
                latency_ms=1.0,
            )
        )
        assert store.count() == 1
    finally:
        store.close()


def _retention_trace(ts: float, marker: bytes = b"secret") -> Trace:
    return Trace(
        method="POST",
        path="/v1/chat/completions",
        query="token=secret",
        request_headers={"x-context": "secret"},
        request_body=marker,
        status_code=200,
        response_headers={"x-result": "secret"},
        response_body=marker,
        latency_ms=1.0,
        ts=ts,
        cost_usd=0.25,
        task_id="task:retained-metric",
    )


def test_prune_trace_payloads_is_dry_run_then_preserves_derived_metrics():
    store = SqliteTraceStore(":memory:")
    try:
        store._insert(_retention_trace(10.0))
        store._insert(_retention_trace(30.0, b"new"))

        planned = store.prune_trace_payloads(20.0)
        assert planned.applied is False
        assert planned.pruned_traces == 1
        assert planned.payload_bytes > 0
        assert store.get(1)["request_body"] == b"secret"

        applied = store.prune_trace_payloads(20.0, apply=True)
        assert applied.applied is True
        assert applied.pruned_traces == 1
        old = store.get(1)
        assert old["query"] == ""
        assert old["request_headers"] == {}
        assert old["request_body"] is None
        assert old["response_headers"] == {}
        assert old["response_body"] is None
        assert old["cost_usd"] == 0.25
        assert old["task_id"] == "task:retained-metric"
        assert store.get(2)["request_body"] == b"new"
        assert store.prune_trace_payloads(20.0).pruned_traces == 0
    finally:
        store.close()


async def test_gateway_startup_applies_opt_in_retention_before_serving():
    store = SqliteTraceStore(":memory:")
    now = time.time()
    store._insert(_retention_trace(now - (2 * 86400)))
    store._insert(_retention_trace(now, b"new"))
    app = create_app(
        Settings(
            upstream_base_url="http://upstream",
            retention_days=1,
        ),
        recorder=Recorder(store),
        store=store,
    )
    try:
        async with app.router.lifespan_context(app):
            assert store.get(1)["request_body"] is None
            assert store.get(2)["request_body"] == b"new"
    finally:
        store.close()


async def test_gateway_refuses_unavailable_automatic_retention_policy():
    store = InMemoryTraceStore()
    app = create_app(
        Settings(
            upstream_base_url="http://upstream",
            retention_days=1,
        ),
        recorder=Recorder(store),
        store=store,
    )

    with pytest.raises(
        ValueError, match="automatic retention requires a pruning store"
    ):
        async with app.router.lifespan_context(app):
            pass


def test_prune_invalidates_inference_derived_from_erased_payloads():
    store = SqliteTraceStore(":memory:")
    try:
        store._insert(_retention_trace(10.0))
        store._insert(_retention_trace(30.0, b"new"))
        store._conn.execute(
            """INSERT INTO inferred_workflow_edges VALUES
               (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                "edge-1",
                40.0,
                "exact-tool-id-v1",
                "task:retained-metric",
                "pipeline",
                "v1",
                "one",
                "two",
                1,
                2,
                "a" * 64,
                1.0,
                "unverified",
            ),
        )
        store._conn.commit()

        planned = store.prune_trace_payloads(20.0)
        assert planned.invalidated_inferred_edges == 1
        assert len(store.inferred_workflow_edges()) == 1

        applied = store.prune_trace_payloads(20.0, apply=True)
        assert applied.invalidated_inferred_edges == 1
        assert store.inferred_workflow_edges() == []
    finally:
        store.close()


def test_workflow_task_erasure_is_atomic_recomputable_and_job_protected():
    store = SqliteTraceStore(":memory:")
    try:
        trace = _retention_trace(10.0)
        trace.workflow = "pipeline"
        trace.workflow_version = "v1"
        trace.step = "research"
        trace.step_run_id = "run-1"
        trace.step_attempt = 1
        store._insert(trace)
        store._conn.execute(
            "INSERT INTO workflow_events VALUES "
            "(NULL, ?, ?, ?, ?, ?, ?, ?, NULL, '[]', 1, 'completed', 1, NULL, NULL)",
            (
                "event-1",
                10.0,
                trace.task_id,
                "pipeline",
                "v1",
                "research",
                "run-1",
            ),
        )
        store._conn.execute(
            "INSERT INTO outcomes (ts, task_id, success, score) VALUES (?, ?, ?, ?)",
            (10.0, trace.task_id, 1, 1.0),
        )
        store._conn.commit()
        identity = WorkflowIdentity(
            trace.task_id, "pipeline", "v1", "research", "run-1"
        )
        store._insert_tool_operation_event(
            ToolOperationEvent(
                ToolOperationIdentity(
                    identity,
                    "search",
                    "operation-1",
                    "attempt-1",
                    effect="pure",
                ),
                "completed",
                event_id="tool-event-1",
                success=True,
                ts=10.0,
            )
        )
        store._conn.execute(
            """INSERT INTO inferred_workflow_edges VALUES
               (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                "edge-task-1",
                10.0,
                "exact-tool-id-v1",
                trace.task_id,
                "pipeline",
                "v1",
                "run-1",
                "run-2",
                1,
                1,
                "b" * 64,
                1.0,
                "unverified",
            ),
        )
        store._conn.commit()

        planned = store.erase_workflow_task(trace.task_id)
        assert (
            planned.traces,
            planned.workflow_events,
            planned.tool_operation_events,
            planned.inferred_edges,
            planned.outcomes,
        ) == (
            1,
            1,
            1,
            1,
            1,
        )
        assert store.workflow_graph(trace.task_id) is not None

        store.create_job(Job(kind="replay_eval", config={"trace_ids": [1]}))
        with pytest.raises(ValueError, match="protected by an active job"):
            store.erase_workflow_task(trace.task_id, apply=True)
        assert store.count() == 1

        store._conn.execute("UPDATE jobs SET status = 'cancelled'")
        store._conn.commit()
        erased = store.erase_workflow_task(trace.task_id, apply=True)
        assert erased.applied is True and erased.traces == 1
        assert store.workflow_graph(trace.task_id) is None
        assert store.workflow_events(trace.task_id) == []
        assert store.tool_operation_events(trace.task_id) == []
        assert store.inferred_workflow_edges() == []
    finally:
        store.close()


def test_workflow_task_erasure_cli_defaults_to_dry_run(
    tmp_path, monkeypatch, capsys
):
    from ctrlrtn.cli.commands import main

    path = str(tmp_path / "erasure.db")
    monkeypatch.setenv("CTRLRTN_DB", path)
    store = SqliteTraceStore(path)
    store._insert(_retention_trace(10.0))
    store.close()

    main(["workflow", "erase-task", "task:retained-metric"])
    assert "Would erase task" in capsys.readouterr().out
    store = SqliteTraceStore(path)
    assert store.count() == 1
    store.close()

    main(["workflow", "erase-task", "task:retained-metric", "--apply"])
    assert "Erased task" in capsys.readouterr().out
    store = SqliteTraceStore(path)
    assert store.count() == 0
    store.close()


def test_prune_protects_active_job_samples_and_replay_rejects_pruned_payload():
    store = SqliteTraceStore(":memory:")
    try:
        store._insert(_retention_trace(10.0, b"protected"))
        store._insert(_retention_trace(10.0, b"pruned"))
        store.create_job(Job(kind="replay_eval", config={"trace_ids": [1]}))

        result = store.prune_trace_payloads(20.0, apply=True)
        assert result.eligible_traces == 2
        assert result.protected_traces == 1
        assert result.pruned_traces == 1
        assert store.requests_by_ids([1])[0]["request_body"] == b"protected"
        with pytest.raises(ValueError, match="payload was pruned"):
            store.requests_by_ids([2])
    finally:
        store.close()


def test_prune_cli_defaults_to_dry_run(tmp_path, monkeypatch, capsys):
    from ctrlrtn.cli.commands import main

    path = str(tmp_path / "retention.db")
    monkeypatch.setenv("CTRLRTN_DB", path)
    store = SqliteTraceStore(path)
    store._insert(_retention_trace(1.0))
    store.close()

    main(["prune", "--older-than-days", "1"])
    output = capsys.readouterr().out
    assert "Would prune 1 trace payload" in output
    assert "Dry run only" in output
    store = SqliteTraceStore(path)
    assert store.get(1)["request_body"] == b"secret"
    store.close()

    main(["prune", "--older-than-days", "1", "--apply"])
    assert "Pruned 1 trace payload" in capsys.readouterr().out
    store = SqliteTraceStore(path)
    assert store.get(1)["request_body"] is None
    store.close()


def test_compaction_requires_exclusive_router_maintenance(tmp_path):
    path = str(tmp_path / "compact.db")
    writer = SqliteTraceStore(path)
    try:
        with pytest.raises(sqlite3.OperationalError, match="writer is active"):
            SqliteTraceStore(path, maintenance=True)
    finally:
        writer.close()

    maintenance = SqliteTraceStore(path, maintenance=True)
    try:
        with pytest.raises(
            sqlite3.OperationalError, match="maintenance is active"
        ):
            SqliteTraceStore(path)
    finally:
        maintenance.close()


def test_compaction_reclaims_pages_after_payload_prune(tmp_path):
    path = str(tmp_path / "compact.db")
    store = SqliteTraceStore(path)
    payload = b"sensitive" * 4096
    for _ in range(40):
        store._insert(_retention_trace(1.0, payload))
    store.close()

    store = SqliteTraceStore(path, maintenance=True)
    try:
        assert store.prune_trace_payloads(2.0, apply=True).pruned_traces == 40
        compacted = store.compact()
        assert compacted.pages_after < compacted.pages_before
        assert compacted.bytes_after < compacted.bytes_before
        assert store.count() == 40
    finally:
        store.close()


def test_prune_cli_compact_requires_apply(capsys):
    from ctrlrtn.cli.commands import main

    with pytest.raises(SystemExit, match="2"):
        main(["prune", "--older-than-days", "1", "--compact"])
    assert "--compact requires --apply" in capsys.readouterr().err


async def test_spend_since_sums_known_costs_only():
    store = SqliteTraceStore(":memory:")
    try:
        for ts, cost, use_case, model in (
            (10.0, 0.2, "tag:x", "gpt-4o"),
            (20.0, None, "tag:x", "unknown-model"),
            (30.0, 0.3, None, "gpt-4o"),
        ):
            await store.save(
                Trace(
                    method="POST",
                    path="/v1/chat/completions",
                    query="",
                    request_headers={},
                    request_body=b"{}",
                    status_code=200,
                    response_headers={},
                    response_body=b"",
                    latency_ms=1.0,
                    ts=ts,
                    cost_usd=cost,
                    use_case_key=use_case,
                    model=model,
                )
            )
        assert store.spend_since(0.0) == 0.5
        assert store.spend_since(20.0) == 0.3
        assert store.spend_breakdown_since(0.0) == (0.5, {"tag:x": 0.2})
        assert store.unpriced_calls_since(0.0) == 1
    finally:
        store.close()


@pytest.mark.parametrize(
    "make_store",
    [InMemoryTraceStore, lambda: SqliteTraceStore(":memory:")],
)
async def test_terminal_counts_since_groups_only_router_terminals(make_store):
    store = make_store()
    try:
        for ts, reason in (
            (10.0, "budget"),
            (20.0, "budget"),
            (30.0, "session_required"),
            (40.0, None),
        ):
            await store.save(
                Trace(
                    method="POST",
                    path="/v1/chat/completions",
                    query="",
                    request_headers={},
                    request_body=b"{}",
                    status_code=429 if reason else 200,
                    response_headers={},
                    response_body=b"",
                    latency_ms=1.0,
                    ts=ts,
                    terminal_reason=reason,
                )
            )

        assert store.terminal_counts_since(20.0) == {
            "budget": 1,
            "session_required": 1,
        }
    finally:
        if isinstance(store, SqliteTraceStore):
            store.close()


async def test_sqlite_persists_serve_decision_and_reenrich_preserves_it():
    store = SqliteTraceStore(":memory:")
    try:
        await store.save(
            Trace(
                method="POST",
                path="/v1/messages",
                query="",
                request_headers={},
                request_body=b'{"model": "claude-opus-4"}',
                status_code=200,
                response_headers={},
                response_body=b'{"model": "claude-haiku-4-5"}',
                latency_ms=1.0,
                provider="anthropic",
                provider_free=True,
                experiment_id="exp:1",
                arm="candidate",
                served_model="claude-haiku-4-5",
                route_rule_scope="workflow_step",
                route_rule_key="pipeline@v1/draft",
                control_revision="a" * 40,
            )
        )
        row = store.get(1)
        assert row["experiment_id"] == "exp:1"
        assert row["arm"] == "candidate"
        assert row["served_model"] == "claude-haiku-4-5"
        assert row["provider"] == "anthropic"
        assert row["provider_free"] is True
        assert row["route_rule_scope"] == "workflow_step"
        assert row["route_rule_key"] == "pipeline@v1/draft"
        assert row["control_revision"] == "a" * 40

        # Re-enrichment that actually REWRITES the derived columns must leave the
        # serving facts (arm/served_model/experiment_id) untouched — proving the
        # two column-sets are genuinely disjoint, not that a no-op is a no-op.
        def enrich(trace: Trace) -> None:
            trace.model = "claude-opus-4"
            trace.use_case_key = "fp:editor"

        store.reenrich(enrich)
        after = store.get(1)
        assert after["model"] == "claude-opus-4"  # derived column changed
        assert after["use_case_key"] == "fp:editor"
        assert after["arm"] == "candidate"  # serving facts preserved
        assert after["served_model"] == "claude-haiku-4-5"
        assert after["experiment_id"] == "exp:1"
        assert after["provider"] == "anthropic"
        assert after["provider_free"] is True
        assert after["route_rule_scope"] == "workflow_step"
        assert after["control_revision"] == "a" * 40
    finally:
        store.close()

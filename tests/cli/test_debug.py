"""M0.5: debug/inspection — recent calls, single-call detail, live hook."""

from __future__ import annotations

import gzip
import json

from ctrlrtn.analysis.report import render_trace
from ctrlrtn.cli.render import render_calls
from ctrlrtn.recorder.recorder import Recorder
from ctrlrtn.recorder.store import InMemoryTraceStore, SqliteTraceStore
from ctrlrtn.recorder.trace import Trace
from ctrlrtn.telemetry.enrich import enrich_trace


def _trace() -> Trace:
    return Trace(
        method="POST",
        path="/v1/messages",
        query="",
        request_headers={
            "x-api-key": "sk-secret-0123456789",
            "content-type": "application/json",
        },
        request_body=b'{"model":"claude","system":"You are X."}',
        status_code=200,
        response_headers={"content-type": "application/json"},
        response_body=b'{"usage":{"input_tokens":5,"output_tokens":2}}',
        latency_ms=12.0,
        provider="anthropic",
        model="claude",
        input_tokens=5,
        output_tokens=2,
        use_case_key="fp:abc123",
    )


async def test_recent_and_get_roundtrip():
    store = SqliteTraceStore(":memory:")
    try:
        await store.save(_trace())
        recent = store.recent(10)
        assert len(recent) == 1
        assert recent[0]["use_case_key"] == "fp:abc123"
        assert recent[0]["model"] == "claude"
        assert recent[0]["provider"] == "anthropic"

        full = store.get(recent[0]["id"])
        assert full["status_code"] == 200
        assert (
            full["request_body"] == b'{"model":"claude","system":"You are X."}'
        )
        assert full["request_headers"]["content-type"] == "application/json"
        assert full["provider"] == "anthropic"
        assert store.get(99999) is None
    finally:
        store.close()


async def test_recent_pages_back_through_the_feed_by_offset():
    store = SqliteTraceStore(":memory:")
    try:
        for _ in range(5):
            await store.save(_trace())
        ids = [row["id"] for row in store.recent(10)]
        assert ids == sorted(ids, reverse=True)  # newest first
        assert [row["id"] for row in store.recent(2)] == ids[:2]
        assert [row["id"] for row in store.recent(2, offset=2)] == ids[2:4]
        assert store.recent(2, offset=99) == []
    finally:
        store.close()


def test_show_masks_secret_and_pretty_prints_body():
    store_row = {
        "id": 1,
        "ts": 0.0,
        "method": "POST",
        "path": "/v1/messages",
        "query": "",
        "status_code": 200,
        "latency_ms": 12.0,
        "model": "claude",
        "input_tokens": 5,
        "output_tokens": 2,
        "cache_read_tokens": None,
        "cache_write_tokens": None,
        "cost_usd": 0.0012,
        "use_case_key": "fp:abc123",
        "request_headers": {
            "x-api-key": "sk-secret-0123456789",
            "content-type": "application/json",
        },
        "request_body": b'{"system":"hi"}',
        "response_headers": {},
        "response_body": b'{"ok":true}',
        "task_id": "task-1",
        "served_model": None,
        "provider": "anthropic",
    }
    out = render_trace(store_row)
    assert "sk-secret-0123456789" not in out  # the key is masked
    assert "fp:abc123" in out
    assert '"system": "hi"' in out  # body pretty-printed
    assert "task     = task-1" in out
    assert "served_model = -" in out  # None renders "-", not the literal None
    assert "provider = anthropic" in out
    assert "$0.0012" in out  # cost shown


def test_render_calls_table():
    rows = [
        {
            "id": 1,
            "use_case_key": "fp:abc",
            "model": "claude",
            "provider": "anthropic",
            "status_code": 200,
            "latency_ms": 12.0,
            "input_tokens": 5,
            "output_tokens": 2,
            "cost_usd": 0.0012,
            "method": "POST",
            "path": "/v1/messages",
        }
    ]
    out = render_calls(rows)
    assert "fp:abc" in out and "claude" in out
    assert "anthropic" in out
    assert "$0.0012" in out


async def test_on_record_hook_is_called_after_save():
    seen: list[str | None] = []
    recorder = Recorder(
        InMemoryTraceStore(),
        on_record=lambda t: seen.append(t.use_case_key),
    )
    recorder.start()
    recorder.enqueue(_trace())
    await recorder.join()
    await recorder.aclose()
    assert seen == ["fp:abc123"]


async def test_reenrich_recomputes_derived_columns_from_raw():
    # A row as it would have been stored before the gzip fix: the raw gzipped
    # body is retained, but the derived columns were never populated.
    store = SqliteTraceStore(":memory:")
    try:
        raw = json.dumps(
            {"usage": {"input_tokens": 42, "output_tokens": 8}}
        ).encode()
        await store.save(
            Trace(
                method="POST",
                path="/v1/messages",
                query="",
                request_headers={},
                request_body=b'{"model":"claude","system":"You are X."}',
                status_code=200,
                response_headers={"content-encoding": "gzip"},
                response_body=gzip.compress(raw),
                latency_ms=5.0,
            )
        )
        assert store.get(1)["input_tokens"] is None  # not enriched yet

        assert store.reenrich(enrich_trace) == 1

        after = store.get(1)
        assert after["input_tokens"] == 42
        assert after["output_tokens"] == 8
        assert after["model"] == "claude"
        assert after["use_case_key"].startswith("fp:")
    finally:
        store.close()


async def test_cost_persists_through_store_and_rankings():
    store = SqliteTraceStore(":memory:")
    try:
        await store.save(
            Trace(
                method="POST",
                path="/v1/messages",
                query="",
                request_headers={},
                request_body=b"{}",
                status_code=200,
                response_headers={},
                response_body=b"{}",
                latency_ms=1.0,
                model="claude-sonnet-4-5",
                input_tokens=1_000_000,
                output_tokens=0,
                cost_usd=3.0,
                use_case_key="fp:x",
            )
        )
        assert store.get(1)["cost_usd"] == 3.0
        assert store.recent(1)[0]["cost_usd"] == 3.0
        assert store.rankings()[0].cost_usd == 3.0
    finally:
        store.close()


def test_migration_adds_columns_to_an_old_db(tmp_path):
    import sqlite3

    db = str(tmp_path / "old.db")
    con = sqlite3.connect(db)
    con.execute(  # the M0 schema, before cost/cache columns existed
        "CREATE TABLE traces (id INTEGER PRIMARY KEY AUTOINCREMENT, "
        "ts REAL NOT NULL, method TEXT NOT NULL, path TEXT NOT NULL, "
        "query TEXT NOT NULL, status_code INTEGER NOT NULL, "
        "latency_ms REAL NOT NULL, model TEXT, input_tokens INTEGER, "
        "output_tokens INTEGER, use_case_key TEXT, request_headers TEXT "
        "NOT NULL, request_body BLOB, response_headers TEXT NOT NULL, "
        "response_body BLOB)"
    )
    con.commit()
    con.close()

    store = SqliteTraceStore(db)  # opening it must ALTER TABLE, not crash
    try:
        cols = {r[1] for r in store._conn.execute("PRAGMA table_info(traces)")}
        assert {
            "cache_read_tokens",
            "cache_write_tokens",
            "cost_usd",
            "provider",
            "provider_free",
            "session_id",
        } <= cols
    finally:
        store.close()

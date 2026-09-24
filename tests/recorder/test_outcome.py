"""M3: app-reported task outcomes (POST /ctrlrtn/outcome) joined into the
per-task view — the quality numerator beside the cost denominator."""

from __future__ import annotations

import httpx
import pytest

from ctrlrtn.analysis.report import _fmt_outcome
from ctrlrtn.cli.render import render_tasks
from ctrlrtn.config import Settings
from ctrlrtn.gateway.app import create_app
from ctrlrtn.recorder.recorder import Recorder
from ctrlrtn.recorder.store import (
    InMemoryTraceStore,
    Outcome,
    SqliteTraceStore,
)
from ctrlrtn.recorder.trace import Trace

_STORES = [InMemoryTraceStore, lambda: SqliteTraceStore(":memory:")]


def _trace(task_id, cost=0.1) -> Trace:
    return Trace(
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
        input_tokens=100,
        output_tokens=10,
        cost_usd=cost,
        use_case_key="fp:x",
        task_id=task_id,
    )


def _close(store) -> None:
    if isinstance(store, SqliteTraceStore):
        store.close()


@pytest.mark.parametrize("make_store", _STORES)
async def test_outcome_joins_into_tasks(make_store):
    store = make_store()
    try:
        await store.save(_trace("edition-7"))
        await store.save_outcome(
            Outcome(task_id="edition-7", success=True, score=0.9)
        )
        row = {r.task_id: r for r in store.tasks()}["edition-7"]
        assert row.success is True
        assert row.score == 0.9
    finally:
        _close(store)


@pytest.mark.parametrize("make_store", _STORES)
async def test_latest_outcome_wins(make_store):
    store = make_store()
    try:
        await store.save(_trace("edition-7"))
        await store.save_outcome(
            Outcome(task_id="edition-7", success=False, score=0.2)
        )
        await store.save_outcome(
            Outcome(task_id="edition-7", success=True, score=0.9)
        )
        row = {r.task_id: r for r in store.tasks()}["edition-7"]
        assert row.success is True  # the later-arriving outcome
        assert row.score == 0.9
    finally:
        _close(store)


@pytest.mark.parametrize("make_store", _STORES)
async def test_later_failure_supersedes_earlier_success(make_store):
    # Guards the MAX(o.success) join from degenerating into "ever succeeded":
    # a later failure must win over an earlier success, not be masked by it.
    store = make_store()
    try:
        await store.save(_trace("edition-7"))
        await store.save_outcome(
            Outcome(task_id="edition-7", success=True, score=0.9)
        )
        await store.save_outcome(
            Outcome(task_id="edition-7", success=False, score=0.1)
        )
        row = {r.task_id: r for r in store.tasks()}["edition-7"]
        assert row.success is False
        assert row.score == 0.1
    finally:
        _close(store)


@pytest.mark.parametrize("make_store", _STORES)
async def test_empty_string_task_id_parity(make_store):
    # Not reachable through the API (enrich/endpoint normalize/reject ""), but
    # the two stores must still group a directly-constructed "" identically.
    store = make_store()
    try:
        await store.save(_trace(""))
        await store.save_outcome(Outcome(task_id="", success=True))
        by_id = {r.task_id: r for r in store.tasks()}
        assert "" in by_id  # its own group, not folded into "(untasked)"
        assert by_id[""].success is True
    finally:
        _close(store)


@pytest.mark.parametrize("make_store", _STORES)
async def test_success_false_survives_the_join(make_store):
    # A reported failure must read back as False, not be confused with "no
    # outcome" (None) — the join stores/reads 0 vs NULL distinctly.
    store = make_store()
    try:
        await store.save(_trace("edition-7"))
        await store.save_outcome(Outcome(task_id="edition-7", success=False))
        row = {r.task_id: r for r in store.tasks()}["edition-7"]
        assert row.success is False
        assert row.score is None
    finally:
        _close(store)


@pytest.mark.parametrize("make_store", _STORES)
async def test_task_without_outcome_is_none(make_store):
    store = make_store()
    try:
        await store.save(_trace("edition-7"))
        await store.save(_trace(None))  # untagged
        by_id = {r.task_id: r for r in store.tasks()}
        assert by_id["edition-7"].success is None
        assert by_id["edition-7"].score is None
        assert by_id["(untasked)"].success is None
    finally:
        _close(store)


def _app_with_store(store):
    return create_app(
        Settings(upstream_base_url="http://upstream"),
        recorder=Recorder(store),
    )


def _client(app):
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://localhost"
    )


async def test_endpoint_records_and_joins():
    store = InMemoryTraceStore()
    await store.save(_trace("edition-7"))
    async with _client(_app_with_store(store)) as client:
        resp = await client.post(
            "/ctrlrtn/outcome",
            json={"task_id": "edition-7", "success": True, "score": 0.9},
        )
    assert resp.status_code == 200
    row = {r.task_id: r for r in store.tasks()}["edition-7"]
    assert row.success is True
    assert row.score == 0.9


async def test_endpoint_rejects_bad_input():
    store = InMemoryTraceStore()
    async with _client(_app_with_store(store)) as client:
        # missing task_id
        r1 = await client.post("/ctrlrtn/outcome", json={"success": True})
        # no signal at all
        r2 = await client.post("/ctrlrtn/outcome", json={"task_id": "x"})
        # wrong type
        r3 = await client.post(
            "/ctrlrtn/outcome", json={"task_id": "x", "success": "yes"}
        )
        # not JSON
        r4 = await client.post(
            "/ctrlrtn/outcome",
            content=b"nope",
            headers={"content-type": "application/json"},
        )
    assert [r1.status_code, r2.status_code, r3.status_code, r4.status_code] == [
        400,
        400,
        400,
        400,
    ]
    assert store.outcomes == []  # nothing was persisted


async def test_endpoint_503_when_recording_disabled():
    app = create_app(Settings(upstream_base_url="http://upstream"))
    async with _client(app) as client:
        resp = await client.post(
            "/ctrlrtn/outcome", json={"task_id": "x", "success": True}
        )
    assert resp.status_code == 503


async def test_endpoint_requires_json_content_type():
    # A cross-origin drive-by POST uses text/plain to dodge CORS preflight.
    store = InMemoryTraceStore()
    async with _client(_app_with_store(store)) as client:
        resp = await client.post(
            "/ctrlrtn/outcome",
            content=b'{"task_id":"x","success":true}',
            headers={"content-type": "text/plain"},
        )
    assert resp.status_code == 415
    assert store.outcomes == []


async def test_endpoint_rejects_non_finite_and_huge_score():
    # stdlib json.loads accepts the Infinity/NaN literals by default; send raw
    # bytes since httpx's json= encoder rejects them before they'd reach us.
    json_ct = {"content-type": "application/json"}
    store = InMemoryTraceStore()
    async with _client(_app_with_store(store)) as client:
        r_inf = await client.post(
            "/ctrlrtn/outcome",
            content=b'{"task_id":"x","score":Infinity}',
            headers=json_ct,
        )
        r_nan = await client.post(
            "/ctrlrtn/outcome",
            content=b'{"task_id":"x","score":NaN}',
            headers=json_ct,
        )
        # a huge integer literal: json.loads -> Python int, float() overflows
        r_big = await client.post(
            "/ctrlrtn/outcome",
            content=b'{"task_id":"x","score":' + b"9" * 400 + b"}",
            headers=json_ct,
        )
    assert r_inf.status_code == 400
    assert r_nan.status_code == 400
    assert r_big.status_code == 400
    assert store.outcomes == []


async def test_endpoint_rejects_overlong_task_id():
    store = InMemoryTraceStore()
    async with _client(_app_with_store(store)) as client:
        resp = await client.post(
            "/ctrlrtn/outcome", json={"task_id": "x" * 600, "success": True}
        )
    assert resp.status_code == 400
    assert store.outcomes == []


async def test_ctrlrtn_namespace_is_never_forwarded(streaming_upstream):
    # The dangerous case: single-upstream mode, where the resolver matches
    # EVERY path. Near-misses on the control-plane route must 404 locally,
    # never reach the upstream (which would leak the request + headers).
    record: dict = {}
    upstream = streaming_upstream(record)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=upstream),
        base_url="http://upstream",
    ) as upstream_client:
        app = create_app(
            Settings(upstream_base_url="http://upstream"),
            upstream_client=upstream_client,
            recorder=Recorder(InMemoryTraceStore()),
        )
        async with _client(app) as client:
            r_slash = await client.post(
                "/ctrlrtn/outcome/", json={"task_id": "x", "success": True}
            )
            r_get = await client.get("/ctrlrtn/outcome")
            r_other = await client.post("/ctrlrtn/anything")
    assert r_slash.status_code == 404
    assert r_get.status_code == 404
    assert r_other.status_code == 404
    assert record == {}  # nothing was forwarded upstream


def test_fmt_outcome_variants():
    assert _fmt_outcome(True, 0.9) == "ok 0.90"
    assert _fmt_outcome(True, None) == "ok"
    assert _fmt_outcome(False, None) == "fail"
    assert _fmt_outcome(None, 0.5) == "0.50"
    assert _fmt_outcome(None, None) == "-"


def test_render_tasks_shows_outcome_column():
    from ctrlrtn.recorder.store import TaskSummary

    rows = [
        TaskSummary(
            task_id="edition-7",
            calls=3,
            cost_usd=0.9,
            input_tokens=300,
            output_tokens=20,
            use_cases=2,
            errors=0,
            success=True,
            score=0.9,
        )
    ]
    out = render_tasks(rows)
    assert "outcome" in out
    assert "ok 0.90" in out


# --- the DNS-rebinding Host guard -------------------------------------------


async def test_outcome_rejects_a_rebound_dns_host():
    # A browser page can reach a loopback-bound port via a rebound domain;
    # the rebound Host is a DNS name the operator never configured.
    app = _app_with_store(InMemoryTraceStore())
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://evil.attacker.example",
    ) as client:
        resp = await client.post(
            "/ctrlrtn/outcome", json={"task_id": "t", "success": True}
        )
    assert resp.status_code == 403
    assert "DNS-rebinding" in resp.text


async def test_outcome_accepts_ip_literals_and_configured_names():
    app = create_app(
        Settings(
            upstream_base_url="http://upstream",
            control_hosts=("router.internal",),
        ),
        recorder=Recorder(InMemoryTraceStore()),
    )
    for base in (
        "http://127.0.0.1:4000",
        "http://10.0.0.7",
        "http://[::1]:4000",
        "http://router.internal",
        "http://localhost",
    ):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url=base
        ) as client:
            resp = await client.post(
                "/ctrlrtn/outcome", json={"task_id": "t", "success": True}
            )
        assert resp.status_code == 200, base

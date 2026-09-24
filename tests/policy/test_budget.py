"""Daily budget policy: pure, deterministic admission decisions."""

import asyncio
import time

import httpx
import pytest
from starlette.applications import Starlette
from starlette.responses import JSONResponse
from starlette.routing import Route

from ctrlrtn.cli import commands as cli
from ctrlrtn.cli.commands import build_app
from ctrlrtn.cli.render import render_budget_status
from ctrlrtn.config import Settings
from ctrlrtn.gateway.app import create_app
from ctrlrtn.policy.budget import (
    BudgetGate,
    BudgetPolicy,
    SpendSnapshot,
)
from ctrlrtn.recorder.recorder import Recorder
from ctrlrtn.recorder.sqlite.store import SqliteTraceStore
from ctrlrtn.recorder.store import InMemoryTraceStore
from ctrlrtn.recorder.trace import Trace
from ctrlrtn.telemetry.enrich import enrich_trace


def test_policy_rejects_invalid_direct_construction():
    with pytest.raises(ValueError, match="finite"):
        BudgetPolicy(global_daily_usd=-1)
    with pytest.raises(ValueError, match="use-case"):
        BudgetPolicy(use_case_daily_usd={"": 1})
    with pytest.raises(ValueError, match="finite"):
        BudgetPolicy(session_limit_usd=-1)
    with pytest.raises(ValueError, match="reserve_in_flight"):
        BudgetPolicy(reserve_in_flight=1)
    with pytest.raises(ValueError, match="ceiling"):
        BudgetPolicy(reserve_in_flight=True)


def test_snapshot_tracks_known_cost_by_day_and_use_case():
    snapshot = SpendSnapshot()
    trace = Trace(
        method="POST",
        path="/",
        query="",
        request_headers={},
        request_body=b"",
        status_code=200,
        response_headers={},
        response_body=b"",
        latency_ms=0,
        ts=0,
        cost_usd=0.2,
        use_case_key="tag:editor",
    )
    snapshot.record(trace, now=0)
    assert snapshot.total == 0.2
    assert snapshot.by_use_case == {"tag:editor": 0.2}


def test_snapshot_ignores_a_delayed_trace_from_yesterday():
    snapshot = SpendSnapshot()
    snapshot.seed(1.0, {"tag:editor": 1.0}, now=86_400)
    trace = Trace(
        method="POST",
        path="/",
        query="",
        request_headers={},
        request_body=b"",
        status_code=200,
        response_headers={},
        response_body=b"",
        latency_ms=0,
        ts=0,
        cost_usd=5.0,
        use_case_key="tag:editor",
    )
    snapshot.record(trace, now=86_400)
    assert snapshot.total == 1.0


def test_snapshot_keeps_lifetime_session_spend_across_daily_rollover():
    snapshot = SpendSnapshot()
    snapshot.seed_sessions({"session-7": 0.5})
    trace = Trace(
        method="POST",
        path="/",
        query="",
        request_headers={},
        request_body=b"",
        status_code=200,
        response_headers={},
        response_body=b"",
        latency_ms=0,
        ts=0,
        cost_usd=0.2,
        session_id="session-7",
    )

    snapshot.record(trace, now=86_400)

    assert snapshot.session_total("session-7") == 0.7
    assert snapshot.total == 0.0


def test_gate_rejects_an_unpriced_model_only_when_a_budget_applies():
    snapshot = SpendSnapshot()
    snapshot.seed(0.0, {}, now=0)
    body = b'{"model":"unknown-model","messages":[]}'

    budgeted = BudgetGate(BudgetPolicy(global_daily_usd=1.0), snapshot)
    assert budgeted.check({}, body, body, provider_free=False).error_type == (
        "ctrlrtn_unpriced_model"
    )
    assert budgeted.check({}, body, body, provider_free=True).allowed

    unbudgeted = BudgetGate(BudgetPolicy(), snapshot)
    assert unbudgeted.check({}, body, body, provider_free=False).allowed


def test_gate_reports_the_matching_use_case_ceiling():
    snapshot = SpendSnapshot()
    snapshot.seed(0.5, {"tag:editor": 0.5})
    gate = BudgetGate(
        BudgetPolicy(use_case_daily_usd={"tag:editor": 0.5}), snapshot
    )
    body = b'{"model":"gpt-4o","messages":[]}'

    decision = gate.check(
        {"x-ctrlrtn-route": "editor"}, body, body, provider_free=False
    )

    assert decision.error_type == "ctrlrtn_budget_exceeded"
    assert decision.scope == "tag:editor"
    assert decision.spent_usd == decision.limit_usd == 0.5
    assert gate.check(
        {"x-ctrlrtn-route": "other"}, body, body, provider_free=False
    ).allowed


def test_gate_requires_session_identity_and_reports_its_ceiling():
    snapshot = SpendSnapshot()
    snapshot.seed_sessions({"session-7": 0.5})
    gate = BudgetGate(BudgetPolicy(session_limit_usd=0.5), snapshot)
    body = b'{"model":"gpt-4o","messages":[]}'

    missing = gate.check({}, body, body, provider_free=False)
    reached = gate.check(
        {"x-ctrlrtn-session": "session-7"}, body, body, provider_free=False
    )

    assert missing.error_type == "ctrlrtn_session_required"
    assert reached.error_type == "ctrlrtn_budget_exceeded"
    assert reached.scope == "session:session-7"
    assert reached.spent_usd == reached.limit_usd == 0.5
    assert gate.check(
        {"x-ctrlrtn-session": "session-8"}, body, body, provider_free=False
    ).allowed


def test_gate_rejects_a_session_with_historical_unknown_cost():
    snapshot = SpendSnapshot()
    snapshot.seed_sessions({}, unknown_sessions={"session-7"})
    gate = BudgetGate(BudgetPolicy(session_limit_usd=1.0), snapshot)
    body = b'{"model":"gpt-4o","messages":[]}'

    decision = gate.check(
        {"x-ctrlrtn-session": "session-7"}, body, body, provider_free=False
    )

    assert decision.error_type == "ctrlrtn_unknown_session_cost"


def test_gate_does_not_require_a_session_for_non_model_provider_requests():
    gate = BudgetGate(BudgetPolicy(session_limit_usd=0), SpendSnapshot())

    assert gate.check({}, b"", b"", provider_free=False).allowed


def test_gate_reserves_before_admission_and_reconciles_recorded_cost():
    snapshot = SpendSnapshot()
    snapshot.seed(0.0, {}, now=0)
    gate = BudgetGate(
        BudgetPolicy(global_daily_usd=0.015, reserve_in_flight=True),
        snapshot,
    )
    body = b'{"model":"gpt-4o","max_tokens":1000,"messages":[]}'

    first = gate.check({}, body, body, provider_free=False)
    second = gate.check({}, body, body, provider_free=False)

    assert first.allowed
    assert first.reservation_id is not None
    assert 0.01 < first.reserved_usd < 0.015
    assert not second.allowed
    assert second.error_type == "ctrlrtn_budget_exceeded"
    assert second.scope == "global"
    assert second.spent_usd == pytest.approx(first.reserved_usd)

    gate.observe(
        Trace(
            method="POST",
            path="/v1/chat/completions",
            query="",
            request_headers={},
            request_body=body,
            status_code=200,
            response_headers={},
            response_body=b"{}",
            latency_ms=1,
            ts=0,
            cost_usd=0.002,
            budget_reservation_id=first.reservation_id,
        ),
        now=0,
    )

    assert gate.reserved_usd == 0.0
    assert snapshot.total == 0.002
    assert gate.check({}, body, body, provider_free=False).allowed


def test_reservation_mode_rejects_a_request_without_an_output_bound():
    gate = BudgetGate(
        BudgetPolicy(global_daily_usd=1.0, reserve_in_flight=True),
        SpendSnapshot(),
    )
    body = b'{"model":"gpt-4o","messages":[]}'

    decision = gate.check({}, body, body, provider_free=False)

    assert decision.error_type == "ctrlrtn_unreservable_request"
    assert decision.reservation_id is None


async def test_gateway_rejects_an_unreservable_request_before_upstream(
    streaming_upstream,
):
    record: dict = {}
    gate = BudgetGate(
        BudgetPolicy(global_daily_usd=1.0, reserve_in_flight=True),
        SpendSnapshot(),
    )
    store = InMemoryTraceStore()
    recorder = Recorder(store, enrich=enrich_trace, on_record=gate.observe)
    app = create_app(
        Settings(upstream_base_url="http://upstream"),
        upstream_client=httpx.AsyncClient(
            transport=httpx.ASGITransport(app=streaming_upstream(record)),
            base_url="http://upstream",
        ),
        recorder=recorder,
        budget_gate=gate,
    )

    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://router"
        ) as client:
            response = await client.post(
                "/v1/chat/completions",
                content=b'{"model":"gpt-4o","messages":[]}',
            )
        await recorder.join()

    assert response.status_code == 400
    assert response.json()["error"]["type"] == "ctrlrtn_unreservable_request"
    assert record == {}
    assert store.traces[0].terminal_reason == "unreservable_request"


def test_free_provider_needs_no_reservation():
    gate = BudgetGate(
        BudgetPolicy(global_daily_usd=1.0, reserve_in_flight=True),
        SpendSnapshot(),
    )
    body = b'{"model":"local-model","messages":[]}'

    decision = gate.check({}, body, body, provider_free=True)

    assert decision.allowed
    assert decision.reservation_id is None
    assert decision.reserved_usd == 0.0


def test_session_reservations_are_isolated_by_session_id():
    gate = BudgetGate(
        BudgetPolicy(session_limit_usd=0.015, reserve_in_flight=True),
        SpendSnapshot(),
    )
    body = b'{"model":"gpt-4o","max_tokens":1000,"messages":[]}'

    first = gate.check(
        {"x-ctrlrtn-session": "session-1"}, body, body, provider_free=False
    )
    same_session = gate.check(
        {"x-ctrlrtn-session": "session-1"}, body, body, provider_free=False
    )
    other_session = gate.check(
        {"x-ctrlrtn-session": "session-2"}, body, body, provider_free=False
    )

    assert first.allowed
    assert same_session.error_type == "ctrlrtn_budget_exceeded"
    assert same_session.scope == "session:session-1"
    assert other_session.allowed


async def test_proxy_settles_a_reservation_after_enriched_persistence():
    async def upstream(request) -> JSONResponse:
        return JSONResponse(
            {
                "model": "gpt-4o",
                "usage": {"prompt_tokens": 100, "completion_tokens": 10},
            }
        )

    upstream_app = Starlette(
        routes=[Route("/{path:path}", upstream, methods=["POST"])]
    )

    snapshot = SpendSnapshot()
    snapshot.seed(0.0, {})
    gate = BudgetGate(
        BudgetPolicy(global_daily_usd=1.0, reserve_in_flight=True),
        snapshot,
    )
    store = InMemoryTraceStore()
    recorder = Recorder(store, enrich=enrich_trace, on_record=gate.observe)
    app = create_app(
        Settings(upstream_base_url="http://upstream"),
        upstream_client=httpx.AsyncClient(
            transport=httpx.ASGITransport(app=upstream_app),
            base_url="http://upstream",
        ),
        recorder=recorder,
        budget_gate=gate,
    )

    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://router"
        ) as client:
            response = await client.post(
                "/v1/chat/completions",
                content=(b'{"model":"gpt-4o","max_tokens":1000,"messages":[]}'),
            )
        await recorder.join()

    assert response.status_code == 200
    assert gate.reserved_usd == 0.0
    assert snapshot.total == pytest.approx(0.00035)
    assert store.traces[0].budget_reservation_id is not None


async def test_proxy_releases_a_reservation_after_upstream_send_failure():
    async def fail(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("offline", request=request)

    snapshot = SpendSnapshot()
    snapshot.seed(0.0, {})
    gate = BudgetGate(
        BudgetPolicy(global_daily_usd=1.0, reserve_in_flight=True),
        snapshot,
    )
    store = InMemoryTraceStore()
    recorder = Recorder(store, enrich=enrich_trace, on_record=gate.observe)
    app = create_app(
        Settings(upstream_base_url="http://upstream"),
        upstream_client=httpx.AsyncClient(
            transport=httpx.MockTransport(fail),
            base_url="http://upstream",
        ),
        recorder=recorder,
        budget_gate=gate,
    )

    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://router"
        ) as client:
            with pytest.raises(httpx.ConnectError):
                await client.post(
                    "/v1/chat/completions",
                    content=(
                        b'{"model":"gpt-4o","max_tokens":1000,'
                        b'"messages":[]}'
                    ),
                )
        await recorder.join()

    assert gate.reserved_usd == 0.0
    assert store.traces[0].terminal_reason == "send_failure"


async def test_dropped_send_failure_trace_still_releases_reservation():
    started = asyncio.Event()
    release = asyncio.Event()

    class BlockingStore(InMemoryTraceStore):
        async def save(self, trace: Trace) -> None:
            started.set()
            await release.wait()
            await super().save(trace)

    async def fail(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("offline", request=request)

    gate = BudgetGate(
        BudgetPolicy(global_daily_usd=1.0, reserve_in_flight=True),
        SpendSnapshot(),
    )
    store = BlockingStore()
    recorder = Recorder(
        store,
        enrich=enrich_trace,
        on_record=gate.observe,
        maxsize=1,
    )
    upstream_client = httpx.AsyncClient(transport=httpx.MockTransport(fail))
    app = create_app(
        Settings(upstream_base_url="http://upstream"),
        upstream_client=upstream_client,
        recorder=recorder,
        budget_gate=gate,
    )
    filler = Trace(
        method="POST",
        path="/",
        query="",
        request_headers={},
        request_body=b"{}",
        status_code=200,
        response_headers={},
        response_body=b"{}",
        latency_ms=0,
    )

    async with app.router.lifespan_context(app):
        recorder.enqueue(filler)
        await started.wait()
        recorder.enqueue(filler)
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://router"
        ) as client:
            with pytest.raises(httpx.ConnectError):
                await client.post(
                    "/v1/chat/completions",
                    content=(
                        b'{"model":"gpt-4o","max_tokens":1000,'
                        b'"messages":[]}'
                    ),
                )
        assert recorder.dropped == 1
        assert gate.reserved_usd == 0.0
        release.set()
        await recorder.join()

    await upstream_client.aclose()


async def test_request_construction_failure_releases_reservation(monkeypatch):
    gate = BudgetGate(
        BudgetPolicy(global_daily_usd=1.0, reserve_in_flight=True),
        SpendSnapshot(),
    )
    store = InMemoryTraceStore()
    recorder = Recorder(store, enrich=enrich_trace, on_record=gate.observe)
    upstream_client = httpx.AsyncClient()

    def fail_build(*args, **kwargs):
        raise httpx.InvalidURL("invalid configured upstream")

    monkeypatch.setattr(upstream_client, "build_request", fail_build)
    app = create_app(
        Settings(upstream_base_url="http://upstream"),
        upstream_client=upstream_client,
        recorder=recorder,
        budget_gate=gate,
    )

    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://router"
        ) as client:
            with pytest.raises(httpx.InvalidURL):
                await client.post(
                    "/v1/chat/completions",
                    content=(
                        b'{"model":"gpt-4o","max_tokens":1000,'
                        b'"messages":[]}'
                    ),
                )
        await recorder.join()

    assert gate.reserved_usd == 0.0
    assert store.traces[0].terminal_reason == "send_failure"
    await upstream_client.aclose()


async def test_gateway_blocks_at_the_global_ceiling_before_upstream(
    streaming_upstream,
):
    record: dict = {}
    snapshot = SpendSnapshot()
    snapshot.seed(1.0, {})
    app = create_app(
        Settings(upstream_base_url="http://upstream"),
        upstream_client=httpx.AsyncClient(
            transport=httpx.ASGITransport(app=streaming_upstream(record)),
            base_url="http://upstream",
        ),
        budget_gate=BudgetGate(BudgetPolicy(global_daily_usd=1.0), snapshot),
    )
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://router"
    ) as client:
        response = await client.post(
            "/v1/chat/completions",
            content=b'{"model":"gpt-4o","messages":[]}',
        )

    assert response.status_code == 429
    assert response.json()["error"] == {
        "type": "ctrlrtn_budget_exceeded",
        "message": "daily budget exceeded",
        "scope": "global",
        "spent_usd": 1.0,
        "limit_usd": 1.0,
    }
    assert record == {}


async def test_gateway_records_a_budget_block_without_an_experiment(
    streaming_upstream,
):
    record: dict = {}
    store = InMemoryTraceStore()
    recorder = Recorder(store)
    snapshot = SpendSnapshot()
    snapshot.seed(1.0, {})
    app = create_app(
        Settings(upstream_base_url="http://upstream"),
        upstream_client=httpx.AsyncClient(
            transport=httpx.ASGITransport(app=streaming_upstream(record)),
            base_url="http://upstream",
        ),
        recorder=recorder,
        budget_gate=BudgetGate(BudgetPolicy(global_daily_usd=1.0), snapshot),
    )

    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://router"
        ) as client:
            response = await client.post(
                "/v1/chat/completions",
                content=b'{"model":"gpt-4o","messages":[]}',
            )
        await recorder.join()

    assert response.status_code == 429
    assert record == {}
    assert len(store.traces) == 1
    assert store.traces[0].terminal_reason == "budget"
    assert store.traces[0].experiment_id is None


def test_budget_status_renders_known_remaining_and_unknowns():
    output = render_budget_status(
        BudgetPolicy(
            global_daily_usd=10.0,
            use_case_daily_usd={"tag:editor": 3.0},
            use_case_fallback_usd={"tag:editor": 2.0},
            session_limit_usd=2.0,
            reserve_in_flight=True,
        ),
        kill_switch=False,
        daily_total=4.0,
        daily_by_use_case={"tag:editor": 1.25},
        unknown_priced_calls=2,
        blocked={
            "budget": 3,
            "session_required": 1,
            "unreservable_request": 2,
        },
        fallback_calls=4,
    )

    assert "Kill switch: off" in output
    assert "Global daily: $4.0000 / $10.0000 ($6.0000 remaining)" in output
    assert "tag:editor: $1.2500 / $3.0000 ($1.7500 remaining)" in output
    assert "fallback at $2.0000" in output
    assert "Session limit: $2.0000 per x-ctrlrtn-session" in output
    assert "In-flight reservations: on" in output
    assert "Unknown-priced calls today: 2" in output
    assert "budget=3" in output
    assert "session_required=1" in output
    assert "unreservable_request=2" in output
    assert "Evidence-approved fallbacks today: 4" in output


async def test_budget_command_reads_current_policy_and_today(
    tmp_path, monkeypatch, capsys
):
    path = str(tmp_path / "budget-status.db")
    store = SqliteTraceStore(path)
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
            latency_ms=1,
            ts=time.time(),
            cost_usd=0.25,
            use_case_key="tag:editor",
        )
    )
    store.close()
    settings = Settings(
        db_path=path,
        kill_switch=True,
        budget_policy=BudgetPolicy(global_daily_usd=1.0),
    )
    monkeypatch.setattr(cli, "load_settings", lambda: settings)

    cli.main(["budget"])

    output = capsys.readouterr().out
    assert "Kill switch: ON" in output
    assert "Global daily: $0.2500 / $1.0000 ($0.7500 remaining)" in output


async def test_gateway_blocks_an_unpriced_model_under_a_matching_budget(
    streaming_upstream,
):
    record: dict = {}
    snapshot = SpendSnapshot()
    snapshot.seed(0.0, {})
    app = create_app(
        Settings(upstream_base_url="http://upstream"),
        upstream_client=httpx.AsyncClient(
            transport=httpx.ASGITransport(app=streaming_upstream(record)),
            base_url="http://upstream",
        ),
        budget_gate=BudgetGate(BudgetPolicy(global_daily_usd=1.0), snapshot),
    )
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://router"
    ) as client:
        response = await client.post(
            "/v1/chat/completions",
            content=b'{"model":"brand-new-model","messages":[]}',
        )

    assert response.status_code == 503
    assert response.json()["error"]["type"] == "ctrlrtn_unpriced_model"
    assert record == {}


async def test_gateway_session_policy_fails_closed_before_upstream(
    streaming_upstream,
):
    record: dict = {}
    snapshot = SpendSnapshot()
    snapshot.seed_sessions(
        {"session-7": 1.0}, unknown_sessions={"session-unknown"}
    )
    app = create_app(
        Settings(upstream_base_url="http://upstream"),
        upstream_client=httpx.AsyncClient(
            transport=httpx.ASGITransport(app=streaming_upstream(record)),
            base_url="http://upstream",
        ),
        budget_gate=BudgetGate(BudgetPolicy(session_limit_usd=1.0), snapshot),
    )
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://router"
    ) as client:
        missing = await client.post(
            "/v1/chat/completions",
            content=b'{"model":"gpt-4o","messages":[]}',
        )
        reached = await client.post(
            "/v1/chat/completions",
            headers={"x-ctrlrtn-session": "session-7"},
            content=b'{"model":"gpt-4o","messages":[]}',
        )
        unknown = await client.post(
            "/v1/chat/completions",
            headers={"x-ctrlrtn-session": "session-unknown"},
            content=b'{"model":"gpt-4o","messages":[]}',
        )

    assert missing.status_code == 400
    assert missing.json()["error"]["type"] == "ctrlrtn_session_required"
    assert reached.status_code == 429
    assert reached.json()["error"] == {
        "type": "ctrlrtn_budget_exceeded",
        "message": "session budget exceeded",
        "scope": "session:session-7",
        "spent_usd": 1.0,
        "limit_usd": 1.0,
    }
    assert unknown.status_code == 503
    assert unknown.json()["error"] == {
        "type": "ctrlrtn_unknown_session_cost",
        "message": "session contains calls with unknown cost",
    }
    assert record == {}


async def test_production_wiring_seeds_and_updates_the_snapshot(tmp_path):
    path = str(tmp_path / "budget.db")
    writer = SqliteTraceStore(path)
    await writer.save(
        Trace(
            method="POST",
            path="/v1/chat/completions",
            query="",
            request_headers={},
            request_body=b"{}",
            status_code=200,
            response_headers={},
            response_body=b"",
            latency_ms=1,
            ts=time.time(),
            cost_usd=0.25,
            use_case_key="tag:existing",
            session_id="session-existing",
        )
    )
    await writer.save(
        Trace(
            method="POST",
            path="/v1/chat/completions",
            query="",
            request_headers={},
            request_body=b"{}",
            status_code=200,
            response_headers={},
            response_body=b"",
            latency_ms=1,
            ts=time.time(),
            cost_usd=None,
            session_id="session-unknown",
        )
    )
    writer.close()

    app = build_app(
        settings=Settings(
            db_path=path,
            upstream_base_url="http://upstream",
            budget_policy=BudgetPolicy(
                global_daily_usd=10, session_limit_usd=10
            ),
        )
    )
    gate = app.state.budget_gate
    assert gate.snapshot.total == 0.25
    assert gate.snapshot.by_use_case == {"tag:existing": 0.25}
    assert gate.snapshot.session_total("session-existing") == 0.25
    assert not gate.snapshot.session_cost_known("session-unknown")

    async with app.router.lifespan_context(app):
        app.state.recorder.enqueue(
            Trace(
                method="POST",
                path="/v1/chat/completions",
                query="",
                request_headers={
                    "x-ctrlrtn-route": "editor",
                    "x-ctrlrtn-session": "session-new",
                },
                request_body=b'{"model":"gpt-4o","messages":[]}',
                status_code=200,
                response_headers={},
                response_body=(
                    b'{"model":"gpt-4o","usage":{"prompt_tokens":1000,'
                    b'"completion_tokens":100}}'
                ),
                latency_ms=1,
            )
        )
        await app.state.recorder.join()
        assert gate.snapshot.total > 0.25
        assert gate.snapshot.by_use_case["tag:editor"] > 0
        assert gate.snapshot.session_total("session-new") > 0

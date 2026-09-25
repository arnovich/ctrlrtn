"""The live A/B serving path (slice 4): the ExperimentRouter decide() hook —
snapshot lookup, task binding, model swap, and cache composition."""

from __future__ import annotations

import json

import httpx
import pytest

from ctrlrtn.config import Settings
from ctrlrtn.gateway.app import create_app
from ctrlrtn.gateway.proxy import TerminalError
from ctrlrtn.gateway.serving import ExperimentRouter
from ctrlrtn.policy.experiment import (
    BASELINE,
    CANDIDATE,
    Experiment,
    assign_arm,
)
from ctrlrtn.recorder.recorder import Recorder
from ctrlrtn.recorder.store import InMemoryTraceStore
from ctrlrtn.recorder.trace import Trace
from ctrlrtn.routing import ProviderCredential, UpstreamRoute
from ctrlrtn.telemetry.enrich import enrich_trace
from ctrlrtn.telemetry.pricing import cost_usd
from ctrlrtn.telemetry.usage import Usage

_PATH = "/v1/messages"
_BODY = b'{"model": "claude-opus-4", "messages": []}'


def _headers(task="task-1", route="editor"):
    h = {}
    if task is not None:
        h["x-ctrlrtn-task"] = task
    if route is not None:
        h["x-ctrlrtn-route"] = route
    return h


def _task_for_arm(exp: Experiment, arm: str) -> str:
    for i in range(2000):
        task = f"task-{i}"
        if assign_arm(exp, task) == arm:
            return task
    raise AssertionError(f"no task hit arm {arm}")


async def _router(store, *, inject_cache=False) -> ExperimentRouter:
    router = ExperimentRouter(
        store, inject_cache=inject_cache, refresh_seconds=999
    )
    await router.refresh()
    return router


def _model_of(body: bytes) -> str:
    return json.loads(body)["model"]


# --- decide() -------------------------------------------------------------


async def test_no_experiments_passes_through():
    router = await _router(InMemoryTraceStore())
    forward, serve = router.decide(_PATH, _headers(), _BODY)
    assert forward == _BODY and serve is None


async def test_untasked_request_is_never_assigned():
    store = InMemoryTraceStore()
    store.create_experiment(
        Experiment("tag:editor", "claude-haiku-4-5", 99, experiment_id="e")
    )
    router = await _router(store)
    forward, serve = router.decide(_PATH, _headers(task=None), _BODY)
    assert forward == _BODY and serve is None


async def test_candidate_arm_swaps_the_model():
    store = InMemoryTraceStore()
    exp = Experiment(
        "tag:editor",
        "claude-haiku-4-5",
        50,
        experiment_id="e",
        candidate_provider="ollama",
    )
    store.create_experiment(exp)
    router = await _router(store)
    task = _task_for_arm(exp, CANDIDATE)
    forward, serve = router.decide(_PATH, _headers(task=task), _BODY)
    assert serve is not None and serve.is_candidate
    assert serve.served_model == "claude-haiku-4-5"
    assert serve.provider == "ollama"
    assert _model_of(forward) == "claude-haiku-4-5"  # forwarded body rewritten
    assert _model_of(_BODY) == "claude-opus-4"  # original bytes untouched


async def test_baseline_arm_passes_body_through():
    store = InMemoryTraceStore()
    exp = Experiment(
        "tag:editor",
        "claude-haiku-4-5",
        50,
        experiment_id="e",
        candidate_provider="ollama",
    )
    store.create_experiment(exp)
    router = await _router(store)
    task = _task_for_arm(exp, BASELINE)
    forward, serve = router.decide(_PATH, _headers(task=task), _BODY)
    assert serve is not None and serve.is_baseline
    assert serve.served_model == "claude-opus-4"
    assert serve.provider is None
    assert forward == _BODY  # baseline never rewrites


async def test_candidate_arm_switches_to_a_same_api_provider(
    streaming_upstream, monkeypatch
):
    record: dict = {}
    monkeypatch.setenv("OLLAMA_API_KEY", "ollama-cloud-secret")
    store = InMemoryTraceStore()
    exp = Experiment(
        "tag:editor",
        "qwen2.5:0.5b",
        50,
        experiment_id="exp:cross",
        candidate_provider="ollama",
    )
    store.create_experiment(exp)
    recorder = Recorder(store, enrich=enrich_trace)
    settings = Settings(
        routes=(
            UpstreamRoute(
                "openai",
                "http://openai",
                ("/v1/chat/completions",),
                api="openai",
            ),
            UpstreamRoute(
                "ollama",
                "http://ollama",
                ("/ollama",),
                strip_prefix="/ollama",
                api="openai",
                free=True,
                credential=ProviderCredential("OLLAMA_API_KEY"),
            ),
        )
    )
    app = create_app(
        settings,
        upstream_client=httpx.AsyncClient(
            transport=httpx.ASGITransport(app=streaming_upstream(record)),
            base_url="http://upstream",
        ),
        recorder=recorder,
        store=store,
    )
    await app.state.experiment_router.refresh()
    recorder.start()
    task = _task_for_arm(exp, CANDIDATE)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://localhost"
    ) as client:
        response = await client.post(
            "/v1/chat/completions?api_key=baseline-secret&x=1",
            content=b'{"model":"gpt-4o","messages":[]}',
            headers={
                "authorization": "Bearer baseline-secret",
                "x-api-key": "baseline-secret",
                "x-ctrlrtn-task": task,
                "x-ctrlrtn-route": "editor",
                "content-type": "application/json",
            },
        )
    await recorder.join()
    await recorder.aclose()

    assert response.status_code == 200
    assert record["headers"]["host"] == "ollama"
    assert record["headers"]["authorization"] == "Bearer ollama-cloud-secret"
    assert "x-api-key" not in record["headers"]
    assert record["path"] == "/v1/chat/completions"
    assert record["query"] == "x=1"
    assert json.loads(record["body"])["model"] == "qwen2.5:0.5b"
    trace = store.traces[0]
    assert trace.arm == CANDIDATE
    assert trace.provider == "ollama"
    assert trace.provider_free is True
    assert trace.cost_usd == 0.0


async def test_cross_provider_candidate_rejects_an_incompatible_api(
    streaming_upstream,
):
    record: dict = {}
    store = InMemoryTraceStore()
    exp = Experiment(
        "tag:editor",
        "claude-haiku-4-5",
        50,
        experiment_id="exp:mismatch",
        candidate_provider="claude-local",
    )
    store.create_experiment(exp)
    recorder = Recorder(store)
    settings = Settings(
        routes=(
            UpstreamRoute(
                "openai",
                "http://openai",
                ("/v1/chat/completions",),
                api="openai",
            ),
            UpstreamRoute(
                "claude-local",
                "http://claude",
                ("/claude",),
                strip_prefix="/claude",
                api="anthropic",
            ),
        )
    )
    app = create_app(
        settings,
        upstream_client=httpx.AsyncClient(
            transport=httpx.ASGITransport(app=streaming_upstream(record)),
            base_url="http://upstream",
        ),
        recorder=recorder,
        store=store,
    )
    await app.state.experiment_router.refresh()
    recorder.start()
    task = _task_for_arm(exp, CANDIDATE)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://router"
    ) as client:
        response = await client.post(
            "/v1/chat/completions",
            content=b'{"model":"gpt-4o","messages":[]}',
            headers={
                "x-ctrlrtn-task": task,
                "x-ctrlrtn-route": "editor",
                "content-type": "application/json",
            },
        )
    await recorder.join()
    await recorder.aclose()

    assert response.status_code == 502
    assert response.json()["error"]["type"] == "ctrlrtn_provider_mismatch"
    assert record == {}
    trace = store.traces[0]
    assert trace.arm == CANDIDATE
    assert trace.provider == "claude-local"
    assert trace.terminal_reason == "provider"


# --- divergence ceiling (4b) ----------------------------------------------


async def test_candidate_ceiling_raises_terminal_after_max_calls():
    store = InMemoryTraceStore()
    exp = Experiment(
        "tag:editor",
        "claude-haiku-4-5",
        50,
        experiment_id="e",
        max_calls_per_task=3,
    )
    store.create_experiment(exp)
    router = await _router(store)
    h = _headers(task=_task_for_arm(exp, CANDIDATE))
    for _ in range(3):  # the first max_calls_per_task candidate calls serve
        _, serve = router.decide(_PATH, h, _BODY)
        assert serve is not None and serve.is_candidate
    with pytest.raises(TerminalError) as excinfo:  # the next one is terminal
        router.decide(_PATH, h, _BODY)
    assert excinfo.value.status == 429
    assert excinfo.value.serve.is_candidate  # the arm rides on the terminal


async def test_ceiling_records_the_failure_only_once():
    # A client that retries the terminal must not inflate the candidate's failure
    # count: the first breach records, every later breach does not.
    store = InMemoryTraceStore()
    exp = Experiment(
        "tag:editor",
        "claude-haiku-4-5",
        50,
        experiment_id="e",
        max_calls_per_task=1,
    )
    store.create_experiment(exp)
    router = await _router(store)
    h = _headers(task=_task_for_arm(exp, CANDIDATE))
    router.decide(_PATH, h, _BODY)  # the one allowed candidate call
    with pytest.raises(TerminalError) as first:
        router.decide(_PATH, h, _BODY)
    assert first.value.record is True  # first breach: count it
    for _ in range(3):  # retries of the terminal: do not re-count
        with pytest.raises(TerminalError) as again:
            router.decide(_PATH, h, _BODY)
        assert again.value.record is False


async def test_baseline_arm_never_hits_the_ceiling():
    store = InMemoryTraceStore()
    exp = Experiment(
        "tag:editor",
        "claude-haiku-4-5",
        50,
        experiment_id="e",
        max_calls_per_task=1,
    )
    store.create_experiment(exp)
    router = await _router(store)
    h = _headers(task=_task_for_arm(exp, BASELINE))
    for _ in range(5):  # the incumbent is never ceilinged, however many calls
        _, serve = router.decide(_PATH, h, _BODY)
        assert serve is not None and serve.is_baseline


async def test_use_case_without_an_experiment_passes_through():
    store = InMemoryTraceStore()
    store.create_experiment(
        Experiment("tag:editor", "claude-haiku-4-5", 99, experiment_id="e")
    )
    router = await _router(store)
    # tagged for a use-case that has no experiment
    forward, serve = router.decide(_PATH, _headers(route="analyst"), _BODY)
    assert forward == _BODY and serve is None


async def test_body_without_a_model_is_not_assigned():
    store = InMemoryTraceStore()
    store.create_experiment(
        Experiment("tag:editor", "claude-haiku-4-5", 99, experiment_id="e")
    )
    router = await _router(store)
    body = b'{"messages": []}'  # nothing to swap
    forward, serve = router.decide(_PATH, _headers(), body)
    assert serve is None and forward == body


async def test_non_json_body_is_not_assigned():
    store = InMemoryTraceStore()
    store.create_experiment(
        Experiment("tag:editor", "claude-haiku-4-5", 99, experiment_id="e")
    )
    router = await _router(store)
    body = b"not json at all"  # tag gives the use-case; body can't be parsed
    forward, serve = router.decide(_PATH, _headers(), body)
    assert serve is None and forward == body


async def test_a_task_binds_to_one_experiment():
    store = InMemoryTraceStore()
    store.create_experiment(
        Experiment("tag:editor", "claude-haiku-4-5", 99, experiment_id="exp:e1")
    )
    store.create_experiment(
        Experiment(
            "tag:analyst", "claude-3-5-haiku", 99, experiment_id="exp:e2"
        )
    )
    router = await _router(store)
    # first contact (editor) binds the task to e1
    _, s1 = router.decide(_PATH, _headers(task="ed-1", route="editor"), _BODY)
    assert s1 is not None and s1.experiment_id == "exp:e1"
    # same task on analyst -> left alone (can't straddle two experiments)
    f2, s2 = router.decide(_PATH, _headers(task="ed-1", route="analyst"), _BODY)
    assert s2 is None and f2 == _BODY
    # a different task on analyst -> gets e2
    _, s3 = router.decide(_PATH, _headers(task="ed-2", route="analyst"), _BODY)
    assert s3 is not None and s3.experiment_id == "exp:e2"


async def test_refresh_picks_up_a_new_experiment():
    store = InMemoryTraceStore()
    router = await _router(store)
    _, serve = router.decide(_PATH, _headers(), _BODY)
    assert serve is None  # nothing running yet
    store.create_experiment(
        Experiment("tag:editor", "claude-haiku-4-5", 99, experiment_id="e")
    )
    await router.refresh()
    _, serve = router.decide(_PATH, _headers(), _BODY)
    assert serve is not None  # picked up after refresh


async def test_cache_injection_composes_with_pass_through():
    # inject_cache on, no experiment: the router still injects a cache breakpoint
    # (composition wires through), and assigns no arm.
    big = "x" * 5000
    body = json.dumps({"model": "claude-opus-4", "system": big}).encode()
    router = await _router(InMemoryTraceStore(), inject_cache=True)
    forward, serve = router.decide(_PATH, _headers(task=None), body)
    assert serve is None
    assert b"cache_control" in forward  # injected
    assert forward != body


async def test_start_and_close_manage_the_refresh_task():
    router = ExperimentRouter(
        InMemoryTraceStore(), inject_cache=False, refresh_seconds=999
    )
    await router.start()
    assert router._refresh_task is not None
    await router.aclose()
    assert router._refresh_task is None


def test_enrich_prices_a_candidate_call_on_the_served_model():
    # A candidate call: opus requested, haiku served. The provider billed haiku,
    # so cost must use haiku's rate — not opus's (which trace.model still shows).
    trace = Trace(
        method="POST",
        path="/v1/messages",
        query="",
        request_headers={},
        request_body=b'{"model": "claude-opus-4"}',
        status_code=200,
        response_headers={},
        response_body=b'{"usage": {"input_tokens": 1000, "output_tokens": 500}}',
        latency_ms=1.0,
        served_model="claude-haiku-4-5",
    )
    enrich_trace(trace)
    assert trace.model == "claude-opus-4"  # display keeps the requested model
    usage = Usage(input_tokens=1000, output_tokens=500)
    assert trace.cost_usd == cost_usd("claude-haiku-4-5", usage)  # served rate
    assert trace.cost_usd != cost_usd("claude-opus-4", usage)  # not requested


# --- end to end through the gateway ---------------------------------------


async def test_serving_swaps_model_end_to_end(streaming_upstream):
    record: dict = {}
    upstream = streaming_upstream(record)
    store = InMemoryTraceStore()
    exp = Experiment("tag:editor", "claude-haiku-4-5", 50, experiment_id="e")
    store.create_experiment(exp)
    recorder = Recorder(store)
    recorder.start()
    task = _task_for_arm(exp, CANDIDATE)

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=upstream),
        base_url="http://upstream",
    ) as upstream_client:
        app = create_app(
            Settings(upstream_base_url="http://upstream"),
            upstream_client=upstream_client,
            recorder=recorder,
            store=store,
        )
        # ASGITransport doesn't run lifespan, so load the snapshot as start()
        # would (mirrors the manual recorder.start() above).
        await app.state.experiment_router.refresh()
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://router"
        ) as client:
            resp = await client.post(
                "/v1/messages",
                content=_BODY,
                headers={
                    "x-ctrlrtn-task": task,
                    "x-ctrlrtn-route": "editor",
                    "content-type": "application/json",
                },
            )
    await recorder.join()
    await recorder.aclose()
    assert resp.status_code == 200
    assert _model_of(record["body"]) == "claude-haiku-4-5"  # upstream saw swap
    trace = store.traces[0]
    assert trace.arm == "candidate"
    assert trace.served_model == "claude-haiku-4-5"
    assert trace.request_body == _BODY  # recorded the original (stable fp)


def _candidate_trace() -> Trace:
    return Trace(
        method="POST",
        path=_PATH,
        query="",
        request_headers={},
        request_body=_BODY,
        status_code=200,
        response_headers={},
        response_body=b"",
        latency_ms=1.0,
        experiment_id="e",
        arm=CANDIDATE,
        served_model="claude-haiku-4-5",
    )


async def test_candidate_traces_are_never_dropped():
    # A maxsize-1 queue would drop under any burst; a candidate trace must not.
    store = InMemoryTraceStore()
    recorder = Recorder(store, maxsize=1)
    recorder.start()
    for _ in range(20):
        await recorder.enqueue_important(_candidate_trace())
    await recorder.join()
    await recorder.aclose()
    assert recorder.dropped == 0
    assert len(store.traces) == 20  # every candidate call persisted


async def test_enqueue_important_drops_loudly_when_wedged():
    # No worker draining + a full queue: the important enqueue must not hang; it
    # drops after the bounded timeout and counts the loss loudly.
    store = InMemoryTraceStore()
    recorder = Recorder(store, maxsize=1, important_timeout=0.01)
    recorder.enqueue(_candidate_trace())  # fill the only slot
    await recorder.enqueue_important(_candidate_trace())  # can't fit -> drop
    assert recorder.dropped_important == 1


async def test_aclose_drains_the_backlog():
    # Shutdown must persist queued traces, not discard them with the worker.
    store = InMemoryTraceStore()
    recorder = Recorder(store)
    recorder.start()
    for _ in range(5):
        recorder.enqueue(_candidate_trace())
    await recorder.aclose()  # no explicit join(): aclose drains first
    assert len(store.traces) == 5


async def test_gateway_counts_a_ceiling_breach_as_a_failure(streaming_upstream):
    record: dict = {}
    upstream = streaming_upstream(record)
    store = InMemoryTraceStore()
    exp = Experiment(
        "tag:editor",
        "claude-haiku-4-5",
        50,
        experiment_id="e",
        max_calls_per_task=1,
    )
    store.create_experiment(exp)
    recorder = Recorder(store)
    recorder.start()
    task = _task_for_arm(exp, CANDIDATE)
    headers = {
        "x-ctrlrtn-task": task,
        "x-ctrlrtn-route": "editor",
        "content-type": "application/json",
    }

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=upstream),
        base_url="http://upstream",
    ) as upstream_client:
        app = create_app(
            Settings(upstream_base_url="http://upstream"),
            upstream_client=upstream_client,
            recorder=recorder,
            store=store,
        )
        await app.state.experiment_router.refresh()
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://router"
        ) as client:
            first = await client.post(
                "/v1/messages", content=_BODY, headers=headers
            )
            record.clear()  # forget the (legitimate) first upstream call
            second = await client.post(
                "/v1/messages", content=_BODY, headers=headers
            )
    await recorder.join()
    await recorder.aclose()

    assert first.status_code == 200  # the first candidate call served normally
    assert second.status_code == 429  # the second breached the ceiling
    assert second.headers.get("retry-after")  # tell the client to back off
    assert second.json()["error"]["type"] == "ctrlrtn_divergence_ceiling"
    assert record == {}  # the terminal never reached upstream
    breach = store.traces[-1]
    assert breach.arm == "candidate"  # counted as a candidate failure
    assert breach.status_code == 429
    assert breach.terminal_reason == "ceiling"  # distinguishable from infra 429


async def test_send_failure_still_records_the_arm():
    store = InMemoryTraceStore()
    exp = Experiment("tag:editor", "claude-haiku-4-5", 50, experiment_id="e")
    store.create_experiment(exp)
    recorder = Recorder(store)
    recorder.start()
    task = _task_for_arm(exp, CANDIDATE)

    def _boom(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("upstream down", request=request)

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(_boom), base_url="http://upstream"
    ) as upstream_client:
        app = create_app(
            Settings(upstream_base_url="http://upstream"),
            upstream_client=upstream_client,
            recorder=recorder,
            store=store,
        )
        await app.state.experiment_router.refresh()
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://router"
        ) as client:
            with pytest.raises(
                httpx.ConnectError
            ):  # the send failure propagates
                await client.post(
                    "/v1/messages",
                    content=_BODY,
                    headers={
                        "x-ctrlrtn-task": task,
                        "x-ctrlrtn-route": "editor",
                        "content-type": "application/json",
                    },
                )
    await recorder.join()
    await recorder.aclose()
    assert store.traces  # the candidate call did not vanish
    assert store.traces[0].arm == "candidate"
    assert store.traces[0].status_code == 502
    assert store.traces[0].terminal_reason == "send_failure"

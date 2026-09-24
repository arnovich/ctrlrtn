"""The opt-in Anthropic prompt-cache injector (ctrlrtn.gateway.inject)."""

from __future__ import annotations

import json

import httpx

from ctrlrtn.config import Settings
from ctrlrtn.gateway.app import create_app
from ctrlrtn.gateway.inject import inject_cache_control
from ctrlrtn.policy.experiment import ServeDecision
from ctrlrtn.recorder.recorder import Recorder
from ctrlrtn.recorder.store import InMemoryTraceStore
from ctrlrtn.routing import UpstreamRoute

_BIG = "x" * 5000  # over the ~4 KiB minimum-prefix threshold
_SMALL = "tiny system"


def _msgs(**extra) -> bytes:
    body = {
        "model": "claude-sonnet-4-5",
        "max_tokens": 1024,
        "messages": [{"role": "user", "content": "hi"}],
    }
    body.update(extra)
    return json.dumps(body).encode()


def _load(body: bytes) -> dict:
    return json.loads(body)


def test_non_messages_path_is_untouched():
    body = _msgs(system=_BIG)
    assert inject_cache_control("/v1/chat/completions", body) == body


def test_non_json_body_is_untouched():
    assert inject_cache_control("/v1/messages", b"not json") == b"not json"


def test_string_system_becomes_a_cached_text_block():
    out = _load(inject_cache_control("/v1/messages", _msgs(system=_BIG)))
    assert out["system"] == [
        {"type": "text", "text": _BIG, "cache_control": {"type": "ephemeral"}}
    ]
    # untouched fields survive the round-trip
    assert out["model"] == "claude-sonnet-4-5"
    assert out["messages"] == [{"role": "user", "content": "hi"}]


def test_list_system_marks_the_last_block():
    system = [
        {"type": "text", "text": "role"},
        {"type": "text", "text": _BIG},
    ]
    out = _load(inject_cache_control("/v1/messages", _msgs(system=system)))
    assert "cache_control" not in out["system"][0]
    assert out["system"][1]["cache_control"] == {"type": "ephemeral"}


def test_tools_are_cached_when_there_is_no_system():
    tools = [
        {"name": "a", "description": "x", "input_schema": {}},
        {"name": "b", "description": _BIG, "input_schema": {}},
    ]
    out = _load(inject_cache_control("/v1/messages", _msgs(tools=tools)))
    assert "cache_control" not in out["tools"][0]
    assert out["tools"][-1]["cache_control"] == {"type": "ephemeral"}


def test_both_tools_and_system_are_marked_when_both_present():
    # Two segments (≤4 allowed): the system breakpoint caches tools+system; the
    # tools breakpoint keeps tools cached even if a dynamic system tail changes.
    tools = [{"name": "a", "description": _BIG, "input_schema": {}}]
    out = _load(
        inject_cache_control("/v1/messages", _msgs(system=_BIG, tools=tools))
    )
    assert out["system"][-1]["cache_control"] == {"type": "ephemeral"}
    assert out["tools"][-1]["cache_control"] == {"type": "ephemeral"}


def test_nested_tool_result_cache_control_blocks_injection():
    # A cache_control hidden inside a tool_result's own content list still
    # counts toward Anthropic's 4-breakpoint cap; injecting a 5th would 400.
    body = _msgs(
        system=_BIG,
        messages=[
            {
                "role": "user",
                "content": [
                    {
                        "type": "tool_result",
                        "tool_use_id": "t1",
                        "content": [
                            {
                                "type": "text",
                                "text": "r",
                                "cache_control": {"type": "ephemeral"},
                            }
                        ],
                    }
                ],
            }
        ],
    )
    assert inject_cache_control("/v1/messages", body) == body


def test_small_prefix_is_left_uncached():
    body = _msgs(system=_SMALL)
    assert inject_cache_control("/v1/messages", body) == body


def test_existing_system_cache_control_is_left_alone():
    system = [
        {"type": "text", "text": _BIG, "cache_control": {"type": "ephemeral"}}
    ]
    body = _msgs(system=system)
    assert inject_cache_control("/v1/messages", body) == body


def test_existing_message_cache_control_blocks_injection():
    # The app already spends breakpoints on messages; adding one would risk the
    # 4-breakpoint limit, so we must not inject.
    body = _msgs(
        system=_BIG,
        messages=[
            {
                "role": "user",
                "content": [
                    {
                        "type": "text",
                        "text": "ctx",
                        "cache_control": {"type": "ephemeral"},
                    }
                ],
            }
        ],
    )
    assert inject_cache_control("/v1/messages", body) == body


def test_injection_is_idempotent():
    once = inject_cache_control("/v1/messages", _msgs(system=_BIG))
    twice = inject_cache_control("/v1/messages", once)
    assert twice == once  # second pass sees existing cache_control -> no-op


def test_malformed_list_system_falls_back_to_tools():
    # Last system entry isn't a dict; we must not crash — fall to tools.
    tools = [{"name": "a", "description": _BIG, "input_schema": {}}]
    out = _load(
        inject_cache_control(
            "/v1/messages", _msgs(system=["plain", "strings"], tools=tools)
        )
    )
    assert out["tools"][-1]["cache_control"] == {"type": "ephemeral"}


def test_no_system_no_tools_is_untouched():
    body = _msgs()
    assert inject_cache_control("/v1/messages", body) == body


def test_output_is_valid_json_bytes():
    out = inject_cache_control("/v1/messages", _msgs(system=_BIG))
    json.loads(out)  # must not raise


# --- wiring through the gateway --------------------------------------------


async def test_enabled_mutates_upstream_but_records_original(
    streaming_upstream,
):
    record: dict = {}
    upstream = streaming_upstream(record)
    store = InMemoryTraceStore()
    recorder = Recorder(store)
    recorder.start()
    original = _msgs(system=_BIG)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=upstream),
        base_url="http://upstream",
    ) as upstream_client:
        app = create_app(
            Settings(upstream_base_url="http://upstream", inject_cache=True),
            upstream_client=upstream_client,
            recorder=recorder,
        )
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://router"
        ) as client:
            resp = await client.post(
                "/v1/messages",
                content=original,
                headers={"content-type": "application/json"},
            )
    await recorder.join()
    await recorder.aclose()
    assert resp.status_code == 200
    # the upstream saw the injected breakpoint ...
    sent = json.loads(record["body"])
    assert sent["system"][-1]["cache_control"] == {"type": "ephemeral"}
    # ... but the recorded request is the app's original (stable fingerprint)
    assert store.traces[0].request_body == original
    assert b"cache_control" not in store.traces[0].request_body
    # cache injection assigns no arm, so the serve-decision fields stay empty
    assert store.traces[0].experiment_id is None
    assert store.traces[0].arm is None
    assert store.traces[0].served_model is None


async def test_named_anthropic_route_injects_cache_control(streaming_upstream):
    record: dict = {}
    original = _msgs(system=_BIG)
    settings = Settings(
        routes=(
            UpstreamRoute(
                "claude",
                "http://anthropic",
                ("/claude",),
                strip_prefix="/claude",
                api="anthropic",
            ),
        ),
        inject_cache=True,
    )
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=streaming_upstream(record)),
        base_url="http://upstream",
    ) as upstream_client:
        app = create_app(settings, upstream_client=upstream_client)
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://router"
        ) as client:
            response = await client.post(
                "/claude/v1/messages", content=original
            )

    assert response.status_code == 200
    assert record["path"] == "/v1/messages"
    assert json.loads(record["body"])["system"][-1]["cache_control"] == {
        "type": "ephemeral"
    }


async def test_non_anthropic_route_never_injects_cache_control(
    streaming_upstream,
):
    record: dict = {}
    original = _msgs(system=_BIG)
    settings = Settings(
        routes=(
            UpstreamRoute(
                "ollama",
                "http://ollama",
                ("/ollama",),
                strip_prefix="/ollama",
                api="openai",
            ),
        ),
        inject_cache=True,
    )
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=streaming_upstream(record)),
        base_url="http://upstream",
    ) as upstream_client:
        app = create_app(settings, upstream_client=upstream_client)
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://router"
        ) as client:
            response = await client.post(
                "/ollama/v1/messages", content=original
            )

    assert response.status_code == 200
    assert record["body"] == original


async def test_proxy_records_the_serve_decision(streaming_upstream):
    record: dict = {}
    upstream = streaming_upstream(record)
    store = InMemoryTraceStore()
    recorder = Recorder(store)
    recorder.start()
    original = b'{"model": "claude-opus-4", "messages": []}'
    swapped = b'{"model": "claude-haiku-4-5", "messages": []}'

    def swap(path, headers, body, upstream_api=None):
        return swapped, ServeDecision(
            experiment_id="exp:1",
            use_case_key="fp:editor",
            task_id="edition-1",
            arm="candidate",
            served_model="claude-haiku-4-5",
            original_model="claude-opus-4",
        )

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=upstream),
        base_url="http://upstream",
    ) as upstream_client:
        app = create_app(
            Settings(upstream_base_url="http://upstream"),
            upstream_client=upstream_client,
            recorder=recorder,
        )
        app.state.request_decider = swap
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://router"
        ) as client:
            resp = await client.post(
                "/v1/messages",
                content=original,
                headers={"content-type": "application/json"},
            )
    await recorder.join()
    await recorder.aclose()
    assert resp.status_code == 200
    assert record["body"] == swapped  # upstream saw the swapped model
    trace = store.traces[0]
    assert trace.request_body == original  # recorded the original (fingerprint)
    assert trace.experiment_id == "exp:1"
    assert trace.arm == "candidate"
    assert trace.served_model == "claude-haiku-4-5"


async def test_proxy_fails_open_on_a_non_bytes_body(streaming_upstream):
    # A hook that returns (None, decision) must forward the ORIGINAL body (never
    # an empty one) and drop the arm it claimed but never actually served.
    record: dict = {}
    upstream = streaming_upstream(record)
    store = InMemoryTraceStore()
    recorder = Recorder(store)
    recorder.start()
    original = b'{"model": "claude-opus-4"}'

    def bad(path, headers, body, upstream_api=None):
        return None, ServeDecision(
            experiment_id="exp:1",
            use_case_key="fp:e",
            task_id="t1",
            arm="candidate",
            served_model="claude-haiku-4-5",
            original_model="claude-opus-4",
        )

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=upstream),
        base_url="http://upstream",
    ) as upstream_client:
        app = create_app(
            Settings(upstream_base_url="http://upstream"),
            upstream_client=upstream_client,
            recorder=recorder,
        )
        app.state.request_decider = bad
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://router"
        ) as client:
            resp = await client.post(
                "/v1/messages",
                content=original,
                headers={"content-type": "application/json"},
            )
    await recorder.join()
    await recorder.aclose()
    assert resp.status_code == 200
    assert record["body"] == original  # original forwarded, not empty
    trace = store.traces[0]
    assert trace.arm is None  # the never-served arm was dropped
    assert trace.experiment_id is None
    assert trace.served_model is None


async def test_decide_reads_headers_case_insensitively(streaming_upstream):
    # The proxy passes case-insensitive headers, so a hook can read x-ctrlrtn-task
    # no matter how the client cased it (the slice-4 arm-lookup contract).
    record: dict = {}
    upstream = streaming_upstream(record)
    seen: dict = {}

    def capture(path, headers, body, upstream_api=None):
        seen["task"] = headers.get("x-ctrlrtn-task")  # lowercase lookup
        seen["api"] = upstream_api
        return body, None

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=upstream),
        base_url="http://upstream",
    ) as upstream_client:
        app = create_app(
            Settings(upstream_base_url="http://upstream"),
            upstream_client=upstream_client,
        )
        app.state.request_decider = capture
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://router"
        ) as client:
            await client.post(
                "/v1/messages",
                content=b"{}",
                headers={
                    "X-Ctrlrtn-Task": "edition-9",  # title-cased by the client
                    "content-type": "application/json",
                },
            )
    assert seen["task"] == "edition-9"
    assert seen["api"] is None


async def test_proxy_is_fail_open_if_decide_raises(streaming_upstream):
    record: dict = {}
    upstream = streaming_upstream(record)
    original = _msgs(system=_BIG)

    def boom(path, headers, body, upstream_api=None):
        raise RuntimeError("decide exploded")

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=upstream),
        base_url="http://upstream",
    ) as upstream_client:
        app = create_app(
            Settings(upstream_base_url="http://upstream"),
            upstream_client=upstream_client,
        )
        app.state.request_decider = boom
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://router"
        ) as client:
            resp = await client.post(
                "/v1/messages",
                content=original,
                headers={"content-type": "application/json"},
            )
    assert resp.status_code == 200
    assert record["body"] == original  # original forwarded despite the crash


async def test_disabled_by_default_is_byte_faithful(streaming_upstream):
    record: dict = {}
    upstream = streaming_upstream(record)
    original = _msgs(system=_BIG)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=upstream),
        base_url="http://upstream",
    ) as upstream_client:
        app = create_app(  # inject_cache defaults False
            Settings(upstream_base_url="http://upstream"),
            upstream_client=upstream_client,
        )
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://router"
        ) as client:
            await client.post(
                "/v1/messages",
                content=original,
                headers={"content-type": "application/json"},
            )
    assert record["body"] == original  # untouched

"""M1: one endpoint fronts many providers, routed per request by path."""

from __future__ import annotations

import json

import httpx
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import Response, StreamingResponse
from starlette.routing import Route

from ctrlrtn.config import Settings, load_settings
from ctrlrtn.gateway.app import create_app
from ctrlrtn.recorder.memory_store import InMemoryTraceStore
from ctrlrtn.recorder.recorder import Recorder
from ctrlrtn.routing import (
    ANTHROPIC_BASE_URL,
    OPENAI_BASE_URL,
    UpstreamRoute,
    UpstreamRouter,
    default_routes,
)
from ctrlrtn.telemetry.enrich import enrich_trace


def test_router_resolves_by_path():
    router = UpstreamRouter(routes=default_routes())
    assert router.resolve("/v1/messages") == ANTHROPIC_BASE_URL
    assert router.resolve("/v1/complete") == ANTHROPIC_BASE_URL
    assert router.resolve("/v1/chat/completions") == OPENAI_BASE_URL
    assert router.resolve("/v1/embeddings") == OPENAI_BASE_URL
    assert router.resolve("/v1/models") is None  # ambiguous -> unroutable


def test_single_upstream_router_catches_all():
    router = UpstreamRouter(default="http://single")
    assert router.resolve("/v1/messages") == "http://single"
    assert router.resolve("/anything") == "http://single"


def test_named_route_strips_its_client_prefix():
    route = UpstreamRoute(
        "ollama",
        "http://ollama",
        ("/ollama",),
        strip_prefix="/ollama",
        api="openai",
    )
    router = UpstreamRouter(routes=(route,))

    target = router.resolve_request("/ollama/v1/chat/completions")

    assert target is not None
    assert target.name == "ollama"
    assert target.api == "openai"
    assert target.base_url == "http://ollama"
    assert target.path == "/v1/chat/completions"
    assert router.resolve("/ollama/v1/chat/completions") == "http://ollama"


def test_named_route_matches_a_path_segment_not_a_text_prefix():
    route = UpstreamRoute(
        "ollama",
        "http://ollama",
        ("/ollama",),
        strip_prefix="/ollama",
    )
    router = UpstreamRouter(routes=(route,))

    assert router.resolve_request("/ollama") is not None
    assert router.resolve_request("/ollama/").path == "/"
    assert router.resolve_request("/ollamatic/v1/chat/completions") is None


def test_router_resolves_an_explicit_provider_without_a_client_mount():
    router = UpstreamRouter(
        routes=(
            UpstreamRoute(
                "ollama",
                "http://ollama",
                ("/ollama",),
                strip_prefix="/ollama",
                api="openai",
                free=True,
            ),
        )
    )

    target = router.resolve_provider("ollama", "/v1/chat/completions")

    assert target is not None
    assert target.base_url == "http://ollama"
    assert target.path == "/v1/chat/completions"
    assert target.api == "openai"
    assert target.free is True
    assert router.resolve_provider("missing", "/v1/chat/completions") is None


def test_settings_resolver_modes():
    single = Settings(upstream_base_url="http://x").resolver()
    assert single.resolve("/v1/messages") == "http://x"
    assert single.resolve("/v1/chat/completions") == "http://x"

    multi = Settings().resolver()
    assert multi.resolve("/v1/messages") == ANTHROPIC_BASE_URL
    assert multi.resolve("/v1/chat/completions") == OPENAI_BASE_URL
    assert multi.resolve("/v1/models") is None


def test_load_settings_single_vs_multi(monkeypatch):
    monkeypatch.setenv("CTRLRTN_UPSTREAM", "http://only")
    assert load_settings().resolver().resolve("/v1/messages") == "http://only"

    monkeypatch.delenv("CTRLRTN_UPSTREAM", raising=False)
    monkeypatch.setenv("CTRLRTN_ANTHROPIC_UPSTREAM", "http://anthropic-x")
    resolver = load_settings().resolver()
    assert resolver.resolve("/v1/messages") == "http://anthropic-x"
    assert resolver.resolve("/v1/chat/completions") == OPENAI_BASE_URL


async def test_routes_to_matching_provider_and_404s_unknown(streaming_upstream):
    record: dict = {}
    upstream = streaming_upstream(record)
    settings = Settings(
        routes=(
            UpstreamRoute("anthropic", "http://anthropic", ("/v1/messages",)),
            UpstreamRoute("openai", "http://openai", ("/v1/chat/completions",)),
        )
    )
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=upstream),
        base_url="http://upstream",
    ) as upstream_client:
        app = create_app(settings, upstream_client=upstream_client)
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="http://router",
        ) as client:
            # The path picks the upstream: the forwarded Host is the chosen
            # provider's, proving it went to the right place.
            await client.post("/v1/messages", content=b"{}")
            assert record["headers"]["host"] == "anthropic"

            await client.post("/v1/chat/completions", content=b"{}")
            assert record["headers"]["host"] == "openai"

            # An unrouted path fails locally; the upstream is never called.
            record.clear()
            resp = await client.get("/v1/models")
            assert resp.status_code == 404
            assert record == {}


async def test_named_route_forwards_the_stripped_path(streaming_upstream):
    record: dict = {}
    upstream = streaming_upstream(record)
    settings = Settings(
        routes=(
            UpstreamRoute(
                "ollama",
                "http://ollama",
                ("/ollama",),
                strip_prefix="/ollama",
                api="openai",
            ),
        )
    )
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=upstream),
        base_url="http://upstream",
    ) as upstream_client:
        app = create_app(settings, upstream_client=upstream_client)
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="http://router",
        ) as client:
            response = await client.post(
                "/ollama/v1/chat/completions?stream=false", content=b"{}"
            )

    assert response.status_code == 200
    assert record["headers"]["host"] == "ollama"
    assert record["path"] == "/v1/chat/completions"
    assert record["query"] == "stream=false"


async def test_ollama_compatibility_bytes_record_usage_and_zero_cost():
    async def chat(request: Request) -> Response:
        payload = await request.json()
        if not payload.get("stream"):
            return Response(
                json.dumps(
                    {
                        "model": "qwen3:14b",
                        "choices": [{"message": {"content": "ok"}}],
                        "usage": {
                            "prompt_tokens": 12,
                            "completion_tokens": 7,
                            "total_tokens": 19,
                        },
                    }
                ),
                media_type="application/json",
            )

        async def chunks():
            yield b'data: {"model":"qwen3:14b","choices":[],"usage":null}\n\n'
            yield (
                b'data: {"model":"qwen3:14b","choices":[],"usage":'
                b'{"prompt_tokens":40,"completion_tokens":11,'
                b'"total_tokens":51}}\n\n'
            )
            yield b"data: [DONE]\n\n"

        return StreamingResponse(chunks(), media_type="text/event-stream")

    upstream = Starlette(
        routes=[Route("/v1/chat/completions", chat, methods=["POST"])]
    )
    store = InMemoryTraceStore()
    recorder = Recorder(store, enrich=enrich_trace)
    recorder.start()
    settings = Settings(
        routes=(
            UpstreamRoute(
                "ollama",
                "http://ollama",
                ("/ollama",),
                strip_prefix="/ollama",
                api="openai",
                free=True,
            ),
        )
    )
    body = {
        "model": "qwen3:14b",
        "messages": [{"role": "system", "content": "Classify this."}],
    }
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=upstream),
        base_url="http://upstream",
    ) as upstream_client:
        app = create_app(
            settings, upstream_client=upstream_client, recorder=recorder
        )
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="http://router",
        ) as client:
            assert (
                await client.post("/ollama/v1/chat/completions", json=body)
            ).status_code == 200
            streamed = await client.post(
                "/ollama/v1/chat/completions",
                json={
                    **body,
                    "stream": True,
                    "stream_options": {"include_usage": True},
                },
            )
            assert streamed.status_code == 200
            assert streamed.content.endswith(b"data: [DONE]\n\n")
    await recorder.join()
    await recorder.aclose()

    assert len(store.traces) == 2
    assert [trace.provider for trace in store.traces] == ["ollama", "ollama"]
    assert [trace.input_tokens for trace in store.traces] == [12, 40]
    assert [trace.output_tokens for trace in store.traces] == [7, 11]
    assert [trace.cost_usd for trace in store.traces] == [0.0, 0.0]
    assert all(
        trace.use_case_key and trace.use_case_key.startswith("fp:")
        for trace in store.traces
    )


async def test_named_route_cannot_escape_into_control_plane(streaming_upstream):
    record: dict = {}
    settings = Settings(
        routes=(
            UpstreamRoute(
                "ollama",
                "http://ollama",
                ("/ollama",),
                strip_prefix="/ollama",
                api="openai",
            ),
        )
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
                "/ollama/ctrlrtn/outcome", content=b"{}"
            )

    assert response.status_code == 404
    assert record == {}


async def test_fingerprint_is_stable_across_named_providers(streaming_upstream):
    store = InMemoryTraceStore()
    recorder = Recorder(store, enrich=enrich_trace)
    recorder.start()
    settings = Settings(
        routes=(
            UpstreamRoute(
                "openai",
                "http://openai",
                ("/openai",),
                strip_prefix="/openai",
                api="openai",
            ),
            UpstreamRoute(
                "ollama",
                "http://ollama",
                ("/ollama",),
                strip_prefix="/ollama",
                api="openai",
                free=True,
            ),
        )
    )
    body = (
        b'{"model":"same-model","messages":['
        b'{"role":"system","content":"You are a classifier."}]}'
    )
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=streaming_upstream({})),
        base_url="http://upstream",
    ) as upstream_client:
        app = create_app(
            settings, upstream_client=upstream_client, recorder=recorder
        )
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://router"
        ) as client:
            await client.post("/openai/v1/chat/completions", content=body)
            await client.post("/ollama/v1/chat/completions", content=body)
    await recorder.join()
    await recorder.aclose()

    assert len(store.traces) == 2
    assert [trace.provider for trace in store.traces] == ["openai", "ollama"]
    assert [trace.provider_free for trace in store.traces] == [False, True]
    assert store.traces[1].cost_usd == 0.0
    assert store.traces[0].use_case_key is not None
    assert store.traces[0].use_case_key == store.traces[1].use_case_key

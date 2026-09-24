"""M0: the gateway is a byte-faithful, fail-open streaming pass-through."""

from __future__ import annotations

import httpx
import pytest

from ctrlrtn.config import Settings
from ctrlrtn.gateway.app import create_app


async def test_kill_switch_blocks_before_contacting_the_upstream(
    streaming_upstream,
):
    record: dict = {}
    app = create_app(
        Settings(upstream_base_url="http://upstream", kill_switch=True),
        upstream_client=httpx.AsyncClient(
            transport=httpx.ASGITransport(app=streaming_upstream(record)),
            base_url="http://upstream",
        ),
    )
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://router"
    ) as client:
        response = await client.post("/v1/chat/completions", content=b"{}")

    assert response.status_code == 503
    assert record == {}


async def test_app_closes_the_upstream_client_it_creates():
    app = create_app(Settings(upstream_base_url="http://upstream"))
    upstream_client = app.state.upstream_client

    async with app.router.lifespan_context(app):
        assert not upstream_client.is_closed

    assert upstream_client.is_closed


async def test_app_leaves_an_injected_upstream_client_open():
    upstream_client = httpx.AsyncClient()
    app = create_app(
        Settings(upstream_base_url="http://upstream"),
        upstream_client=upstream_client,
    )

    async with app.router.lifespan_context(app):
        pass

    assert not upstream_client.is_closed
    await upstream_client.aclose()


async def test_app_closes_an_owned_store():
    class Store:
        """A minimal ServingRepository: the gateway needs every reader."""

        def __init__(self) -> None:
            self.closed = False

        def running_experiments(self):
            return {}

        def routes(self):
            return []

        def workflow_routes(self):
            return []

        def fallbacks(self):
            return []

        def control_revision(self):
            return None

        def close(self) -> None:
            self.closed = True

    store = Store()
    app = create_app(
        Settings(upstream_base_url="http://upstream"),
        store=store,
        close_store_on_shutdown=True,
    )

    async with app.router.lifespan_context(app):
        pass

    assert store.closed


async def test_owned_store_must_be_closeable():
    class Store:
        def running_experiments(self):
            return {}

        def routes(self):
            return []

    with pytest.raises(TypeError, match="owned store must define close"):
        create_app(
            Settings(upstream_base_url="http://upstream"),
            store=Store(),
            close_store_on_shutdown=True,
        )


async def test_post_streams_through_byte_faithfully(streaming_upstream):
    record: dict = {}
    upstream = streaming_upstream(record)

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=upstream),
        base_url="http://upstream",
    ) as upstream_client:
        app = create_app(
            Settings(upstream_base_url="http://upstream"),
            upstream_client=upstream_client,
        )
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="http://router",
        ) as client:
            resp = await client.post(
                "/v1/chat/completions?foo=bar",
                content=b'{"model":"gpt-4o"}',
                headers={
                    "authorization": "Bearer sk-test",
                    "anthropic-version": "2023-06-01",
                    "content-type": "application/json",
                },
            )

    # The request reached the upstream unchanged (method, path, query, body,
    # and the client's own headers — including its provider key).
    assert record["method"] == "POST"
    assert record["path"] == "/v1/chat/completions"
    assert record["query"] == "foo=bar"
    assert record["body"] == b'{"model":"gpt-4o"}'
    assert record["headers"]["authorization"] == "Bearer sk-test"
    assert record["headers"]["anthropic-version"] == "2023-06-01"

    # The streamed response came back intact, in order, with its headers.
    assert resp.status_code == 200
    assert resp.headers["x-upstream"] == "1"
    assert resp.headers["content-type"].startswith("text/event-stream")
    assert resp.text == "data: a\n\ndata: b\n\ndata: [DONE]\n\n"


async def test_non_2xx_status_passes_through(status_upstream):
    # Fail-open means we never swallow or rewrite the upstream's own errors.
    upstream = status_upstream(503, b"upstream down")

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=upstream),
        base_url="http://upstream",
    ) as upstream_client:
        app = create_app(
            Settings(upstream_base_url="http://upstream"),
            upstream_client=upstream_client,
        )
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="http://router",
        ) as client:
            resp = await client.get("/v1/models")

    assert resp.status_code == 503
    assert resp.text == "upstream down"

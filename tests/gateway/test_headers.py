"""Transparent proxy header transport preserves HTTP field semantics."""

import httpx
from starlette.applications import Starlette
from starlette.responses import Response
from starlette.routing import Route

from ctrlrtn.config import Settings
from ctrlrtn.gateway.app import create_app
from ctrlrtn.gateway.proxy import (
    _forward_request_headers,
    _forward_response_headers,
)


def test_connection_nominated_fields_are_not_forwarded():
    headers = httpx.Headers(
        [
            (b"connection", b"x-internal-hop"),
            (b"x-internal-hop", b"private"),
            (b"x-repeat", b"one"),
            (b"x-repeat", b"two"),
        ]
    )

    forwarded = _forward_request_headers(headers)

    assert "connection" not in forwarded
    assert "x-internal-hop" not in forwarded
    assert forwarded.get_list("x-repeat") == ["one", "two"]


def test_repeated_response_fields_are_not_coalesced():
    headers = httpx.Headers(
        [
            (b"set-cookie", b"session=a; Path=/"),
            (b"set-cookie", b"prefs=b; Path=/"),
            (b"connection", b"x-internal-hop"),
            (b"x-internal-hop", b"private"),
        ]
    )

    forwarded = _forward_response_headers(headers)

    assert forwarded.get_list("set-cookie") == [
        "session=a; Path=/",
        "prefs=b; Path=/",
    ]
    assert "x-internal-hop" not in forwarded


async def test_gateway_preserves_repeated_response_fields_end_to_end():
    async def upstream(request) -> Response:
        response = Response(b"ok")
        response.raw_headers = [
            (b"set-cookie", b"session=a; Path=/"),
            (b"set-cookie", b"prefs=b; Path=/"),
            (b"connection", b"x-internal-hop"),
            (b"x-internal-hop", b"private"),
            (b"content-length", b"2"),
        ]
        return response

    upstream_app = Starlette(routes=[Route("/{path:path}", upstream)])
    upstream_client = httpx.AsyncClient(
        transport=httpx.ASGITransport(app=upstream_app),
        base_url="http://upstream",
    )
    app = create_app(
        Settings(upstream_base_url="http://upstream"),
        upstream_client=upstream_client,
    )

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://router"
    ) as client:
        response = await client.get("/cookies")

    assert response.content == b"ok"
    assert response.headers.get_list("set-cookie") == [
        "session=a; Path=/",
        "prefs=b; Path=/",
    ]
    assert "x-internal-hop" not in response.headers
    await upstream_client.aclose()

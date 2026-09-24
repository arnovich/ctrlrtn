"""Shared test fixtures.

The mock upstream is a tiny ASGI app the gateway's httpx client is pointed at
(via ``ASGITransport``), so the whole proxy path is exercised in-process with
no network and no real provider keys.
"""

from __future__ import annotations

from collections.abc import Callable

import pytest
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import Response, StreamingResponse
from starlette.routing import Route


def pytest_addoption(parser: pytest.Parser) -> None:
    """Add an opt-in CI guard against silently skipped coverage."""

    parser.addoption(
        "--fail-on-skip",
        action="store_true",
        default=False,
        help="fail the session if any collected test is skipped",
    )


def pytest_sessionfinish(
    session: pytest.Session, exitstatus: int | pytest.ExitCode
) -> None:
    """Turn unexpected skips into a failing CI result when requested."""

    if not session.config.getoption("--fail-on-skip"):
        return
    reporter = session.config.pluginmanager.get_plugin("terminalreporter")
    if reporter is not None and reporter.stats.get("skipped"):
        session.exitstatus = pytest.ExitCode.TESTS_FAILED


def _streaming_upstream(record: dict) -> Starlette:
    """An upstream that records the request it received and streams an
    SSE-style response back in several chunks."""

    async def echo(request: Request) -> StreamingResponse:
        record["method"] = request.method
        record["path"] = request.url.path
        record["query"] = request.url.query
        record["headers"] = dict(request.headers)
        record["body"] = await request.body()

        async def body():
            for chunk in (
                b"data: a\n\n",
                b"data: b\n\n",
                b"data: [DONE]\n\n",
            ):
                yield chunk

        return StreamingResponse(
            body(),
            media_type="text/event-stream",
            headers={"x-upstream": "1"},
        )

    return Starlette(
        routes=[Route("/{path:path}", echo, methods=["GET", "POST"])]
    )


@pytest.fixture
def streaming_upstream() -> Callable[[dict], Starlette]:
    """Factory: ``streaming_upstream(record)`` -> mock upstream app."""
    return _streaming_upstream


@pytest.fixture
def status_upstream() -> Callable[[int, bytes], Starlette]:
    """Factory: ``status_upstream(code, body)`` -> upstream returning that
    status verbatim (to check non-2xx pass-through)."""

    def factory(status_code: int, body: bytes = b"err") -> Starlette:
        async def handler(request: Request) -> Response:
            return Response(body, status_code=status_code)

        return Starlette(
            routes=[Route("/{path:path}", handler, methods=["GET", "POST"])]
        )

    return factory

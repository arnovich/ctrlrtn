"""Property tests for proxy header preservation and exclusion invariants."""

from __future__ import annotations

import httpx
from hypothesis import given, settings
from hypothesis import strategies as st
from starlette.datastructures import Headers

from ctrlrtn.gateway.proxy.headers import (
    _forward_request_headers,
    _forward_response_headers,
)

_FIXED_HOPS = {
    "connection",
    "keep-alive",
    "proxy-authenticate",
    "proxy-authorization",
    "proxy-connection",
    "te",
    "trailer",
    "trailers",
    "transfer-encoding",
    "upgrade",
}
_NAMES = st.text(
    alphabet="abcdefghijklmnopqrstuvwxyz", min_size=3, max_size=12
).filter(lambda value: value not in _FIXED_HOPS)
_VALUES = st.text(
    alphabet="abcdefghijklmnopqrstuvwxyz0123456789-_=; /",
    min_size=0,
    max_size=30,
)


@settings(max_examples=100)
@given(
    nominated=_NAMES,
    safe=_NAMES,
    nominated_value=_VALUES,
    first=_VALUES,
    second=_VALUES,
)
def test_response_filter_preserves_safe_duplicates_and_removes_nominated_hops(
    nominated: str,
    safe: str,
    nominated_value: str,
    first: str,
    second: str,
):
    if safe == nominated:
        safe = f"safe-{safe}"
    headers = httpx.Headers(
        [
            (b"connection", nominated.encode()),
            (nominated.encode(), nominated_value.encode()),
            (safe.encode(), first.encode()),
            (safe.encode(), second.encode()),
        ]
    )

    forwarded = _forward_response_headers(headers)

    assert "connection" not in forwarded
    assert nominated not in forwarded
    assert forwarded.get_list(safe) == [first, second]


@settings(max_examples=100)
@given(secret=_VALUES, safe_value=_VALUES)
def test_request_filter_never_forwards_credentials_or_router_metadata(
    secret: str, safe_value: str
):
    headers = Headers(
        raw=[
            (b"host", b"router"),
            (b"authorization", secret.encode()),
            (b"x-api-key", secret.encode()),
            (b"x-ctrlrtn-task", b"task-secret"),
            (b"x-safe", safe_value.encode()),
        ]
    )

    forwarded = _forward_request_headers(headers, strip_credentials=True)

    assert "authorization" not in forwarded
    assert "x-api-key" not in forwarded
    assert "x-ctrlrtn-task" not in forwarded
    assert "host" not in forwarded
    assert forwarded.get_list("x-safe") == [safe_value]


def test_comma_hops_and_framing_are_removed_but_metadata_is_preserved():
    response_headers = httpx.Headers(
        [
            (b"connection", b"x-first-hop, x-second-hop"),
            (b"x-first-hop", b"private-one"),
            (b"x-second-hop", b"private-two"),
            (b"content-length", b"99"),
            (b"x-ctrlrtn-upstream", b"visible"),
        ]
    )
    response = _forward_response_headers(response_headers)
    assert "x-first-hop" not in response
    assert "x-second-hop" not in response
    assert "content-length" not in response
    assert response["x-ctrlrtn-upstream"] == "visible"

    request = _forward_request_headers(
        Headers(
            raw=[
                (b"host", b"router"),
                (b"content-length", b"99"),
                (b"x-safe", b"visible"),
            ]
        )
    )
    assert "content-length" not in request
    assert request["x-safe"] == "visible"

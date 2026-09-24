"""Credential-safe, byte-faithful upstream header transport."""

from __future__ import annotations

from typing import Protocol

import httpx

from ctrlrtn.recorder.redaction import CREDENTIAL_HEADERS, redact_headers
from ctrlrtn.workflow.identity import CTRLRTN_HEADER_PREFIX

# Hop-by-hop headers (RFC 7230 §6.1) must not be forwarded by a proxy.
_HOP_BY_HOP = frozenset(
    {
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
)


class _StarletteHeaders(Protocol):
    """Starlette's request ``Headers``: raw fields plus ``getlist``."""

    @property
    def raw(self) -> list[tuple[bytes, bytes]]: ...

    def getlist(self, key: str) -> list[str]: ...


# Request headers arrive from Starlette and response headers from httpx; the
# two spell the repeated-value accessor differently. httpx is named as the
# concrete class so the dispatch below is one cheap type check per request.
_RawHeaders = _StarletteHeaders | httpx.Headers


# Credential redaction lives in ctrlrtn.recorder.redaction: applied at
# capture, not at display — display masking alone would still write the secret
# to disk.
_recordable_headers = redact_headers


def _values(headers: _RawHeaders, name: str) -> list[str]:
    if isinstance(headers, httpx.Headers):
        return headers.get_list(name)
    return headers.getlist(name)


def _hop_by_hop(headers: _RawHeaders) -> set[str]:
    """Return fixed and Connection-nominated hop-by-hop field names."""
    nominated = {
        token.strip().lower()
        for value in _values(headers, "connection")
        for token in value.split(",")
        if token.strip()
    }
    return set(_HOP_BY_HOP) | nominated


def _filtered(
    headers: _RawHeaders,
    skip: set[str] | frozenset[str],
    *,
    strip_ctrlrtn: bool = False,
) -> httpx.Headers:
    """Filter raw fields without coalescing repeated header values."""
    return httpx.Headers(
        [
            (key.lower(), value)
            for key, value in headers.raw
            if key.decode("ascii").lower() not in skip
            and (
                not strip_ctrlrtn
                or not key.lower().startswith(CTRLRTN_HEADER_PREFIX.encode())
            )
        ]
    )


def _forward_request_headers(
    headers: _RawHeaders, *, strip_credentials: bool = False
) -> httpx.Headers:
    # Drop hop-by-hop, ``host`` (httpx sets it from the target URL) and
    # ``content-length`` (httpx recomputes it from the body we send). The
    # client's own provider key rides through untouched unless a serving
    # decision switches providers; a baseline credential must never be sent to
    # a different upstream.
    skip = _hop_by_hop(headers) | {"host", "content-length"}
    if strip_credentials:
        skip |= CREDENTIAL_HEADERS
    return _filtered(headers, skip, strip_ctrlrtn=True)


def _forward_response_headers(headers: httpx.Headers) -> httpx.Headers:
    # Drop hop-by-hop and ``content-length`` (we re-stream the body, so the
    # ASGI server sets the framing). ``content-encoding`` is preserved because
    # we forward the raw, still-encoded bytes via ``aiter_raw``.
    skip = _hop_by_hop(headers) | {"content-length"}
    return _filtered(headers, skip)

"""Credential redaction for RECORDED traffic — capture-time, not display-time.

Credentials are used to forward and never persisted: the recorded trace (and
therefore the SQLite file, its backups, and every console/CLI read of it)
must not contain them. Two carriers are covered:

* **headers** — ``authorization`` / ``x-api-key`` / cookies etc.;
* **query parameters** — some providers authenticate as ``?key=...``
  (e.g. Google-style APIs); the recorded query string redacts the values of
  credential-named params, while the forwarded URL keeps them.

The guarantee is a *named list*, not magic: an upstream using an exotic
credential carrier (a custom header name, a body field) is out of scope —
the README states this next to the guarantee.

This lives with the recorder because the promise it makes is about what
reaches storage. The gateway applies it on the way in, and the maintenance
scrub re-applies it to rows captured before a carrier was covered; forwarding
concerns live in ``gateway/redact.py``.
"""

from __future__ import annotations

from urllib.parse import parse_qsl, urlencode

REDACTED = "[redacted]"

CREDENTIAL_HEADERS = frozenset(
    {
        "authorization",
        "proxy-authorization",
        "x-api-key",
        "api-key",
        "cookie",
        "set-cookie",
    }
)

CREDENTIAL_PARAMS = frozenset(
    {"key", "api_key", "apikey", "api-key", "access_token", "token"}
)


def redact_headers(headers) -> dict[str, str]:
    """A copy of ``headers`` with credential values replaced. Accepts any
    (key, value) mapping/items provider (httpx Headers, dict)."""
    return {
        k: (REDACTED if k.lower() in CREDENTIAL_HEADERS else v)
        for k, v in headers.items()
    }


def redact_query(query: str) -> str:
    """The query string with credential-named params' values replaced,
    other params byte-preserved as closely as urlencode allows. "" stays ""."""
    if not query:
        return query
    pairs = parse_qsl(query, keep_blank_values=True)
    if not any(name.lower() in CREDENTIAL_PARAMS for name, _ in pairs):
        return query  # untouched — the common case records verbatim
    return urlencode(
        [
            (
                name,
                REDACTED if name.lower() in CREDENTIAL_PARAMS else value,
            )
            for name, value in pairs
        ]
    )

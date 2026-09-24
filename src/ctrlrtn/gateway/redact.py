"""Credential handling on the FORWARDED request — transport, not storage.

The recorded-trace guarantee lives in ``recorder/redaction.py``; this module
covers the other direction, stripping credential-named query parameters from
the URL the router sends upstream. It shares that module's named-carrier
vocabulary so the two halves cannot drift apart.
"""

from __future__ import annotations

from urllib.parse import parse_qsl, urlencode

from ctrlrtn.recorder.redaction import CREDENTIAL_PARAMS


def strip_query_credentials(query: str) -> str:
    """Remove credential-named parameters from a forwarded query string."""
    if not query:
        return query
    pairs = parse_qsl(query, keep_blank_values=True)
    if not any(name.lower() in CREDENTIAL_PARAMS for name, _ in pairs):
        return query
    return urlencode(
        [
            (name, value)
            for name, value in pairs
            if name.lower() not in CREDENTIAL_PARAMS
        ]
    )

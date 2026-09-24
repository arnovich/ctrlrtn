"""Extract token usage, the requested model, and the served (response-echoed)
model from a recorded call.

Token usage is the spend signal. It lives in different places per provider and
per streaming mode, so this handles all four:
  - OpenAI non-streaming:   ``usage.prompt_tokens`` / ``usage.completion_tokens``
  - Anthropic non-streaming: ``usage.input_tokens`` / ``usage.output_tokens``
  - OpenAI streaming:        a final SSE chunk carries top-level ``usage``
                             (only when ``stream_options.include_usage``)
  - Anthropic streaming:     ``message_start`` carries input tokens,
                             ``message_delta`` carries the final output tokens

Cache tokens are normalized so ``input_tokens`` always means *uncached* input
and cache reads/writes are separate: Anthropic already reports input separately
from ``cache_read_input_tokens`` / ``cache_creation_input_tokens``, while OpenAI's
``prompt_tokens`` *includes* ``cached_tokens`` (so cached is subtracted out).
This lets pricing bill cached input at its discounted rate. When usage is absent
the tokens are left ``None`` rather than guessed.
"""

from __future__ import annotations

import gzip
import json
import zlib
from dataclasses import dataclass
from typing import Any


@dataclass
class Usage:
    """Token counts extracted from one call. ``input_tokens`` is uncached
    input; cache reads and writes are kept separate so pricing can discount
    them. ``None`` means the provider reported nothing, never zero."""

    input_tokens: int | None = None  # uncached input
    output_tokens: int | None = None
    cache_read_tokens: int | None = None  # cached input read (discounted)
    cache_write_tokens: int | None = None  # cache creation (Anthropic)


def decode_body(
    body: bytes | None, content_encoding: str | None = None
) -> bytes:
    """Decompress a response body for inspection. The proxy streams (and stores)
    the raw, still-encoded bytes to stay byte-faithful; usage extraction and the
    `show` command need the decoded form. Falls back to the raw bytes on any
    error or unknown encoding."""
    if not body:
        return body or b""
    enc = (content_encoding or "").lower().strip()
    try:
        if enc == "gzip" or body[:2] == b"\x1f\x8b":  # magic-byte fallback
            return gzip.decompress(body)
        if enc == "deflate":
            try:
                return zlib.decompress(body)
            except zlib.error:
                return zlib.decompress(body, -zlib.MAX_WBITS)
        if enc == "br":
            import brotli  # optional dependency

            return brotli.decompress(body)
    except Exception:
        return body
    return body


def extract_model(request_body: bytes) -> str | None:
    payload = _try_json(request_body)
    if payload is None:
        return None
    model = payload.get("model")
    return model if isinstance(model, str) else None


def extract_usage(response_body: bytes) -> Usage:
    payload = _try_json(response_body)
    if payload is not None:
        return _normalize_usage(payload.get("usage"))
    return _usage_from_sse(response_body)


def extract_response_model(response_body: bytes) -> str | None:
    """The model the provider actually served, echoed in the response — the
    ground-truth billed model, which can differ from the requested one (an alias,
    a deprecation redirect, a fallback). Both providers echo it at the top level;
    streaming carries it in Anthropic's ``message_start.message.model`` or an
    OpenAI chunk's top-level ``model``. Recomputable from the stored response
    bytes, so pricing stays reenrich-safe. ``None`` when absent/unparseable."""
    payload = _try_json(response_body)
    if payload is not None:
        model = payload.get("model")
        return model if isinstance(model, str) else None
    return _model_from_sse(response_body)


def _model_from_sse(body: bytes) -> str | None:
    for raw in body.split(b"\n"):
        line = raw.strip()
        if not line.startswith(b"data:"):
            continue
        data = line[len(b"data:") :].strip()
        if data in (b"", b"[DONE]"):
            continue
        try:
            event = json.loads(data)
        except (json.JSONDecodeError, ValueError):
            continue
        if not isinstance(event, dict):
            continue
        model = event.get("model")  # OpenAI chunk
        if isinstance(model, str):
            return model
        message = event.get("message")  # Anthropic message_start
        if isinstance(message, dict) and isinstance(message.get("model"), str):
            return message["model"]
    return None


def _try_json(body: bytes) -> dict[str, Any] | None:
    try:
        payload = json.loads(body)
    except (json.JSONDecodeError, ValueError):
        return None
    return payload if isinstance(payload, dict) else None


def _normalize_usage(usage: Any) -> Usage:
    """Map a provider usage object to uncached input + cache read/write."""
    if not isinstance(usage, dict):
        return Usage()
    if "prompt_tokens" in usage:  # OpenAI: prompt_tokens includes cached
        prompt = _int(usage, "prompt_tokens")
        details = usage.get("prompt_tokens_details")
        cached = _int(details, "cached_tokens")
        uncached = None if prompt is None else prompt - (cached or 0)
        return Usage(
            input_tokens=uncached,
            output_tokens=_int(usage, "completion_tokens"),
            cache_read_tokens=cached,
        )
    # Anthropic: input_tokens already excludes cache reads/writes
    return Usage(
        input_tokens=_int(usage, "input_tokens"),
        output_tokens=_int(usage, "output_tokens"),
        cache_read_tokens=_int(usage, "cache_read_input_tokens"),
        cache_write_tokens=_int(usage, "cache_creation_input_tokens"),
    )


def _usage_from_sse(body: bytes) -> Usage:
    merged = Usage()
    for raw in body.split(b"\n"):
        line = raw.strip()
        if not line.startswith(b"data:"):
            continue
        data = line[len(b"data:") :].strip()
        if data in (b"", b"[DONE]"):
            continue
        try:
            event = json.loads(data)
        except (json.JSONDecodeError, ValueError):
            continue
        if not isinstance(event, dict):
            continue
        usage = event.get("usage")
        if usage is None and isinstance(event.get("message"), dict):
            usage = event["message"].get("usage")
        merged = _merge_usage(merged, _normalize_usage(usage))
    return merged


def _merge_usage(base: Usage, new: Usage) -> Usage:
    """Later non-None fields win (e.g. message_delta's final output tokens)."""

    def pick(a, b):
        return b if b is not None else a

    return Usage(
        input_tokens=pick(base.input_tokens, new.input_tokens),
        output_tokens=pick(base.output_tokens, new.output_tokens),
        cache_read_tokens=pick(base.cache_read_tokens, new.cache_read_tokens),
        cache_write_tokens=pick(
            base.cache_write_tokens, new.cache_write_tokens
        ),
    )


def _int(source: Any, key: str) -> int | None:
    if not isinstance(source, dict):
        return None
    value = source.get(key)
    return value if isinstance(value, int) else None

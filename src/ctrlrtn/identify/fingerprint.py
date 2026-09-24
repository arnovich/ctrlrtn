"""Compute a stable use-case key for a recorded call.

Precedence ladder (strongest signal wins):
  1. explicit tag  -> ``tag:<value>`` from the ``x-ctrlrtn-route`` header
  2. fingerprint   -> ``fp:<sha256[:16]>`` over the *structure* of the request:
                      system prompt + tool schemas + response_format.

Before hashing, the system text is normalized: generic volatile spans that
apps commonly inject (the current date, a timestamp) are stripped, so one
use-case doesn't fork into a fresh ``fp:`` key every day. Only clearly-generic
ASCII machine formats are touched (ISO dates ``YYYY-MM-DD`` / ``YYYY/MM/DD``,
ISO datetimes, ``HH:MM:SS`` times). App-specific volatile content (an injected
ticker list, retrieved context) — and forms the regexes deliberately miss
(localized/long-form dates, bare ``HH:MM``) — can't be recognized generically
without over-collapsing distinct use-cases, so the explicit ``x-ctrlrtn-route``
tag is the escape hatch. Calls with no identifying structure (no system, no
tools, no response_format) are left unkeyed (``None``); the tag is likewise the
escape hatch for those.

NOTE — changing this normalization RE-KEYS every ``fp:`` use-case. An experiment
created before such a change is frozen on its old ``fp:`` digest and will stop
matching live traffic (going silent, not erroring — it reads as a stale verdict,
NOT a propagation failure). Stop and recreate it on the new key, or move the app
to an ``x-ctrlrtn-route`` tag, which is immune to re-keying. ``tag:`` keys are
unaffected (the tag is resolved before any normalization runs).

Both wire shapes are supported:
  - OpenAI chat:  ``messages[role in {system, developer}]``, ``tools[].function``,
                  ``response_format``
  - Anthropic:    top-level ``system``, ``tools[].input_schema`` (no
                  response_format)
"""

from __future__ import annotations

import hashlib
import json
import re
from typing import Any, Mapping

from ctrlrtn.recorder.trace import Trace

_TAG_HEADER = "x-ctrlrtn-route"

# Generic volatile spans injected into system prompts (the current date, a
# timestamp) that would otherwise fork one use-case into a new fp: key every
# day. Each is replaced with a constant placeholder before hashing so the key
# stays stable. Order matters: the ISO datetime is matched FIRST so a minute-
# precision timestamp collapses whole — the bare-time rule needs seconds, so
# without this "<date> 16:42" would still fork per minute. [0-9] (not \d) keeps
# it ASCII: ISO 8601 is ASCII, and matching Unicode digits would over-collapse
# prompts that legitimately contain non-ASCII numerals. Kept deliberately narrow
# so genuinely-distinct use-cases are never collapsed; app-specific volatile
# content uses the x-ctrlrtn-route tag instead.
_VOLATILE_SPANS: tuple[tuple[re.Pattern[str], str], ...] = (
    (
        re.compile(
            r"[0-9]{4}-[0-9]{2}-[0-9]{2}[ T][0-9]{2}:[0-9]{2}(?::[0-9]{2})?"
            r"(?:\.[0-9]+)?(?:Z|[+-][0-9]{2}:?[0-9]{2})?"
        ),
        "<ts>",
    ),
    (re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}"), "<date>"),  # ISO date
    (re.compile(r"\b[0-9]{4}/[0-9]{2}/[0-9]{2}\b"), "<date>"),  # slash ISO
    (re.compile(r"\b[0-9]{2}:[0-9]{2}:[0-9]{2}\b"), "<time>"),  # HH:MM:SS
)


def _normalize_volatile(text: str) -> str:
    for pattern, placeholder in _VOLATILE_SPANS:
        text = pattern.sub(placeholder, text)
    return text


def fingerprint(headers: Mapping[str, str], body: bytes) -> str | None:
    """The use-case key for a request, or ``None`` if unkeyable.

    The pure core shared by the recorder's enrichment and the serving hot path,
    so an experiment lookup keys on exactly the use-case that gets recorded.
    ``headers`` must resolve ``x-ctrlrtn-route`` by that lowercase name (the
    stored dict is lowercased; a live case-insensitive Headers works too).
    """
    tag = headers.get(_TAG_HEADER)
    if tag:
        return f"tag:{tag}"

    material = _extract_material(body)
    if material is None:
        return None

    canonical = json.dumps(material, sort_keys=True, separators=(",", ":"))
    digest = hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:16]
    return f"fp:{digest}"


def fingerprint_trace(trace: Trace) -> str | None:
    """Return the use-case key for ``trace``, or ``None`` if unkeyable."""
    return fingerprint(trace.request_headers, trace.request_body)


def _extract_material(body: bytes) -> dict[str, Any] | None:
    try:
        payload = json.loads(body)
    except (json.JSONDecodeError, ValueError):
        return None
    if not isinstance(payload, dict):
        return None

    system = _system_text(payload)
    if system is not None:
        system = _normalize_volatile(system)
    tools = _tool_signatures(payload.get("tools"))
    response_format = payload.get("response_format")

    if system is None and not tools and response_format is None:
        return None
    return {
        "system": system,
        "tools": tools,
        "response_format": response_format,
    }


def _system_text(payload: dict[str, Any]) -> str | None:
    # Anthropic: top-level "system".
    if "system" in payload:
        return _content_to_text(payload["system"])
    # OpenAI chat: messages with a system/developer role.
    messages = payload.get("messages")
    if isinstance(messages, list):
        parts = [
            text
            for message in messages
            if isinstance(message, dict)
            and message.get("role") in ("system", "developer")
            for text in [_content_to_text(message.get("content"))]
            if text
        ]
        if parts:
            return "\n".join(parts)
    return None


def _content_to_text(content: Any) -> str | None:
    if content is None:
        return None
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        texts: list[str] = []
        for block in content:
            if isinstance(block, dict) and isinstance(block.get("text"), str):
                texts.append(block["text"])
            elif isinstance(block, str):
                texts.append(block)
        return "\n".join(texts) if texts else None
    return None


def _tool_signatures(tools: Any) -> list[dict[str, Any]]:
    """Normalize a tools array to a name-sorted list of ``{name, schema}`` so
    the key is independent of tool order and of human-facing descriptions."""
    if not isinstance(tools, list):
        return []
    signatures: list[dict[str, Any]] = []
    for tool in tools:
        if not isinstance(tool, dict):
            continue
        if isinstance(tool.get("function"), dict):  # OpenAI
            function = tool["function"]
            name = function.get("name")
            schema = function.get("parameters")
        else:  # Anthropic
            name = tool.get("name")
            schema = tool.get("input_schema")
        if name is None:
            continue
        signatures.append({"name": name, "schema": schema})
    signatures.sort(key=lambda sig: sig["name"])
    return signatures

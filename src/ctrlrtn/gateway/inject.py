"""Optional request transform: inject an Anthropic prompt-cache breakpoint.

Opt-in (``CTRLRTN_INJECT_CACHE``). The router marks the static
system+tools prefix of an Anthropic Messages request with ``cache_control`` so
the provider caches it. This is a pure cost/latency win: a cache breakpoint
cannot change the model's output, so unlike a model downgrade it needs no
*quality* eval.

It is the router's only *output-preserving* request mutation — ``serving.py``
also rewrites ``model`` and clamps ``max_tokens`` — and the one that runs
without a quality eval behind it, so it is deliberately cautious:

* **Scoped** to Anthropic ``/v1/messages`` (OpenAI caches automatically).
* **Fail-open** — any unexpected body shape returns the bytes unchanged; a call
  is never broken by this transform.
* **Hands off** if the request already uses caching anywhere (system, tools, or
  nested message/tool_result content), so we never push past Anthropic's
  4-breakpoint limit (which 400s). We add at most two breakpoints.
* **Skips small prefixes** below a rough size gate. The real minimum cacheable
  size is per-model in *tokens* (≈1k–4k), so the byte gate is only a cheap
  pre-filter; a sub-minimum marker is ignored by Anthropic (no error, no cache).

Caching is a net win only when the prefix is *reused within the ~5-min cache
TTL* (the write costs ~1.25x; each later read ~0.1x — break-even is ~1.3 reads).
This flag is **global** across Anthropic traffic, so on one-shot or
TTL-spaced-out use-cases it is a net loss. The ``recommendations`` command's
``caching_waste`` guardrail flags any use-case that writes to cache but never
reads back, so the loss is visible rather than silent; injection itself cannot
be scoped per use-case.
"""

from __future__ import annotations

import json
from collections.abc import Mapping

ANTHROPIC_MESSAGES_PATH = "/v1/messages"
_EPHEMERAL = {"type": "ephemeral"}
# Cheap pre-filter only: roughly Anthropic's smallest per-model minimum (~1k
# tokens at ≈4 chars/token). Sub-minimum markers are ignored by Anthropic, and
# the real net-loss guard is reuse (see module docstring), not size.
_MIN_PREFIX_BYTES = 4096


def cache_inject_decide(
    path: str,
    headers: Mapping[str, str],
    body: bytes,
    upstream_api: str | None = None,
) -> tuple[bytes, None]:
    """Adapt the cache injector to the proxy's ``decide`` hook.

    Cache injection never assigns an A/B arm, so the ServeDecision is always
    None. When live A/B lands, the serving engine composes this with arm
    assignment behind the same hook.
    """
    if upstream_api not in (None, "anthropic"):
        return body, None
    return inject_cache_control(path, body), None


def inject_cache_control(path: str, body: bytes) -> bytes:
    """Return ``body`` with a cache breakpoint on its static prefix, or the
    original bytes unchanged when injection does not apply or is unsafe."""
    if path != ANTHROPIC_MESSAGES_PATH:
        return body
    if len(body) < _MIN_PREFIX_BYTES:
        # The cacheable prefix is a subset of the body, so if the whole body is
        # under the minimum the prefix is too — skip without parsing.
        return body
    try:
        payload = json.loads(body)
    except (ValueError, TypeError):
        return body
    if not isinstance(payload, dict):
        return body
    try:
        if _uses_caching(payload):
            return body  # the app manages caching; don't add a breakpoint
        if not _add_breakpoint(payload):
            return body  # nothing big enough to cache
        return json.dumps(payload).encode("utf-8")
    except Exception:
        return body  # fail-open: this transform must never break a call


def _add_breakpoint(payload: dict) -> bool:
    """Mark the static prefix for caching. Anthropic's cache order is tools,
    then system, then messages, and a breakpoint caches everything up to and
    including it. Mark the end of ``tools`` AND the end of ``system`` (two of
    the four allowed breakpoints): the system breakpoint caches tools+system,
    and the separate tools breakpoint keeps the stable tool definitions cached
    even when a dynamic system tail changes. Mutates ``payload``; returns
    whether anything was marked."""
    system = payload.get("system")
    tools = payload.get("tools")
    if _prefix_bytes(system, tools) < _MIN_PREFIX_BYTES:
        return False
    marked = False
    if isinstance(tools, list) and tools and isinstance(tools[-1], dict):
        tools[-1]["cache_control"] = _EPHEMERAL
        marked = True
    if isinstance(system, str) and system:
        payload["system"] = [
            {"type": "text", "text": system, "cache_control": _EPHEMERAL}
        ]
        marked = True
    elif isinstance(system, list) and system and isinstance(system[-1], dict):
        system[-1]["cache_control"] = _EPHEMERAL
        marked = True
    return marked


def _prefix_bytes(system: object, tools: object) -> int:
    """Rough size of the static (system + tools) prefix, in bytes."""
    total = 0
    if isinstance(system, str):
        total += len(system)
    elif isinstance(system, list):
        total += len(json.dumps(system))
    if isinstance(tools, list):
        total += len(json.dumps(tools))
    return total


def _uses_caching(payload: dict) -> bool:
    """True if the request already carries a ``cache_control`` anywhere we'd
    touch (system, tools, or message content) — in which case we leave it be."""
    system = payload.get("system")
    if isinstance(system, list) and _any_marked(system):
        return True
    tools = payload.get("tools")
    if isinstance(tools, list) and _any_marked(tools):
        return True
    messages = payload.get("messages")
    if isinstance(messages, list):
        for message in messages:
            if not isinstance(message, dict):
                continue
            if "cache_control" in message:
                return True
            content = message.get("content")
            if isinstance(content, list) and _any_marked(content):
                return True
    return False


def _any_marked(blocks: list) -> bool:
    """Whether any block carries a ``cache_control`` — recursing one level into
    a block's own ``content`` list, since a ``tool_result`` nests its blocks
    there and a marker hidden inside still counts toward the 4-breakpoint cap.
    """
    for block in blocks:
        if not isinstance(block, dict):
            continue
        if "cache_control" in block:
            return True
        inner = block.get("content")
        if isinstance(inner, list) and _any_marked(inner):
            return True
    return False

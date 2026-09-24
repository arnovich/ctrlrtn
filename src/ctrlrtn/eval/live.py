"""Anthropic-backed replay and judge functions for a live eval run.

These are the only network-touching pieces of the replay path, so they need
``ANTHROPIC_API_KEY``. The body
construction and response parsing are pure (``swap_model`` /
``extract_output_text``) and unit-tested here with a mock transport; the live
smoke test against the real API is the operator's.

Limits: same-provider Anthropic Messages only. A recorded
request's ``anthropic-beta`` header is not re-emitted, so a beta-gated use-case
(context management, MCP servers, Files) will 400 — the failure is surfaced in
the report, not silent (see ``ReplayReport.failures``). No retry/backoff: a
transient 429/529 counts as a failed sample.
"""

from __future__ import annotations

import json

import httpx

from ctrlrtn.eval.judge import JudgeFn
from ctrlrtn.eval.replay import ReplayFn, extract_output_text, swap_model

ANTHROPIC_MESSAGES_URL = "https://api.anthropic.com/v1/messages"
_ANTHROPIC_VERSION = "2023-06-01"
# A strong, neutral grader by default; override per run if cost matters.
DEFAULT_JUDGE_MODEL = "claude-opus-4-8"
# The verdict JSON is ~25 tokens, but a thinking-disabled model can write some
# reasoning before it; leave headroom so a preamble can't truncate the JSON.
_JUDGE_MAX_TOKENS = 1024

# A replay can return a large completion, so the read budget is generous — but
# clamp connect/write/pool so a black-holed host fails fast instead of hanging
# for the full read budget. The judge returns a tiny object: a short read.
REPLAY_TIMEOUT = httpx.Timeout(connect=10.0, read=600.0, write=30.0, pool=10.0)
JUDGE_TIMEOUT = httpx.Timeout(connect=10.0, read=60.0, write=30.0, pool=10.0)

# Request fields that 400 when a recorded request is replayed on a different
# model: an extended-thinking block is bound to the model that produced it.
_CROSS_MODEL_DROP = ("thinking",)


def _headers(api_key: str) -> dict[str, str]:
    return {
        "x-api-key": api_key,
        "anthropic-version": _ANTHROPIC_VERSION,
        "content-type": "application/json",
    }


def _text_response(
    http: httpx.Client, url: str, body: bytes, api_key: str
) -> str:
    resp = http.post(url, content=body, headers=_headers(api_key))
    resp.raise_for_status()
    # httpx has already transparently decompressed ``.content``; passing the
    # stale Content-Encoding header would make the decoder try to re-inflate it.
    return extract_output_text(resp.content)


def anthropic_replay_fn(
    api_key: str,
    *,
    client: httpx.Client | None = None,
    url: str = ANTHROPIC_MESSAGES_URL,
    max_tokens: int | None = None,
) -> ReplayFn:
    """A replay function that re-sends a recorded request on ``model``.

    Drops request fields that don't carry across models (a recorded ``thinking``
    block 400s on a different model). Pass ``max_tokens`` to clamp the recorded
    cap to the candidate's ceiling (a smaller candidate may 400 on the
    baseline's higher cap)."""
    http = client or httpx.Client(timeout=REPLAY_TIMEOUT)

    def replay(request_body: bytes, model: str) -> str:
        payload = json.loads(swap_model(request_body, model))
        for field in _CROSS_MODEL_DROP:
            payload.pop(field, None)
        if max_tokens is not None:
            payload["max_tokens"] = max_tokens
        return _text_response(http, url, json.dumps(payload).encode(), api_key)

    return replay


def anthropic_judge_fn(
    api_key: str,
    *,
    model: str = DEFAULT_JUDGE_MODEL,
    client: httpx.Client | None = None,
    url: str = ANTHROPIC_MESSAGES_URL,
) -> JudgeFn:
    """A judge function that grades a (blinded) prompt with ``model``."""
    http = client or httpx.Client(timeout=JUDGE_TIMEOUT)

    def judge(prompt: str) -> str:
        body = json.dumps(
            {
                "model": model,
                "max_tokens": _JUDGE_MAX_TOKENS,
                "messages": [{"role": "user", "content": prompt}],
            }
        ).encode()
        return _text_response(http, url, body, api_key)

    return judge

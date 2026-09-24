"""M0: the fingerprinter groups calls into stable use-case keys."""

from __future__ import annotations

import json

import httpx

from ctrlrtn.config import Settings
from ctrlrtn.gateway.app import create_app
from ctrlrtn.identify.fingerprint import fingerprint_trace
from ctrlrtn.recorder.recorder import Recorder
from ctrlrtn.recorder.store import InMemoryTraceStore
from ctrlrtn.recorder.trace import Trace


def _trace(body, headers: dict | None = None) -> Trace:
    raw = body if isinstance(body, bytes) else json.dumps(body).encode()
    return Trace(
        method="POST",
        path="/v1/chat/completions",
        query="",
        request_headers=headers or {},
        request_body=raw,
        status_code=200,
        response_headers={},
        response_body=b"",
        latency_ms=0.0,
    )


def test_same_structure_same_key_regardless_of_user_text():
    # Same system prompt, different user content and even a different model
    # -> same use-case.
    a = _trace(
        {
            "model": "gpt-4o",
            "messages": [
                {"role": "system", "content": "You are a classifier."},
                {"role": "user", "content": "ticket 1"},
            ],
        }
    )
    b = _trace(
        {
            "model": "gpt-4o-mini",
            "messages": [
                {"role": "system", "content": "You are a classifier."},
                {"role": "user", "content": "a totally different ticket"},
            ],
        }
    )
    assert fingerprint_trace(a) == fingerprint_trace(b)
    assert fingerprint_trace(a).startswith("fp:")


def test_different_system_prompt_different_key():
    a = _trace({"messages": [{"role": "system", "content": "Classify."}]})
    b = _trace({"messages": [{"role": "system", "content": "Summarize."}]})
    assert fingerprint_trace(a) != fingerprint_trace(b)


def test_tool_order_does_not_change_key():
    t1 = {"type": "function", "function": {"name": "a", "parameters": {}}}
    t2 = {"type": "function", "function": {"name": "b", "parameters": {}}}
    base = [{"role": "system", "content": "s"}]
    a = _trace({"messages": base, "tools": [t1, t2]})
    b = _trace({"messages": base, "tools": [t2, t1]})
    assert fingerprint_trace(a) == fingerprint_trace(b)


def test_response_format_changes_key():
    base = {"messages": [{"role": "system", "content": "s"}]}
    a = _trace(base)
    b = _trace({**base, "response_format": {"type": "json_object"}})
    assert fingerprint_trace(a) != fingerprint_trace(b)


def test_anthropic_shape_is_keyed():
    trace = _trace(
        {
            "model": "claude-haiku",
            "system": "You are a helper.",
            "tools": [{"name": "lookup", "input_schema": {"type": "object"}}],
            "messages": [{"role": "user", "content": "hi"}],
        }
    )
    key = fingerprint_trace(trace)
    assert key is not None and key.startswith("fp:")


def test_explicit_tag_overrides_fingerprint():
    trace = _trace(
        {"messages": [{"role": "system", "content": "s"}]},
        headers={"x-ctrlrtn-route": "summarize_ticket"},
    )
    assert fingerprint_trace(trace) == "tag:summarize_ticket"


def test_non_json_body_is_unkeyed():
    assert fingerprint_trace(_trace(b"not json at all")) is None


def test_no_identifying_structure_is_unkeyed():
    # User-only chat: no system, tools, or response_format to key on.
    trace = _trace({"messages": [{"role": "user", "content": "hi"}]})
    assert fingerprint_trace(trace) is None


# --- volatile-span normalization: stable keys across injected dates/times ---


def test_injected_date_does_not_change_key():
    # An app that injects the current date into the system prompt must not fork
    # one use-case into a new fp: key each day.
    a = _trace({"system": "You are the editor. As of 2026-07-01, write."})
    b = _trace({"system": "You are the editor. As of 2026-07-02, write."})
    assert fingerprint_trace(a) == fingerprint_trace(b)
    assert fingerprint_trace(a).startswith("fp:")


def test_injected_timestamp_does_not_change_key():
    a = _trace({"system": "Report at 2026-07-01 16:42:45. Summarize."})
    b = _trace({"system": "Report at 2026-07-02 09:00:00. Summarize."})
    assert fingerprint_trace(a) == fingerprint_trace(b)


def test_bare_clock_time_is_normalized():
    a = _trace({"system": "Now 16:42:45. Go."})
    b = _trace({"system": "Now 09:00:00. Go."})
    assert fingerprint_trace(a) == fingerprint_trace(b)


def test_normalization_does_not_collapse_distinct_use_cases():
    # Differing in real content (not just a date) stays distinct — the point is
    # to strip volatile spans, not to over-collapse.
    a = _trace({"system": "As of 2026-07-01, classify tickets."})
    b = _trace({"system": "As of 2026-07-01, summarize tickets."})
    assert fingerprint_trace(a) != fingerprint_trace(b)


def test_minute_precision_timestamp_is_normalized():
    # No seconds: only the datetime rule (matched first) catches this — the
    # bare HH:MM:SS rule needs seconds. Pins that rule as load-bearing.
    a = _trace({"system": "Report at 2026-07-01 16:42. Summarize."})
    b = _trace({"system": "Report at 2026-07-02 09:00. Summarize."})
    assert fingerprint_trace(a) == fingerprint_trace(b)


def test_iso_datetime_with_t_fraction_and_zone_is_normalized():
    a = _trace({"system": "At 2026-07-01T16:42:45.123Z go."})
    b = _trace({"system": "At 2026-07-02T09:00:00.001Z go."})
    assert fingerprint_trace(a) == fingerprint_trace(b)


def test_slash_iso_date_is_normalized():
    a = _trace({"system": "As of 2026/07/01, write."})
    b = _trace({"system": "As of 2026/07/02, write."})
    assert fingerprint_trace(a) == fingerprint_trace(b)


def test_version_strings_are_not_over_collapsed():
    # Two prompts differing ONLY in a non-date numeric token stay distinct —
    # pins the false-positive boundary (dates are stripped, versions are not).
    a = _trace({"system": "Use ruleset v1.2.3 exactly."})
    b = _trace({"system": "Use ruleset v1.2.4 exactly."})
    assert fingerprint_trace(a) != fingerprint_trace(b)


def test_non_ascii_digit_date_is_not_normalized():
    # ISO 8601 is ASCII; a fullwidth-digit "date" is left intact (patterns use
    # [0-9], not \d), so two different ones stay distinct rather than collapsing.
    a = _trace({"system": "As of ２０２６-０７-01, x."})
    b = _trace({"system": "As of ２０２６-０７-02, x."})
    assert fingerprint_trace(a) != fingerprint_trace(b)


def test_tag_still_wins_over_a_normalized_fingerprint():
    trace = _trace(
        {"system": "As of 2026-07-01, do the thing."},
        headers={"x-ctrlrtn-route": "daily_report"},
    )
    assert fingerprint_trace(trace) == "tag:daily_report"


async def test_key_flows_into_stored_trace(streaming_upstream):
    store = InMemoryTraceStore()
    recorder = Recorder(
        store,
        enrich=lambda t: setattr(t, "use_case_key", fingerprint_trace(t)),
    )
    recorder.start()

    body = json.dumps(
        {
            "model": "gpt-4o",
            "messages": [
                {"role": "system", "content": "You are a classifier."}
            ],
        }
    ).encode()

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=streaming_upstream({})),
        base_url="http://upstream",
    ) as upstream_client:
        app = create_app(
            Settings(upstream_base_url="http://upstream"),
            upstream_client=upstream_client,
            recorder=recorder,
        )
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="http://router",
        ) as client:
            await client.post(
                "/v1/chat/completions",
                content=body,
                headers={"content-type": "application/json"},
            )
        await recorder.join()
    await recorder.aclose()

    assert len(store.traces) == 1
    assert store.traces[0].use_case_key == fingerprint_trace(_trace(body))
    assert store.traces[0].use_case_key.startswith("fp:")

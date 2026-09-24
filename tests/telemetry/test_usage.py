"""M0: token/model extraction across providers and streaming modes."""

from __future__ import annotations

import gzip
import json

from ctrlrtn.recorder.trace import Trace
from ctrlrtn.telemetry.enrich import enrich_trace
from ctrlrtn.telemetry.pricing import cost_usd
from ctrlrtn.telemetry.usage import (
    Usage,
    decode_body,
    extract_model,
    extract_response_model,
    extract_usage,
)


def test_openai_non_streaming_usage():
    body = json.dumps(
        {"usage": {"prompt_tokens": 12, "completion_tokens": 7}}
    ).encode()
    usage = extract_usage(body)
    assert usage.input_tokens == 12
    assert usage.output_tokens == 7


def test_anthropic_non_streaming_usage():
    body = json.dumps(
        {"usage": {"input_tokens": 30, "output_tokens": 9}}
    ).encode()
    usage = extract_usage(body)
    assert usage.input_tokens == 30
    assert usage.output_tokens == 9


def test_openai_streaming_usage_in_final_chunk():
    chunks = [
        'data: {"choices":[{"delta":{"content":"hi"}}],"usage":null}',
        'data: {"choices":[],"usage":'
        '{"prompt_tokens":40,"completion_tokens":11}}',
        "data: [DONE]",
    ]
    body = ("\n\n".join(chunks) + "\n\n").encode()
    usage = extract_usage(body)
    assert usage.input_tokens == 40
    assert usage.output_tokens == 11


def test_anthropic_streaming_usage_across_events():
    events = [
        'data: {"type":"message_start","message":'
        '{"usage":{"input_tokens":50,"output_tokens":1}}}',
        'data: {"type":"message_delta","delta":{},'
        '"usage":{"output_tokens":23}}',
        "data: [DONE]",
    ]
    body = ("\n\n".join(events) + "\n\n").encode()
    usage = extract_usage(body)
    assert usage.input_tokens == 50
    assert usage.output_tokens == 23


def test_no_usage_is_left_none():
    usage = extract_usage(b"data: hello\n\ndata: [DONE]\n\n")
    assert usage.input_tokens is None
    assert usage.output_tokens is None


def test_extract_model():
    assert extract_model(json.dumps({"model": "gpt-4o"}).encode()) == "gpt-4o"
    assert extract_model(b"not json") is None


def test_anthropic_cache_tokens_are_separated():
    body = json.dumps(
        {
            "usage": {
                "input_tokens": 100,
                "output_tokens": 20,
                "cache_read_input_tokens": 500,
                "cache_creation_input_tokens": 50,
            }
        }
    ).encode()
    usage = extract_usage(body)
    assert (usage.input_tokens, usage.output_tokens) == (100, 20)
    assert usage.cache_read_tokens == 500
    assert usage.cache_write_tokens == 50


def test_openai_cached_tokens_subtracted_from_input():
    body = json.dumps(
        {
            "usage": {
                "prompt_tokens": 600,
                "completion_tokens": 20,
                "prompt_tokens_details": {"cached_tokens": 500},
            }
        }
    ).encode()
    usage = extract_usage(body)
    assert usage.input_tokens == 100  # 600 prompt - 500 cached
    assert usage.cache_read_tokens == 500
    assert usage.output_tokens == 20


def test_decode_body_gzip_roundtrip():
    raw = b'{"usage":{"input_tokens":11,"output_tokens":4}}'
    gz = gzip.compress(raw)
    assert decode_body(gz, "gzip") == raw
    assert decode_body(gz, None) == raw  # magic-byte fallback
    assert decode_body(raw, None) == raw  # passthrough when not compressed


def test_usage_from_gzipped_response():
    raw = json.dumps(
        {"usage": {"input_tokens": 260, "output_tokens": 6}}
    ).encode()
    usage = extract_usage(decode_body(gzip.compress(raw), "gzip"))
    assert usage.input_tokens == 260
    assert usage.output_tokens == 6


def test_enrich_decodes_gzip_for_tokens():
    # The real-world bug: Anthropic gzips the response, so the stored body must
    # be decoded before usage extraction.
    raw = json.dumps(
        {
            "model": "claude-sonnet-4-5",
            "usage": {"input_tokens": 99, "output_tokens": 7},
        }
    ).encode()
    trace = Trace(
        method="POST",
        path="/v1/messages",
        query="",
        request_headers={},
        request_body=b'{"model":"claude","system":"You are X."}',
        status_code=200,
        response_headers={"content-encoding": "gzip"},
        response_body=gzip.compress(raw),
        latency_ms=1.0,
    )
    enrich_trace(trace)
    assert trace.input_tokens == 99
    assert trace.output_tokens == 7
    assert trace.use_case_key.startswith("fp:")


# --- served (response-echoed) model (slice 5) -----------------------------


def test_response_model_non_streaming():
    body = json.dumps({"model": "claude-sonnet-4-5", "usage": {}}).encode()
    assert extract_response_model(body) == "claude-sonnet-4-5"


def test_response_model_openai_streaming_chunk():
    body = (
        b'data: {"model":"gpt-4o-2024","choices":[{"delta":{}}]}\n\n'
        b"data: [DONE]\n\n"
    )
    assert extract_response_model(body) == "gpt-4o-2024"


def test_response_model_anthropic_message_start():
    body = (
        b'data: {"type":"message_start","message":'
        b'{"model":"claude-haiku-4-5","usage":{"input_tokens":1}}}\n\n'
        b"data: [DONE]\n\n"
    )
    assert extract_response_model(body) == "claude-haiku-4-5"


def test_response_model_absent_is_none():
    assert extract_response_model(b'{"usage": {}}') is None
    assert extract_response_model(b"not json") is None


def test_enrich_prices_on_the_response_echoed_model():
    # Ground truth wins: we requested opus and swapped to haiku (A/B), but the
    # provider actually served (and billed) sonnet — pricing must follow the
    # response echo, over both served_model and the requested model.
    raw = json.dumps(
        {
            "model": "claude-sonnet-4-5",
            "usage": {"input_tokens": 1000, "output_tokens": 500},
        }
    ).encode()
    trace = Trace(
        method="POST",
        path="/v1/messages",
        query="",
        request_headers={},
        request_body=b'{"model": "claude-opus-4"}',
        status_code=200,
        response_headers={},
        response_body=raw,
        latency_ms=1.0,
        served_model="claude-haiku-4-5",
    )
    enrich_trace(trace)
    assert trace.model == "claude-opus-4"  # display: requested
    usage = Usage(input_tokens=1000, output_tokens=500)
    assert trace.cost_usd == cost_usd("claude-sonnet-4-5", usage)  # served echo

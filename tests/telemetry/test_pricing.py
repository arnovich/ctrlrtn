"""M2: cache-aware per-model cost."""

from __future__ import annotations

import json

from ctrlrtn.recorder.trace import Trace
from ctrlrtn.telemetry.enrich import enrich_trace
from ctrlrtn.telemetry.pricing import (
    cheaper_candidates,
    cost_usd,
    price_for,
)
from ctrlrtn.telemetry.usage import Usage


def test_cheaper_candidates_by_family():
    assert "claude-haiku-4-5" in cheaper_candidates("claude-sonnet-4-5-x")
    assert cheaper_candidates("gpt-4o") == ["gpt-4o-mini"]
    assert cheaper_candidates("claude-haiku-4-5") == []  # already cheapest
    assert cheaper_candidates("mystery-model") == []
    assert cheaper_candidates(None) == []


def test_price_for_prefix_matches_version_suffix():
    assert price_for("claude-sonnet-4-5-20250929") == price_for(
        "claude-sonnet-4-5"
    )
    assert price_for("gpt-4o-mini-2024-07-18") is not None
    assert price_for("totally-unknown-model") is None
    assert price_for(None) is None


def test_family_requires_a_delimiter_boundary():
    assert price_for("gpt-4o-mini") is not None
    assert (
        price_for("gpt-4ofoo") is None
    )  # no delimiter after gpt-4o -> no match
    assert price_for("claudexyz") is None
    # a non-version suffix falls back to the coarser family, not the -4-5 one
    assert price_for("claude-sonnet-4-5x") == price_for("claude-sonnet-4")


def test_longest_prefix_wins_so_mini_is_not_gpt4o():
    assert price_for("gpt-4o-mini") == price_for("gpt-4o-mini-2024-07-18")
    assert price_for("gpt-4o-mini").input < price_for("gpt-4o").input


def test_opus_price_generations_split_at_4_5():
    # Opus 4.5-4.8 are $5/$25; Opus 4.0/4.1 stay at the retired $15/$75. The
    # boundary rides the longest-prefix rule: -4-1 has no own row, so it falls
    # to the claude-opus-4 legacy family — it must NOT catch the -4-5+ price.
    assert price_for("claude-opus-4-8").input == 5.0
    assert price_for("claude-opus-4-8-20260115") == price_for("claude-opus-4-8")
    assert price_for("claude-opus-4-5").output == 25.0
    assert price_for("claude-opus-4-1").input == 15.0
    assert price_for("claude-opus-4").output == 75.0


def test_claude_5_generation_is_priced():
    # A missing row records $0 cost for real traffic (the campaign report
    # warns on exactly that) — pin that the current models are priced.
    assert price_for("claude-fable-5").input == 10.0
    assert price_for("claude-sonnet-5").input == 2.0  # intro until 2026-09-01
    # sonnet-5 must not prefix-fall into the sonnet-4 family.
    assert (
        price_for("claude-sonnet-5").input != price_for("claude-sonnet-4").input
    )


def test_cost_is_cache_aware():
    price = price_for("claude-sonnet-4-5")
    # 1M uncached input billed at the input rate
    assert (
        cost_usd("claude-sonnet-4-5", Usage(input_tokens=1_000_000))
        == price.input
    )
    # cache reads billed at the (much lower) cache-read rate
    cached = cost_usd("claude-sonnet-4-5", Usage(cache_read_tokens=1_000_000))
    assert cached == price.cache_read
    assert cached < price.input


def test_cost_none_for_unpriced_model_or_missing_usage():
    assert cost_usd("mystery-model", Usage(input_tokens=100)) is None
    assert cost_usd("claude-sonnet-4-5", None) is None


def test_free_provider_cost_is_zero_independent_of_model_or_usage():
    usage = Usage(input_tokens=1_000_000, output_tokens=1_000_000)
    assert cost_usd("gpt-4o", usage, free=True) == 0.0
    assert cost_usd("mystery-model", usage, free=True) == 0.0
    assert cost_usd("mystery-model", None, free=True) == 0.0


def test_same_model_can_be_free_on_one_provider_and_paid_on_another():
    usage = Usage(input_tokens=1_000_000)
    assert cost_usd("gpt-4o", usage, free=True) == 0.0
    assert cost_usd("gpt-4o", usage) == price_for("gpt-4o").input


def test_enrich_computes_cost_from_response():
    body = json.dumps(
        {"usage": {"input_tokens": 1_000_000, "output_tokens": 0}}
    ).encode()
    trace = Trace(
        method="POST",
        path="/v1/messages",
        query="",
        request_headers={},
        request_body=b'{"model":"claude-sonnet-4-5-20250929"}',
        status_code=200,
        response_headers={},
        response_body=body,
        latency_ms=1.0,
    )
    enrich_trace(trace)
    assert trace.model == "claude-sonnet-4-5-20250929"
    assert trace.cost_usd == price_for("claude-sonnet-4-5").input


def test_enrich_honors_the_recorded_provider_pricing_policy():
    body = json.dumps(
        {"usage": {"prompt_tokens": 1_000_000, "completion_tokens": 0}}
    ).encode()
    common = {
        "method": "POST",
        "path": "/v1/chat/completions",
        "query": "",
        "request_headers": {},
        "request_body": b'{"model":"gpt-4o"}',
        "status_code": 200,
        "response_headers": {},
        "response_body": body,
        "latency_ms": 1.0,
    }
    paid = Trace(provider="openai", **common)
    free = Trace(provider="ollama", provider_free=True, **common)

    enrich_trace(paid)
    enrich_trace(free)

    assert paid.cost_usd == price_for("gpt-4o").input
    assert free.cost_usd == 0.0
    assert free.input_tokens == paid.input_tokens == 1_000_000


def test_nano_snapshot_price_and_cache_accounting_reach_recorded_trace():
    """The actual served nano snapshot gets a cache-aware price and output cap."""
    import pytest

    from ctrlrtn.telemetry.pricing import max_output_tokens

    model = "gpt-5-nano-2025-08-07"
    trace = Trace(
        method="POST",
        path="/v1/chat/completions",
        query="",
        request_headers={},
        status_code=200,
        response_headers={},
        latency_ms=1.0,
        request_body=json.dumps({"model": "gpt-5-nano"}).encode(),
        response_body=json.dumps(
            {
                "model": model,
                "usage": {
                    "prompt_tokens": 1000,
                    "completion_tokens": 200,
                    "prompt_tokens_details": {"cached_tokens": 400},
                    "completion_tokens_details": {"reasoning_tokens": 150},
                },
            }
        ).encode(),
    )
    enrich_trace(trace)
    assert trace.cost_usd == pytest.approx(0.000112)
    assert max_output_tokens(model) == 128000
    assert price_for("gpt-5-nanoish") is None

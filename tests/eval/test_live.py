"""Live eval wiring: Anthropic replay/judge clients, the store sampler, and
the CLI report render (ctrlrtn.eval.live + cli.render_replay)."""

from __future__ import annotations

import gzip
import json

import httpx
import pytest

from ctrlrtn.cli.render import render_replay
from ctrlrtn.eval.live import anthropic_judge_fn, anthropic_replay_fn
from ctrlrtn.eval.ni import NIResult
from ctrlrtn.eval.replay import ReplayReport
from ctrlrtn.recorder.store import _UNKEYED, SqliteTraceStore
from ctrlrtn.recorder.trace import Trace


def _mock_client(handler):
    return httpx.Client(transport=httpx.MockTransport(handler))


def _anthropic_text(text):
    return httpx.Response(
        200, json={"content": [{"type": "text", "text": text}]}
    )


# --- live replay / judge clients -------------------------------------------


def test_replay_fn_swaps_model_disables_stream_and_parses():
    seen = {}

    def handler(request):
        seen["body"] = json.loads(request.content)
        seen["key"] = request.headers.get("x-api-key")
        return _anthropic_text("hello")

    fn = anthropic_replay_fn("sk-test", client=_mock_client(handler))
    body = json.dumps(
        {
            "model": "base",
            "stream": True,
            "max_tokens": 1000,
            "messages": [{"role": "user", "content": "hi"}],
        }
    ).encode()
    assert fn(body, "claude-haiku-4-5") == "hello"
    assert seen["body"]["model"] == "claude-haiku-4-5"
    assert "stream" not in seen["body"]
    assert seen["key"] == "sk-test"


def test_replay_fn_overrides_max_tokens_when_given():
    seen = {}

    def handler(request):
        seen["body"] = json.loads(request.content)
        return _anthropic_text("x")

    fn = anthropic_replay_fn("k", client=_mock_client(handler), max_tokens=512)
    fn(
        json.dumps({"model": "m", "max_tokens": 9999, "messages": []}).encode(),
        "m2",
    )
    assert seen["body"]["max_tokens"] == 512


def test_judge_fn_posts_prompt_and_returns_raw_text():
    seen = {}

    def handler(request):
        seen["body"] = json.loads(request.content)
        return _anthropic_text('{"score_a": 7, "score_b": 3}')

    fn = anthropic_judge_fn(
        "k", model="claude-opus-4-8", client=_mock_client(handler)
    )
    raw = fn("grade this pair")
    assert raw == '{"score_a": 7, "score_b": 3}'
    assert seen["body"]["model"] == "claude-opus-4-8"
    assert seen["body"]["messages"][0]["content"] == "grade this pair"


def test_replay_fn_raises_on_http_error():
    fn = anthropic_replay_fn(
        "k", client=_mock_client(lambda r: httpx.Response(400, json={"e": 1}))
    )
    with pytest.raises(httpx.HTTPStatusError):
        fn(json.dumps({"model": "m", "messages": []}).encode(), "m2")


def test_replay_fn_drops_thinking_block_on_cross_model_replay():
    # A recorded extended-thinking block is bound to the model that produced it
    # and 400s on a different model; replay must strip it.
    seen = {}

    def handler(request):
        seen["body"] = json.loads(request.content)
        return _anthropic_text("ok")

    fn = anthropic_replay_fn("k", client=_mock_client(handler))
    body = json.dumps(
        {
            "model": "base",
            "thinking": {"type": "enabled", "budget_tokens": 4000},
            "messages": [{"role": "user", "content": "hi"}],
        }
    ).encode()
    fn(body, "claude-haiku-4-5")
    assert "thinking" not in seen["body"]
    assert seen["body"]["model"] == "claude-haiku-4-5"


def test_replay_fn_parses_gzipped_response_without_double_decode():
    # httpx already decompresses .content; passing the stale content-encoding
    # would make us try to re-inflate plain JSON. Use a really-gzipped body.
    raw = json.dumps({"content": [{"type": "text", "text": "unzipped"}]})

    def handler(request):
        return httpx.Response(
            200,
            content=gzip.compress(raw.encode()),
            headers={"content-encoding": "gzip"},
        )

    fn = anthropic_replay_fn("k", client=_mock_client(handler))
    assert fn(json.dumps({"model": "m", "messages": []}).encode(), "m2") == (
        "unzipped"
    )


# --- store sampler ---------------------------------------------------------


def _req_trace(use_case, status, body, task_id) -> Trace:
    return Trace(
        method="POST",
        path="/v1/messages",
        query="",
        request_headers={},
        request_body=body,
        status_code=status,
        response_headers={},
        response_body=b"{}",
        latency_ms=1.0,
        use_case_key=use_case,
        task_id=task_id,
    )


async def test_requests_for_use_case_filters_and_orders():
    store = SqliteTraceStore(":memory:")
    try:
        await store.save(_req_trace("fp:x", 200, b'{"a":1}', "ed1"))
        await store.save(_req_trace("fp:x", 201, b'{"a":2}', "ed2"))  # 2xx ok
        await store.save(_req_trace("fp:x", 500, b'{"a":3}', "ed3"))  # error
        await store.save(_req_trace("fp:y", 200, b'{"a":4}', None))  # other uc
        rows = store.requests_for_use_case("fp:x", 10)
        assert len(rows) == 2  # both 2xx fp:x calls, the 5xx excluded
        assert rows[0]["request_body"] == b'{"a":2}'  # newest first
        assert {r["task_id"] for r in rows} == {"ed1", "ed2"}
    finally:
        store.close()


async def test_requests_for_use_case_round_robins_across_tasks():
    # The NI test clusters by task: 40 calls from 3 editions is 3 units and
    # underpowered. The sampler must spread the limit across tasks (newest of
    # each first), not take the newest-N bunched into the last editions.
    store = SqliteTraceStore(":memory:")
    try:
        for i in range(6):  # a chatty recent edition...
            await store.save(_req_trace("fp:x", 200, f"A{i}".encode(), "edA"))
        for i in range(2):  # ...older, quieter editions
            await store.save(_req_trace("fp:x", 200, f"B{i}".encode(), "edB"))
        await store.save(_req_trace("fp:x", 200, b"C0", "edC"))
        await store.save(_req_trace("fp:x", 200, b"N0", None))  # untasked
        rows = store.requests_for_use_case("fp:x", 4)
        # One call from EVERY unit before a second from any: 4 distinct units.
        tasks = [r["task_id"] for r in rows]
        assert sorted(t or "(none)" for t in tasks) == [
            "(none)",
            "edA",
            "edB",
            "edC",
        ]
        # A bigger limit then takes each task's second-newest, and so on.
        rows = store.requests_for_use_case("fp:x", 6)
        assert [r["task_id"] for r in rows].count("edA") == 2
        assert [r["task_id"] for r in rows].count("edB") == 2
    finally:
        store.close()


async def test_requests_for_use_case_passes_task_id_through_as_none():
    # The no-COALESCE contract: an untasked sample must come back as None (not a
    # sentinel string), so replay keeps it an independent statistical unit.
    store = SqliteTraceStore(":memory:")
    try:
        await store.save(_req_trace("fp:x", 200, b'{"a":1}', None))
        rows = store.requests_for_use_case("fp:x", 10)
        assert len(rows) == 1
        assert rows[0]["task_id"] is None
    finally:
        store.close()


async def test_requests_for_use_case_reaches_unkeyed_bucket():
    # Unkeyed traffic is stored as NULL use_case_key; COALESCE makes the
    # "(unkeyed)" label an operator can read off rankings actually resolve.
    store = SqliteTraceStore(":memory:")
    try:
        await store.save(_req_trace(None, 200, b'{"a":1}', "ed1"))
        rows = store.requests_for_use_case(_UNKEYED, 10)
        assert len(rows) == 1
        assert rows[0]["request_body"] == b'{"a":1}'
    finally:
        store.close()


# --- render ----------------------------------------------------------------


def _report(
    non_inferior,
    *,
    underpowered=False,
    lower=-0.2,
    n_units=40,
    n_pairings=40,
    n_failed=0,
    failures=(),
):
    return ReplayReport(
        result=NIResult(
            non_inferior=non_inferior,
            mean_diff=-0.1,
            lower_bound=lower,
            n=n_pairings,
            n_units=n_units,
            margin=1.0,
            confidence=0.95,
            underpowered=underpowered,
        ),
        n_pairings=n_pairings,
        n_failed=n_failed,
        n_blank=0,
        mean_diff=-0.1,
        baseline_model="claude-sonnet-4-5",
        candidate_model="claude-haiku-4-5",
        failures=failures,
    )


def test_render_replay_non_inferior():
    out = render_replay(_report(True))
    assert "NON-INFERIOR" in out
    assert "claude-sonnet-4-5 -> claude-haiku-4-5" in out


def test_render_replay_not_non_inferior():
    out = render_replay(_report(False))
    assert "NOT non-inferior" in out


def test_render_replay_underpowered_handles_inf_bound():
    out = render_replay(
        _report(False, underpowered=True, lower=float("-inf"), n_units=5)
    )
    assert "UNDERPOWERED" in out
    assert "-inf" in out


def test_render_replay_surfaces_failure_reasons():
    # A degraded batch must name WHY, not just say "UNDERPOWERED".
    out = render_replay(
        _report(
            False,
            underpowered=True,
            lower=float("-inf"),
            n_units=0,
            n_pairings=0,
            n_failed=20,
            failures=("HTTPStatusError: 401 authentication_error",),
        )
    )
    assert "401 authentication_error" in out
    assert "no usable pairings" in out  # diff line suppressed on empty batch
    assert "+0.000" not in out  # no fake measured "tie"

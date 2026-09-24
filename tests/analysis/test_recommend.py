"""M2: zero-eval recommendations from recorded traffic."""

from __future__ import annotations

import pytest

from ctrlrtn.analysis.recommend import build_recommendations
from ctrlrtn.recorder.store import (
    InMemoryTraceStore,
    SqliteTraceStore,
    UseCaseRanking,
)
from ctrlrtn.recorder.trace import Trace


def _model_trace(model, ts) -> Trace:
    return Trace(
        method="POST",
        path="/v1/messages",
        query="",
        request_headers={},
        request_body=b"{}",
        status_code=200,
        response_headers={},
        response_body=b"{}",
        latency_ms=1.0,
        model=model,
        use_case_key="fp:x",
        ts=ts,
    )


def _rank(
    use_case, calls, inp, out, cost, *, cache_read=0, cache_write=0
) -> UseCaseRanking:
    return UseCaseRanking(
        use_case=use_case,
        calls=calls,
        input_tokens=inp,
        output_tokens=out,
        avg_latency_ms=100.0,
        cost_usd=cost,
        cache_read_tokens=cache_read,
        cache_write_tokens=cache_write,
    )


def test_flags_spend_concentration():
    rankings = [
        _rank("a", 21, 260_000, 6_000, 0.88),
        _rank("b", 5, 1_000, 100, 0.02),
    ]
    models = {"a": "claude-sonnet-4-5", "b": "claude-sonnet-4-5"}
    recs = build_recommendations(rankings, models)
    focus = [r for r in recs if r.kind == "focus"]
    assert focus and focus[0].use_case == "a"
    assert recs[0].kind == "focus"  # focus is surfaced first


def test_flags_caching_when_no_cache_used():
    rankings = [_rank("a", 21, 260_000, 6_000, 0.88, cache_read=0)]
    recs = build_recommendations(rankings, {"a": "claude-sonnet-4-5"})
    assert any(r.kind == "enable_caching" for r in recs)


def test_flags_caching_waste_when_writes_never_read():
    # Caching is on (writes) but nothing is read back — a net loss.
    rankings = [
        _rank("a", 21, 260_000, 6_000, 0.88, cache_read=0, cache_write=50_000)
    ]
    recs = build_recommendations(rankings, {"a": "claude-sonnet-4-5"})
    waste = [r for r in recs if r.kind == "caching_waste"]
    assert waste and waste[0].use_case == "a"
    assert recs[0].kind == "caching_waste"  # surfaced first (active money leak)


def test_no_enable_caching_when_caching_already_active():
    # Writes present => caching is enabled; must not also say "enable caching".
    rankings = [
        _rank("a", 21, 260_000, 6_000, 0.88, cache_read=0, cache_write=50_000)
    ]
    recs = build_recommendations(rankings, {"a": "claude-sonnet-4-5"})
    assert not any(r.kind == "enable_caching" for r in recs)


def test_no_caching_waste_when_reads_present():
    # Caching is working (reads materialized) — neither waste nor enable.
    rankings = [
        _rank(
            "a",
            21,
            260_000,
            6_000,
            0.88,
            cache_read=200_000,
            cache_write=50_000,
        )
    ]
    recs = build_recommendations(rankings, {"a": "claude-sonnet-4-5"})
    assert not any(r.kind == "caching_waste" for r in recs)
    assert not any(r.kind == "enable_caching" for r in recs)


def test_no_caching_rec_when_cache_already_used():
    rankings = [_rank("a", 21, 260_000, 6_000, 0.88, cache_read=100_000)]
    recs = build_recommendations(rankings, {"a": "claude-sonnet-4-5"})
    assert not any(r.kind == "enable_caching" for r in recs)


def test_downgrade_candidate_savings_estimate():
    # sonnet ($3/M in) -> haiku ($1/M in) on 1M input, 0 output => save $2.
    rankings = [_rank("a", 10, 1_000_000, 0, 3.0)]
    recs = build_recommendations(rankings, {"a": "claude-sonnet-4-5"})
    dg = [r for r in recs if r.kind == "downgrade_candidate"]
    assert dg
    assert abs(dg[0].est_savings_usd - 2.0) < 1e-9


def test_no_downgrade_when_already_cheapest_or_unpriced():
    rankings = [
        _rank("a", 10, 1_000_000, 0, 1.0),  # haiku: no cheaper sibling
        _rank("b", 10, 1_000_000, 0, 0.0),  # unknown model
    ]
    models = {"a": "claude-haiku-4-5", "b": "mystery-model"}
    recs = build_recommendations(rankings, models)
    assert not any(r.kind == "downgrade_candidate" for r in recs)


def test_no_recs_for_empty():
    assert build_recommendations([], {}) == []


def test_caching_outranks_downgrade():
    # The safe, free win must never be ranked below the risky model change.
    rankings = [_rank("a", 21, 260_000, 6_000, 0.88)]
    recs = build_recommendations(rankings, {"a": "claude-sonnet-4-5"})
    kinds = [r.kind for r in recs]
    assert kinds.index("enable_caching") < kinds.index("downgrade_candidate")


def test_downgrade_savings_use_current_model_not_recorded_cost():
    # Recorded cost ($0.50, from mixed history) must NOT be the baseline; the
    # saving is sonnet->haiku on these tokens (3.0 - 1.0 = 2.0).
    rankings = [_rank("a", 10, 1_000_000, 0, 0.50)]
    recs = build_recommendations(rankings, {"a": "claude-sonnet-4-5"})
    dg = [r for r in recs if r.kind == "downgrade_candidate"]
    assert dg and abs(dg[0].est_savings_usd - 2.0) < 1e-9


def test_no_downgrade_below_savings_floor():
    # cost passes the gate, but sonnet->haiku on 2k input saves < $0.005.
    rankings = [_rank("a", 10, 2_000, 0, 0.01)]
    recs = build_recommendations(rankings, {"a": "claude-sonnet-4-5"})
    assert not any(r.kind == "downgrade_candidate" for r in recs)


def test_downgrade_picks_cheapest_candidate_by_cost():
    # opus ladder is (sonnet, haiku); the headline must use the cheapest (haiku).
    rankings = [_rank("a", 10, 1_000_000, 0, 15.0)]
    recs = build_recommendations(rankings, {"a": "claude-opus-4"})
    dg = [r for r in recs if r.kind == "downgrade_candidate"]
    assert dg and "claude-haiku-4-5" in dg[0].summary
    assert abs(dg[0].est_savings_usd - 14.0) < 1e-9  # opus 15 -> haiku 1


@pytest.mark.parametrize(
    "make_store",
    [InMemoryTraceStore, lambda: SqliteTraceStore(":memory:")],
)
async def test_use_case_models_latest_non_null_by_ts(make_store):
    store = make_store()
    try:
        await store.save(_model_trace("claude-opus-4", ts=1.0))
        await store.save(_model_trace(None, ts=2.0))  # latest, but no model
        assert store.use_case_models()["fp:x"] == "claude-opus-4"
    finally:
        if isinstance(store, SqliteTraceStore):
            store.close()


@pytest.mark.parametrize(
    "make_store",
    [InMemoryTraceStore, lambda: SqliteTraceStore(":memory:")],
)
async def test_use_case_models_orders_by_ts_not_insertion(make_store):
    store = make_store()
    try:
        await store.save(_model_trace("claude-haiku-4-5", ts=2.0))
        await store.save(_model_trace("claude-sonnet-4-5", ts=1.0))  # older ts
        assert store.use_case_models()["fp:x"] == "claude-haiku-4-5"
    finally:
        if isinstance(store, SqliteTraceStore):
            store.close()


async def test_recommendations_through_the_store():
    store = SqliteTraceStore(":memory:")
    try:
        for _ in range(4):
            await store.save(
                Trace(
                    method="POST",
                    path="/v1/messages",
                    query="",
                    request_headers={},
                    request_body=b'{"model":"claude-sonnet-4-5"}',
                    status_code=200,
                    response_headers={},
                    response_body=b"{}",
                    latency_ms=10.0,
                    model="claude-sonnet-4-5",
                    input_tokens=65_000,
                    output_tokens=1_500,
                    cost_usd=0.22,
                    use_case_key="fp:x",
                )
            )
        recs = build_recommendations(store.rankings(), store.use_case_models())
        kinds = {r.kind for r in recs}
        assert {"focus", "enable_caching", "downgrade_candidate"} <= kinds
    finally:
        store.close()

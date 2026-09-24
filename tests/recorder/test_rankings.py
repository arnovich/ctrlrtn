"""M0: use-cases ranked by spend (token totals), and the CLI rendering."""

from __future__ import annotations

import os
import tempfile
import time

from ctrlrtn.cli import commands as cli
from ctrlrtn.cli.commands import build_app
from ctrlrtn.cli.render import render_rankings
from ctrlrtn.recorder.sqlite.store import (
    _BUCKET_SERIES,
    _MODEL_RANKINGS,
    _RANKINGS,
    _where,
)
from ctrlrtn.recorder.store import (
    InMemoryTraceStore,
    SqliteTraceStore,
    UseCaseRanking,
)
from ctrlrtn.recorder.trace import Trace


def _trace(
    key, input_tokens, output_tokens, latency=10.0, ts=0.0, cost=None
) -> Trace:
    return Trace(
        method="POST",
        path="/v1/chat/completions",
        query="",
        request_headers={},
        request_body=b"{}",
        status_code=200,
        response_headers={},
        response_body=b"",
        latency_ms=latency,
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        use_case_key=key,
        ts=ts,
        cost_usd=cost,
    )


async def test_inmemory_rankings_order_and_sums():
    store = InMemoryTraceStore()
    for trace in [
        _trace("fp:a", 100, 50),
        _trace("fp:a", 100, 50),
        _trace("fp:b", 10, 5),
        _trace(None, 1, 1),
    ]:
        await store.save(trace)

    rows = store.rankings()

    assert rows[0].use_case == "fp:a"
    assert rows[0].calls == 2
    assert rows[0].input_tokens == 200
    assert rows[0].output_tokens == 100
    assert rows[0].total_tokens == 300
    assert "(unkeyed)" in [row.use_case for row in rows]


async def test_sqlite_rankings_match_inmemory():
    store = SqliteTraceStore(":memory:")
    try:
        for trace in [_trace("fp:a", 100, 50), _trace("fp:b", 10, 5)]:
            await store.save(trace)
        rows = store.rankings()
        assert [r.use_case for r in rows] == ["fp:a", "fp:b"]
        assert rows[0].total_tokens == 150
    finally:
        store.close()


async def test_spend_cli_surfaces_unknown_pricing(
    tmp_path, monkeypatch, capsys
):
    path = str(tmp_path / "spend.db")
    store = SqliteTraceStore(path)
    known = _trace("tag:x", 1, 1, ts=time.time(), cost=0.25)
    known.model = "gpt-4o"
    unknown = _trace("tag:x", 1, 1, ts=time.time(), cost=None)
    unknown.model = "brand-new-model"
    await store.save(known)
    await store.save(unknown)
    store.close()
    monkeypatch.setattr(cli, "_db_path", lambda: path)

    cli.main(["spend"])

    output = capsys.readouterr().out
    assert "$0.2500 total" in output
    assert "Unknown-priced calls: 1 total" in output


async def test_model_rankings_group_by_the_model_actually_served():
    store = SqliteTraceStore(":memory:")
    try:
        # Requested sonnet, served sonnet (no experiment).
        t1 = _trace("tag:editor", 100, 50, cost=1.0)
        t1.model = "claude-sonnet-4-5"
        # Requested sonnet, but the router swapped the arm to haiku: the split
        # must attribute this call to HAIKU, or the A/B share is invisible.
        t2 = _trace("tag:editor", 100, 50, cost=0.1)
        t2.model = "claude-sonnet-4-5"
        t2.served_model = "claude-haiku-4-5"
        # No model at all (error before parse) -> "(unknown)".
        t3 = _trace("tag:editor", 0, 0, cost=None)
        for t in (t1, t2, t3):
            await store.save(t)
        rows = store.model_rankings()
        by_model = {r.model: r for r in rows}
        assert by_model["claude-sonnet-4-5"].calls == 1
        assert by_model["claude-haiku-4-5"].calls == 1
        assert by_model["claude-haiku-4-5"].cost_usd == 0.1
        assert by_model["(unknown)"].calls == 1
        # Sorted by spend: sonnet ($1.0) first.
        assert rows[0].model == "claude-sonnet-4-5"
    finally:
        store.close()


async def test_aggregates_take_a_trailing_window_and_default_to_all_history():
    """rankings/model_rankings/tasks scope to `since` so the console's table
    window can narrow them; without it they still span everything recorded."""
    store = SqliteTraceStore(":memory:")
    try:
        now = 100_000.0
        old = _trace("fp:old", 10, 5, ts=now - 7200, cost=1.0)
        old.served_model = "opus"
        old.task_id = "task-old"
        recent = _trace("fp:new", 1, 1, ts=now - 60, cost=0.25)
        recent.served_model = "haiku"
        recent.task_id = "task-new"
        for trace in (old, recent):
            await store.save(trace)

        assert {r.use_case for r in store.rankings()} == {"fp:old", "fp:new"}
        assert {m.model for m in store.model_rankings()} == {"opus", "haiku"}
        assert {t.task_id for t in store.tasks()} == {"task-old", "task-new"}

        hour_ago = now - 3600
        assert [r.use_case for r in store.rankings(since=hour_ago)] == [
            "fp:new"
        ]
        assert [m.model for m in store.model_rankings(since=hour_ago)] == [
            "haiku"
        ]
        assert [t.task_id for t in store.tasks(since=hour_ago)] == ["task-new"]
        # The window composes with baseline_only rather than replacing it.
        assert (
            store.rankings(since=hour_ago, baseline_only=True)[0].cost_usd
            == 0.25
        )
        assert store.rankings(since=now) == []
    finally:
        store.close()


async def test_windowed_queries_use_the_trace_time_index():
    """Every "since T" query must range-scan ix_traces_ts, not the table: at
    300k traces that is ~0.5ms instead of ~8ms, on every console refresh."""
    store = SqliteTraceStore(":memory:")
    try:
        await store.save(_trace("fp:a", 1, 1, ts=100.0, cost=0.1))
        indexes = {
            row[1]
            for row in store._conn.execute(
                "PRAGMA index_list('traces')"
            ).fetchall()
        }
        assert "ix_traces_ts" in indexes
        for sql, params in (
            (_RANKINGS.format(where=_where(["ts >= ?"])), ("(unkeyed)", 0.0)),
            (
                _MODEL_RANKINGS.format(where=_where(["ts >= ?"])),
                ("(unknown)", 0.0),
            ),
            (_BUCKET_SERIES, (10, 0.0)),
        ):
            plan = " ".join(
                row[3]
                for row in store._conn.execute(
                    "EXPLAIN QUERY PLAN " + sql, params
                ).fetchall()
            )
            assert "ix_traces_ts" in plan, plan
    finally:
        store.close()


async def test_bucket_series_buckets_zero_fills_and_windows():
    store = SqliteTraceStore(":memory:")
    try:
        now = 100_000.0  # minute 1666
        for ts, cost in [
            (now - 5, 0.5),  # this minute
            (now - 20, None),  # this minute, unenriched (cost NULL)
            (now - 125, 0.25),  # two minutes back
            (now - 40 * 60, 9.9),  # outside a 5-minute window
        ]:
            await store.save(_trace("fp:a", 1, 1, ts=ts, cost=cost))
        series = store.bucket_series(seconds=60, buckets=5, now=now)
        assert len(series) == 5  # zero-filled: quiet minutes are 0-bars
        assert [b["calls"] for b in series] == [0, 0, 1, 0, 2]
        assert [b["cost_usd"] for b in series] == [0, 0, 0.25, 0, 0.5]
        # Latency averages per bucket (all traces at 10ms); tokens sum in+out
        # (1+1 per trace). Empty buckets are zero, not missing.
        assert [b["latency_ms"] for b in series] == [0, 0, 10.0, 0, 10.0]
        assert [b["tokens"] for b in series] == [0, 0, 2, 0, 4]
        # Buckets are bucket-aligned epochs, oldest first.
        assert series[0]["ts"] % 60 == 0
        assert series[-1]["ts"] - series[0]["ts"] == 4 * 60
    finally:
        store.close()


async def test_bucket_series_takes_a_finer_bucket_size():
    store = SqliteTraceStore(":memory:")
    try:
        now = 100_000.0  # exactly on a bucket edge: the last bucket just began
        await store.save(_trace("fp:a", 1, 1, ts=now - 3, cost=0.5))
        await store.save(_trace("fp:a", 1, 1, ts=now - 13, cost=0.5))
        series = store.bucket_series(seconds=10, buckets=3, now=now)
        # now-13 -> bucket [now-20, now-10); now-3 -> [now-10, now); the
        # trailing (current, partial) bucket [now, now+10) is still empty.
        assert [b["calls"] for b in series] == [1, 1, 0]
        assert series[-1]["ts"] - series[0]["ts"] == 2 * 10
    finally:
        store.close()


async def test_bucket_series_averages_latency_and_ignores_null_tokens():
    store = SqliteTraceStore(":memory:")
    try:
        now = 100_000.0
        # Two calls in the same bucket: latencies 10 and 30 -> avg 20; one is
        # unenriched (tokens None) and must count 0 tokens, not poison the sum.
        await store.save(_trace("fp:a", 5, 5, latency=10.0, ts=now - 3))
        await store.save(_trace("fp:a", None, None, latency=30.0, ts=now - 4))
        series = store.bucket_series(seconds=60, buckets=1, now=now)
        assert series[0]["calls"] == 2
        assert series[0]["latency_ms"] == 20.0
        assert series[0]["tokens"] == 10
    finally:
        store.close()


async def test_bucket_series_on_an_empty_store_is_all_zero():
    store = SqliteTraceStore(":memory:")
    try:
        series = store.bucket_series(seconds=60, buckets=3, now=100_000.0)
        assert [b["calls"] for b in series] == [0, 0, 0]
        assert [b["cost_usd"] for b in series] == [0, 0, 0]
        assert [b["latency_ms"] for b in series] == [0, 0, 0]
        assert [b["tokens"] for b in series] == [0, 0, 0]
    finally:
        store.close()


def test_render_rankings_table():
    out = render_rankings([UseCaseRanking("fp:a", 2, 200, 100, 12.0)])
    assert "use-case" in out
    assert "fp:a" in out


def test_render_rankings_empty():
    assert "No traffic" in render_rankings([])


def test_build_app_wires_recorder():
    db = os.path.join(tempfile.mkdtemp(), "traces.db")
    os.environ["CTRLRTN_DB"] = db
    try:
        app = build_app()
        assert app.state.recorder is not None
    finally:
        os.environ.pop("CTRLRTN_DB", None)

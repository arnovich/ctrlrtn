"""Header-propagation gate: store counts (windowed/scoped), pure report, render.

The gate is honest only if it (a) ignores unkeyed calls as non-sub-agents,
(b) needs several linked tasks not one fluke, (c) reads a recent window, and
(d) counts only real completion calls. Each is pinned below.
"""

from __future__ import annotations

from ctrlrtn.analysis.propagation import (
    MIN_MULTI_AGENT_TASKS,
    build_propagation_report,
)
from ctrlrtn.cli.render import render_propagation
from ctrlrtn.recorder.store import SqliteTraceStore
from ctrlrtn.recorder.trace import Trace


def _row(task_id, use_case_key, calls):
    return {"task_id": task_id, "use_case_key": use_case_key, "calls": calls}


def _tasks(n, agents=("fp:orch", "fp:analyst", "fp:editor")):
    """n tasks, each linking the same set of sub-agents under one task id."""
    return [_row(f"ed{i}", a, 1) for i in range(n) for a in agents]


# --- pure report -----------------------------------------------------------


def test_unkeyed_call_is_not_a_second_subagent():
    # A tagged task with one keyed + one unkeyed call must NOT read as spanning
    # two sub-agents (an unproven result must never read as proven).
    report = build_propagation_report(
        [_row("ed1", "fp:orch", 1), _row("ed1", None, 1)]
    )
    assert report.multi_agent_tasks == 0
    assert not report.propagated


def test_one_linked_task_is_not_enough():
    report = build_propagation_report(_tasks(1))
    assert report.multi_agent_tasks == 1
    assert not report.propagated  # needs MIN_MULTI_AGENT_TASKS
    assert "NO CROSS-AGENT LINK" in render_propagation(report)


def test_enough_linked_tasks_propagate():
    report = build_propagation_report(_tasks(MIN_MULTI_AGENT_TASKS))
    assert report.multi_agent_tasks == MIN_MULTI_AGENT_TASKS
    assert report.propagated
    assert "PROPAGATING" in render_propagation(report)


def test_mostly_untagged_is_not_propagating():
    rows = _tasks(MIN_MULTI_AGENT_TASKS) + [_row(None, "fp:editor", 50)]
    report = build_propagation_report(rows)
    assert report.tasked_fraction < 0.5
    assert not report.propagated
    assert "NOT PROPAGATING" in render_propagation(report)


def test_empty_is_no_data():
    report = build_propagation_report([])
    assert report.total_calls == 0
    assert not report.propagated
    assert "NO DATA" in render_propagation(report)


def test_untasked_calls_counted_but_not_grouped():
    rows = [
        _row(None, "fp:editor", 4),  # untasked
        _row("ed1", "fp:orch", 1),
        _row("ed1", "fp:editor", 1),
    ]
    report = build_propagation_report(rows)
    assert report.total_calls == 6
    assert report.untasked_calls == 4
    assert report.n_tasks == 1
    assert report.multi_agent_tasks == 1


def test_focus_present_but_never_linked():
    # editor is tagged but always alone in its task -> co-located 0.
    rows = [_row("ed1", "fp:editor", 1), _row("ed2", "fp:editor", 1)]
    report = build_propagation_report(rows, focus_use_case="fp:editor")
    assert report.focus_task_count == 2
    assert report.focus_co_located_tasks == 0
    assert "never shares a task" in render_propagation(report)


def test_focus_absent_from_tagged_tasks():
    rows = _tasks(1, agents=("fp:orch", "fp:analyst"))
    report = build_propagation_report(rows, focus_use_case="fp:editor")
    assert report.focus_task_count == 0
    assert "in NO tagged task" in render_propagation(report)


# --- store query: windowing + scoping --------------------------------------


def _trace(
    task_id, use_case_key, *, method="POST", status=200, path="/v1/messages"
) -> Trace:
    return Trace(
        method=method,
        path=path,
        query="",
        request_headers={},
        request_body=b"{}",
        status_code=status,
        response_headers={},
        response_body=b"{}",
        latency_ms=1.0,
        use_case_key=use_case_key,
        task_id=task_id,
    )


async def test_counts_only_completion_2xx_calls():
    store = SqliteTraceStore(":memory:")
    try:
        await store.save(_trace("ed1", "fp:orch"))  # kept
        await store.save(_trace("ed1", "fp:other", path="/v1/chat/completions"))
        await store.save(_trace("ed1", "fp:orch", status=500))  # error -> drop
        await store.save(_trace("ed1", "fp:x", method="GET", path="/v1/models"))
        await store.save(
            _trace("ed1", "fp:x", path="/v1/messages/count_tokens")
        )  # drop
        rows = store.task_use_case_counts()
        keys = {(r["task_id"], r["use_case_key"]) for r in rows}
        assert keys == {("ed1", "fp:orch"), ("ed1", "fp:other")}
    finally:
        store.close()


async def test_window_limits_to_recent_calls():
    store = SqliteTraceStore(":memory:")
    try:
        for i in range(5):
            await store.save(_trace(f"ed{i}", "fp:orch"))
        rows = store.task_use_case_counts(window=2)
        assert sum(r["calls"] for r in rows) == 2  # only the 2 most recent
    finally:
        store.close()


async def test_store_preserves_task_id_and_unkeyed_nulls():
    store = SqliteTraceStore(":memory:")
    try:
        await store.save(_trace("ed1", "fp:orch"))
        await store.save(_trace(None, None))  # untasked + unkeyed
        rows = store.task_use_case_counts()
        by_key = {(r["task_id"], r["use_case_key"]): r["calls"] for r in rows}
        assert by_key[("ed1", "fp:orch")] == 1
        assert by_key[(None, None)] == 1  # both NULLs survive as None
    finally:
        store.close()

"""Operator-defined session identity and spend attribution."""

from __future__ import annotations

import pytest

from ctrlrtn.cli import commands as cli
from ctrlrtn.cli.render import render_sessions
from ctrlrtn.config import Settings
from ctrlrtn.policy.budget import BudgetPolicy
from ctrlrtn.recorder.models import SessionSummary
from ctrlrtn.recorder.store import InMemoryTraceStore, SqliteTraceStore
from ctrlrtn.recorder.trace import Trace
from ctrlrtn.telemetry.enrich import enrich_trace


def _trace(
    session_id: str | None,
    use_case: str,
    cost: float | None,
    *,
    status: int = 200,
) -> Trace:
    return Trace(
        method="POST",
        path="/v1/messages",
        query="",
        request_headers={},
        request_body=b"{}",
        status_code=status,
        response_headers={},
        response_body=b"{}",
        latency_ms=1.0,
        model="claude-sonnet-4-5",
        input_tokens=100,
        output_tokens=10,
        cost_usd=cost,
        use_case_key=use_case,
        session_id=session_id,
    )


@pytest.mark.parametrize(
    ("value", "expected"),
    [("session-7", "session-7"), ("", None), (None, None)],
)
def test_enrich_captures_and_normalizes_session_header(value, expected):
    headers = {} if value is None else {"x-ctrlrtn-session": value}
    trace = Trace(
        method="POST",
        path="/v1/messages",
        query="",
        request_headers=headers,
        request_body=b'{"model":"claude-sonnet-4-5","system":"X"}',
        status_code=200,
        response_headers={},
        response_body=b"{}",
        latency_ms=1.0,
    )

    enrich_trace(trace)

    assert trace.session_id == expected


@pytest.mark.parametrize(
    "make_store",
    [InMemoryTraceStore, lambda: SqliteTraceStore(":memory:")],
)
async def test_sessions_group_spend_and_usage(make_store):
    store = make_store()
    try:
        await store.save(_trace("session-7", "fp:editor", 0.80))
        await store.save(_trace("session-7", "fp:analyst", 0.10, status=500))
        await store.save(_trace("session-7", "fp:editor", None))
        await store.save(_trace(None, "fp:editor", 0.05))

        rows = store.sessions()
        by_id = {row.session_id: row for row in rows}
        session = by_id["session-7"]
        assert session.calls == 3
        assert session.use_cases == 2
        assert session.errors == 1
        assert session.unknown_cost_calls == 1
        assert session.input_tokens == 300
        assert session.output_tokens == 30
        assert session.cost_usd == pytest.approx(0.90)
        assert "(unsessioned)" in by_id
        assert rows[0].session_id == "session-7"
        totals, unknown = store.session_spend_state()
        assert totals == {"session-7": 0.9}
        assert unknown == {"session-7"}
        assert [row.session_id for row in store.sessions(limit=1)] == [
            "session-7"
        ]
    finally:
        if isinstance(store, SqliteTraceStore):
            store.close()


async def test_session_id_persists_and_is_shown_by_cli(
    tmp_path, monkeypatch, capsys
):
    path = str(tmp_path / "sessions.db")
    store = SqliteTraceStore(path)
    await store.save(_trace("session-7", "fp:editor", 0.25))
    assert store.get(1)["session_id"] == "session-7"
    store.close()
    monkeypatch.setattr(cli, "_db_path", lambda: path)
    monkeypatch.setattr(
        cli,
        "load_settings",
        lambda: Settings(
            db_path=path,
            budget_policy=BudgetPolicy(session_limit_usd=1.0),
        ),
    )

    cli.main(["sessions"])

    output = capsys.readouterr().out
    assert "session-7" in output
    assert "$0.2500" in output
    assert "use-cases" in output
    assert "unknown" in output
    assert "$0.7500" in output


def test_sessions_remaining_distinguishes_known_unknown_and_no_policy():
    rows = [
        SessionSummary(
            session_id="known",
            calls=1,
            cost_usd=0.25,
            input_tokens=1,
            output_tokens=1,
            use_cases=1,
            errors=0,
            unknown_cost_calls=0,
        ),
        SessionSummary(
            session_id="unknown",
            calls=1,
            cost_usd=0.25,
            input_tokens=1,
            output_tokens=1,
            use_cases=1,
            errors=0,
            unknown_cost_calls=1,
        ),
    ]

    with_policy = render_sessions(rows, session_limit_usd=1.0)
    without_policy = render_sessions(rows)

    assert "$0.7500" in with_policy
    assert "N/A" in with_policy
    assert "remaining" in with_policy
    assert "remaining" not in without_policy


async def test_reenrich_recovers_session_id_from_stored_headers():
    store = SqliteTraceStore(":memory:")
    try:
        trace = _trace(None, "fp:editor", 0.25)
        trace.request_headers = {"x-ctrlrtn-session": "session-7"}
        await store.save(trace)

        store.reenrich(enrich_trace)

        assert store.get(1)["session_id"] == "session-7"
    finally:
        store.close()

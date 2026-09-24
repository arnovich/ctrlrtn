"""The TUI: data functions, the read-only guarantee, the
missing-extra / missing-DB fallbacks, and Textual Pilot behaviour."""

from __future__ import annotations

import sqlite3
import subprocess
import time
from types import SimpleNamespace

import pytest

pytest.importorskip("textual")  # skips where the 'tui' extra isn't installed

from textual.widgets import (
    Button,
    DataTable,
    Header,
    Input,
    Label,
    Sparkline,
    Static,
)

from ctrlrtn.cli import commands as cli
from ctrlrtn.cli.console import (
    _DEFAULT_GRAPH_WINDOW,
    _DEFAULT_TABLE_WINDOW,
    _EMPTY,
    _GRAPH_WINDOWS,
    _PAGE_SIZE,
    _TABLE_WINDOWS,
    ConfirmScreen,
    ConsoleApp,
    ConsoleUnavailable,
    DiscoveredWorkflowDagScreen,
    GitConfigScreen,
    LiveExperimentScreen,
    OfflineExperimentScreen,
    RouteScreen,
    ShadowExperimentScreen,
    WindowPickerScreen,
    WorkflowDiscoveryScopeScreen,
    WorkflowIdentifyScreen,
    WorkflowProposalVerifyScreen,
    call_detail,
    command_panel_text,
    experiment_detail,
    field_matches,
    graph_axis,
    graph_label,
    job_detail,
    load_state,
    page_heading,
    run_console,
    shadow_detail,
    task_detail,
    usecase_detail,
)
from ctrlrtn.eval.tripwire import run_tripwire
from ctrlrtn.jobs import Job, Worker
from ctrlrtn.policy.budget import BudgetPolicy
from ctrlrtn.policy.experiment import (
    BASELINE,
    CANDIDATE,
    Experiment,
)
from ctrlrtn.policy.shadow import ShadowExperiment
from ctrlrtn.recorder.store import (
    Outcome,
    SqliteTraceStore,
)
from ctrlrtn.recorder.trace import Trace
from ctrlrtn.workflow import discovery_job
from ctrlrtn.workflow.identity import (
    WorkflowEvent,
    WorkflowIdentity,
)


def _trace(
    task_id,
    arm,
    *,
    use_case,
    served_model,
    cost,
    experiment_id,
    ts=0.0,
    body=b'{"system":"x"}',
):
    return Trace(
        method="POST",
        path="/v1/messages",
        query="",
        request_headers={},
        request_body=body,
        status_code=200,
        response_headers={},
        response_body=b"",
        latency_ms=10.0,
        task_id=task_id,
        experiment_id=experiment_id,
        arm=arm,
        served_model=served_model,
        use_case_key=use_case,
        cost_usd=cost,
        ts=ts,
    )


@pytest.fixture
def populated_db(tmp_path):
    """A read-write store seeds the DB synchronously (via the sync insert
    helpers), then hands back the path for read-only opens."""
    db = str(tmp_path / "console.db")
    writer = SqliteTraceStore(db)
    writer.create_experiment(
        Experiment("tag:editor", "claude-haiku-4-5", 50, experiment_id="exp:e")
    )
    for i in range(3):
        writer._insert(
            _trace(
                f"b{i}",
                BASELINE,
                use_case="tag:editor",
                served_model="opus",
                cost=0.9,
                experiment_id="exp:e",
            )
        )
        writer._insert_outcome(Outcome(f"b{i}", success=True))
    writer._insert(
        _trace(
            "c0",
            CANDIDATE,
            use_case="tag:editor",
            served_model="haiku",
            cost=0.2,
            experiment_id="exp:e",
        )
    )
    writer._insert_outcome(Outcome("c0", success=True))
    writer.close()
    return db


@pytest.fixture
def store(populated_db):
    ro = SqliteTraceStore(populated_db, read_only=True)
    yield ro
    ro.close()


def _experiment(store, experiment_id="exp:e"):
    return next(
        e for e in store.experiments() if e.experiment_id == experiment_id
    )


# --- read-only guarantee ---------------------------------------------------


def test_read_only_store_blocks_writes(populated_db):
    ro = SqliteTraceStore(populated_db, read_only=True)
    try:
        with pytest.raises(sqlite3.OperationalError):
            ro.create_experiment(
                Experiment("tag:z", "m", 50, experiment_id="exp:z")
            )
    finally:
        ro.close()


async def test_console_queues_scoped_workflow_discovery(tmp_path):
    db_path = tmp_path / "scoped-discovery.db"
    store = SqliteTraceStore(db_path)
    try:
        for index, task_id in enumerate(("scope-a", "scope-b")):
            for call, model in enumerate(("model-a", "model-b")):
                store._insert(
                    _trace(
                        task_id,
                        None,
                        use_case=f"tag:step-{call}",
                        served_model=model,
                        cost=0.01,
                        experiment_id=None,
                        ts=float(index * 2 + call),
                    )
                )
    finally:
        store.close()

    reader = SqliteTraceStore(db_path, read_only=True)
    try:
        app = ConsoleApp(reader, db_path=db_path, refresh_seconds=999)
        async with app.run_test() as pilot:
            await pilot.pause()
            await pilot.press("f")
            await pilot.pause()
            app.screen.query_one("#discovery-scope-model", Input).value = (
                "model-a"
            )
            app.screen.query_one("#discovery-scope-queue", Button).press()
            await pilot.pause()
            assert "frozen trace(s)" in app._last_notice
    finally:
        reader.close()
    writer = SqliteTraceStore(db_path)
    try:
        job = writer.jobs()[0]
        assert job.config["scope"]["model"] == "model-a"
        assert len(job.config["trace_ids"]) == 4
    finally:
        writer.close()


def test_read_only_open_does_not_create_a_missing_db(tmp_path):
    missing = tmp_path / "nope.db"
    with pytest.raises(sqlite3.OperationalError):
        SqliteTraceStore(str(missing), read_only=True)
    assert not missing.exists()  # a viewer must never materialize the DB


# --- data functions --------------------------------------------------------


def test_load_state_returns_lists_and_live_budget_status(store):
    state = load_state(
        store,
        budget_policy=BudgetPolicy(
            global_daily_usd=10.0,
            use_case_daily_usd={"tag:editor": 5.0},
            use_case_fallback_usd={"tag:editor": 4.0},
        ),
        kill_switch=True,
        now=1.0,
    )
    assert any(e.experiment_id == "exp:e" for e in state["experiments"])
    assert any(r.use_case == "tag:editor" for r in state["rankings"])
    assert any(t.task_id == "b0" for t in state["tasks"])
    assert len(state["calls"]) == 4  # 3 baseline + 1 candidate traces
    # Newest first — the feed reads top-down like a tail -f.
    ids = [c["id"] for c in state["calls"]]
    assert ids == sorted(ids, reverse=True)
    assert "Kill switch: ON" in state["budget_status"]
    assert "Global daily: $2.9000 / $10.0000" in state["budget_status"]
    assert "fallback at $4.0000" in state["budget_status"]
    assert "Evidence-approved fallbacks today: 0" in state["budget_status"]
    assert state["jobs"] == []


def test_load_state_and_detail_expose_job_progress(populated_db):
    writer = SqliteTraceStore(populated_db)
    writer.create_job(
        Job(
            "replay_eval",
            {"candidate": "local"},
            job_id="job:offline",
            progress_current=4,
            progress_total=10,
            progress_message="judging",
        )
    )
    writer.close()
    reader = SqliteTraceStore(populated_db, read_only=True)
    try:
        state = load_state(reader)
    finally:
        reader.close()
    assert state["jobs"][0].job_id == "job:offline"
    detail = job_detail(state["jobs"][0])
    assert "4 / 10" in detail
    assert "judging" in detail


def test_load_state_buckets_the_series_over_the_chosen_graph_window(store):
    # The default window and a wider one must each yield exactly their own
    # bucket count, so the graphs stay a fixed-width picture of a wider span.
    default = load_state(store)
    assert len(default["series"]) == _DEFAULT_GRAPH_WINDOW.buckets
    week = next(w for w in _GRAPH_WINDOWS if w.label == "7d")
    assert len(load_state(store, graph_window=week)["series"]) == week.buckets


def test_every_graph_window_is_one_bucket_grid_over_its_own_span(store):
    labelled = {
        "10m": 600,
        "30m": 1800,
        "1h": 3600,
        "6h": 6 * 3600,
        "12h": 12 * 3600,
        "1d": 86400,
        "7d": 7 * 86400,
        "all": None,  # span unknown until the store says when it started
    }
    assert [w.label for w in _GRAPH_WINDOWS] == list(labelled)
    for window in _GRAPH_WINDOWS:
        assert window.seconds == labelled[window.label]
    # Both pickers offer the same spans in the same order, "all" last.
    assert [w.label for w in _TABLE_WINDOWS] == list(labelled)
    assert _DEFAULT_TABLE_WINDOW.label == "all"


@pytest.mark.parametrize("now", [1_000_000.0, 1_000_123.0, 1788938717.0])
def test_all_window_retains_the_earliest_partial_bucket(now):
    """The current partial bucket must not push the first call off the graph."""
    all_window = next(w for w in _GRAPH_WINDOWS if w.label == "all")
    earliest = now - 86_400
    grid = all_window.resolve(earliest=earliest, now=now)
    first_bucket = int(now // grid.bucket_seconds) - grid.buckets + 1
    assert first_bucket * grid.bucket_seconds <= earliest


def test_the_all_window_buckets_from_the_first_trace_to_now():
    all_window = next(w for w in _GRAPH_WINDOWS if w.label == "all")
    assert all_window.is_all
    day = 86_400.0
    grid = all_window.resolve(earliest=1_000_000.0, now=1_000_000.0 + day)
    assert grid.buckets == 180
    assert grid.bucket_seconds == 483  # reserve the current partial bucket
    assert grid.seconds >= day  # covers the whole history, never less
    # A store with nothing in it (or one minute old) must not divide by ~0.
    empty = all_window.resolve(earliest=None, now=1_000_000.0)
    assert empty.bucket_seconds >= 1
    assert empty.seconds == _DEFAULT_GRAPH_WINDOW.seconds
    # Resolving a fixed window is a no-op.
    half_hour = _DEFAULT_GRAPH_WINDOW
    assert half_hour.resolve(earliest=0.0, now=1e9) == half_hour


async def test_load_state_buckets_all_history_over_the_recorded_span(store):
    all_window = next(w for w in _GRAPH_WINDOWS if w.label == "all")
    series = load_state(store, graph_window=all_window)["series"]
    assert len(series) == 180
    # The fixture's traces sit at ts=0, so "all" spans decades of buckets and
    # every one of them is wider than the 30m window's 10s.
    assert series[1]["ts"] - series[0]["ts"] > 10


def test_experiment_detail_is_the_tripwire_status(store):
    text = experiment_detail(store, _experiment(store))
    assert "VERDICT" in text and "candidate" in text


def test_experiment_detail_matches_default_status_verdict(store):
    # The console must reproduce `experiment status` at DEFAULT thresholds.
    exp = _experiment(store)
    report = run_tripwire(store.experiment_task_rows("exp:e"), now=time.time())
    assert report.verdict in experiment_detail(store, exp)


def test_usecase_detail_shows_cost_experiment_and_model_breakdown(store):
    state = load_state(store)
    text = usecase_detail(
        store, state["rankings"], state["experiments"], "tag:editor"
    )
    assert "tag:editor" in text and "exp:e" in text and "cost" in text
    # The per-use-case model split: 3 opus calls and 1 haiku call.
    assert "models served:" in text
    assert "opus" in text and "haiku" in text


def test_usecase_detail_reports_when_no_experiment_runs(store):
    state = load_state(store)
    text = usecase_detail(store, state["rankings"], [], "tag:editor")
    assert "none running" in text


def test_usecase_detail_shows_the_route_and_savings(populated_db):
    from ctrlrtn.policy.route import Route

    writer = SqliteTraceStore(populated_db)
    writer.set_route(
        Route(
            "tag:editor",
            "claude-haiku-4-5",
            previous_model="claude-sonnet-4-5",
            note="adopted from exp:e",
            ts=0.0,  # before the fixture traffic -> it all counts post-switch
        )
    )
    writer.close()
    # One call the route actually swapped (served_model set, no experiment);
    # the fixture's experiment-arm traffic must NOT count toward the saving.
    writer = SqliteTraceStore(populated_db)
    writer._insert(
        _trace(
            "routed-1",
            None,
            use_case="tag:editor",
            served_model="claude-haiku-4-5",
            cost=0.05,
            experiment_id=None,
            ts=1.0,
        )
    )
    writer.close()
    ro = SqliteTraceStore(populated_db, read_only=True)
    try:
        state = load_state(ro)
        text = usecase_detail(ro, state["rankings"], [], "tag:editor")
        assert (
            "route:       -> claude-haiku-4-5 (was claude-sonnet-4-5)" in text
        )
        assert "swapped: 1 calls" in text  # only the route-swapped call
        assert "saved" in text
        assert "adopted from exp:e" in text
    finally:
        ro.close()


def test_task_detail_shows_outcome(store):
    text = task_detail(load_state(store)["tasks"], "b0")
    assert "task: b0" in text and "ok" in text


def test_call_detail_shows_the_full_trace(store):
    newest = str(load_state(store)["calls"][0]["id"])
    text = call_detail(store, newest)
    # The same full render as `ctrlrtn show <id>`, loaded on selection:
    # summary lines plus the actual request/response bodies.
    assert f"trace #{newest}" in text and "POST /v1/messages" in text
    assert "use_case" in text and "cost" in text and "task" in text
    assert "request body" in text and '"system"' in text  # the recorded body
    assert "response body" in text
    # Unenriched fields (tokens, model...) render "-", not the literal None.
    assert "None" not in text


def test_call_detail_reports_a_vanished_call(store):
    assert "not found" in call_detail(store, "99999")


def test_usecase_detail_survives_a_pre_routes_database(populated_db):
    # A read-only console open runs no DDL: a DB created before routes existed
    # has no routes table, and the detail pane must render, not error.
    import sqlite3 as _sq

    conn = _sq.connect(populated_db)
    conn.execute("DROP TABLE routes")
    conn.commit()
    conn.close()
    ro = SqliteTraceStore(populated_db, read_only=True)
    try:
        state = load_state(ro)
        text = usecase_detail(ro, state["rankings"], [], "tag:editor")
        assert "tag:editor" in text and "models served:" in text
    finally:
        ro.close()


# --- CLI fallbacks: missing extra vs. missing DB ---------------------------


def test_console_without_the_extra_fails_cleanly(monkeypatch):
    import importlib.util
    import sys

    # Simulate: importing the console raises ImportError AND textual is absent.
    monkeypatch.setitem(sys.modules, "ctrlrtn.cli.console", None)
    real_find_spec = importlib.util.find_spec
    monkeypatch.setattr(
        importlib.util,
        "find_spec",
        lambda name, *a, **k: (
            None if name == "textual" else real_find_spec(name, *a, **k)
        ),
    )
    with pytest.raises(SystemExit) as exit_info:
        cli._console(SimpleNamespace(refresh=3.0))
    assert exit_info.value.code == 2


def test_a_real_console_import_bug_is_not_masked(monkeypatch):
    import sys

    # textual IS installed (find_spec unpatched); a failing console import is a
    # genuine bug and must propagate, not read as "install the extra".
    monkeypatch.setitem(sys.modules, "ctrlrtn.cli.console", None)
    with pytest.raises(ImportError):
        cli._console(SimpleNamespace(refresh=3.0))


def test_run_console_on_a_missing_db_raises_unavailable(tmp_path):
    with pytest.raises(ConsoleUnavailable):
        run_console(str(tmp_path / "nope.db"))


def test_cli_console_passes_the_resolved_budget_policy(monkeypatch):
    from ctrlrtn.cli import console
    from ctrlrtn.config import Settings

    captured = {}
    settings = Settings(
        db_path="status.db",
        kill_switch=True,
        budget_policy=BudgetPolicy(global_daily_usd=3.0),
    )
    monkeypatch.setattr(cli, "load_settings", lambda: settings)
    monkeypatch.setattr(
        console,
        "run_console",
        lambda db_path, **kwargs: captured.update(
            {"db_path": db_path, **kwargs}
        ),
    )

    cli._console(
        SimpleNamespace(
            refresh=7.0,
            routing_config="desired.yaml",
            routing_repo="/config/repo",
        )
    )

    assert captured == {
        "db_path": "status.db",
        "refresh_seconds": 7.0,
        "budget_policy": settings.budget_policy,
        "kill_switch": True,
        "routing_config_path": "desired.yaml",
        "routing_repo": "/config/repo",
    }


def test_live_budget_status_counts_successful_fallback_calls(populated_db):
    writer = SqliteTraceStore(populated_db)
    trace = _trace(
        "fallback",
        None,
        use_case="tag:editor",
        served_model="haiku",
        cost=0.1,
        experiment_id=None,
        ts=time.time(),
    )
    trace.budget_fallback = True
    writer._insert(trace)
    writer.close()
    ro = SqliteTraceStore(populated_db, read_only=True)
    try:
        status = load_state(ro)["budget_status"]
        assert "Evidence-approved fallbacks today: 1" in status
    finally:
        ro.close()


async def test_app_mounts_lists_rows_and_shows_a_verdict(store):
    app = ConsoleApp(
        store,
        refresh_seconds=999,
        budget_policy=BudgetPolicy(global_daily_usd=10.0),
        kill_switch=True,
    )
    async with app.run_test() as pilot:
        await pilot.pause()
        assert app.query_one("#experiments", DataTable).row_count >= 1
        assert app.query_one("#usecases", DataTable).row_count >= 1
        assert app.query_one("#tasks", DataTable).row_count >= 1
        assert "VERDICT" in app._last_detail  # highlighted experiment's status
        budget = str(app.query_one("#budget-status").content)
        assert "Kill switch: ON" in budget
        assert "Global daily:" in budget
        assert "Shadows: 0 running" in str(
            app.query_one("#shadow-status").content
        )


async def test_budget_details_toggle_survives_refresh_without_writes(store):
    app = ConsoleApp(store, refresh_seconds=999)
    async with app.run_test(size=(120, 32)) as pilot:
        await pilot.pause()
        status = app.query_one("#budget-status", Static)
        compact = str(status.content)
        assert "b details" in compact
        assert "unknown-priced" in compact and "blocked" in compact
        assert "unreservable_request=" not in compact
        app.query_one("#usecases", DataTable).focus()
        await pilot.press("enter", "b")
        await pilot.pause()
        assert not app._detail_expanded
        assert status.display
        assert "unreservable_request=0" in str(status.content)
        app.action_refresh()
        assert "unreservable_request=0" in str(status.content)
        await pilot.press("B")
        await pilot.pause()
        assert str(status.content) == compact
        assert "o Offline test" in str(
            app.query_one("#experiment-actions", Static).content
        )


def test_compact_budget_retains_active_limits_and_problem_counts():
    from ctrlrtn.cli.render import render_budget_status

    text = render_budget_status(
        BudgetPolicy(
            global_daily_usd=10,
            use_case_daily_usd={"tag:editor": 5},
            session_limit_usd=2,
            reserve_in_flight=True,
        ),
        kill_switch=True,
        daily_total=3,
        daily_by_use_case={"tag:editor": 3},
        unknown_priced_calls=4,
        blocked={"budget": 2, "unpriced_model": 3, "send_failure": 100},
        fallback_calls=6,
        compact=True,
    )
    assert "Kill switch: ON" in text
    assert "$3.0000 / $10.0000 ($7.0000 remaining)" in text
    assert "Use-case limits: 1" in text
    assert "Session limit: $2.0000" in text
    assert "Reservations: on" in text
    assert "4 unknown-priced · 5 blocked · 6 fallbacks" in text


async def test_focusing_a_table_retargets_the_detail(store):
    app = ConsoleApp(store, refresh_seconds=999)
    async with app.run_test() as pilot:
        await pilot.pause()
        app.query_one("#tasks", DataTable).focus()
        await pilot.pause()
        assert "task:" in app._last_detail
        app.query_one("#usecases", DataTable).focus()
        await pilot.pause()
        assert "use-case:" in app._last_detail


async def test_workflow_table_opens_provenance_aware_timeline(
    populated_db, store
):
    writer = SqliteTraceStore(populated_db)
    root = WorkflowIdentity("task-flow", "pipeline", "v1", "plan", "run-plan")
    child = WorkflowIdentity(
        "task-flow", "pipeline", "v1", "act", "run-act", "run-plan"
    )
    writer._insert_workflow_event(
        WorkflowEvent(root, "completed", event_id="flow-1", ts=1)
    )
    writer._insert_workflow_event(
        WorkflowEvent(child, "started", event_id="flow-2", ts=2)
    )
    writer.close()

    app = ConsoleApp(store, refresh_seconds=999)
    async with app.run_test() as pilot:
        await pilot.pause()
        workflows = app.query_one("#workflows", DataTable)
        assert workflows.row_count == 1
        workflows.focus()
        await pilot.pause()
        assert "workflow task: task-flow" in app._last_detail
        assert "run-plan -> (parent)" in app._last_detail
        assert "analysis-only inference" in app._last_detail
        await pilot.press("d")
        await pilot.pause()
        step_runs = app.screen.query_one("#step-runs", DataTable)
        assert step_runs.row_count == 2
        detail = str(app.screen.query_one("#step-detail", Static).content)
        assert "step run:" in detail and "lifecycle:" in detail
        structure = str(app.screen.query_one("#step-structure", Static).content)
        assert "structure (explicit edges" in structure
        await pilot.press("ctrl+f")
        await pilot.press("a", "c", "t")
        await pilot.pause()
        assert step_runs.row_count == 1
        await pilot.press("escape")


async def test_refresh_preserves_the_selection(store):
    app = ConsoleApp(store, refresh_seconds=999)
    async with app.run_test() as pilot:
        await pilot.pause()
        tasks = app.query_one("#tasks", DataTable)
        tasks.focus()
        await pilot.pause()
        tasks.move_cursor(row=2)  # a non-top row
        await pilot.pause()
        chosen = app._selected["tasks"]
        app.action_refresh()  # a full reload
        await pilot.pause()
        assert app._selected["tasks"] == chosen  # survived the refresh


# --- the live call feed (follow mode) ---------------------------------------


def _insert_call(db_path, task_id="live", ts=0.0):
    """Append one committed trace, as the serving process would."""
    writer = SqliteTraceStore(db_path)
    writer._insert(
        _trace(
            task_id,
            BASELINE,
            use_case="tag:editor",
            served_model="opus",
            cost=0.1,
            experiment_id=None,
            ts=ts,
        )
    )
    writer.close()


async def test_calls_feed_follows_the_newest_call(populated_db, store):
    app = ConsoleApp(store, refresh_seconds=999)
    async with app.run_test() as pilot:
        await pilot.pause()
        calls = app.query_one("#calls", DataTable)
        assert calls.row_count == 4
        top = app._selected["calls"]
        _insert_call(populated_db)  # a new call lands while we watch
        app.action_refresh()
        await pilot.pause()
        assert calls.row_count == 5
        assert app._selected["calls"] != top  # cursor moved to the newest
        assert app._selected["calls"] == str(app._calls[0]["id"])
        assert calls.cursor_row == 0  # pinned to the top of the feed


async def test_moving_off_the_top_pauses_following(populated_db, store):
    app = ConsoleApp(store, refresh_seconds=999)
    async with app.run_test() as pilot:
        await pilot.pause()
        calls = app.query_one("#calls", DataTable)
        calls.focus()
        await pilot.pause()
        calls.move_cursor(row=2)  # inspect an older call
        await pilot.pause()
        assert app._follow_calls is False
        inspected = app._selected["calls"]
        _insert_call(populated_db)  # new calls keep arriving...
        app.action_refresh()
        await pilot.pause()
        # ...but the inspected call holds still instead of jumping to the top:
        # same key, and the cursor rides the row down as new calls stack above.
        assert app._selected["calls"] == inspected
        assert calls.cursor_row == 3
        assert app._follow_calls is False  # a refresh must not resume following
        assert "trace #" in app._last_detail


async def test_returning_to_the_top_resumes_following(store):
    app = ConsoleApp(store, refresh_seconds=999)
    async with app.run_test() as pilot:
        await pilot.pause()
        calls = app.query_one("#calls", DataTable)
        calls.focus()
        await pilot.pause()
        calls.move_cursor(row=2)
        await pilot.pause()
        assert app._follow_calls is False
        calls.move_cursor(row=0)  # back to the newest row
        await pilot.pause()
        assert app._follow_calls is True


async def test_a_scrolled_out_selection_resumes_following(populated_db, store):
    app = ConsoleApp(store, refresh_seconds=999)
    async with app.run_test() as pilot:
        await pilot.pause()
        calls = app.query_one("#calls", DataTable)
        calls.focus()
        await pilot.pause()
        calls.move_cursor(row=3)  # pause on the OLDEST call
        await pilot.pause()
        assert app._follow_calls is False
        # Simulate the row leaving the feed window: the tracked key vanishes.
        app._selected["calls"] = "0"  # no trace has id 0
        app.action_refresh()
        await pilot.pause()
        assert app._follow_calls is True  # resumed at the top
        assert app._selected["calls"] == str(app._calls[0]["id"])


async def test_models_table_splits_by_served_model(store):
    app = ConsoleApp(store, refresh_seconds=999)
    async with app.run_test() as pilot:
        await pilot.pause()
        models = app.query_one("#models", DataTable)
        # Fixture: 3 baseline calls served by "opus", 1 candidate by "haiku"
        # -> the served-model split shows both arms of the experiment.
        assert models.row_count == 2
        models.focus()
        await pilot.pause()
        # opus is the top spender ($2.70 vs $0.20) -> highlighted first.
        assert "model: opus" in app._last_detail
        assert "of recorded spend" in app._last_detail


# --- the traffic graphs -------------------------------------------------------


async def test_graphs_render_the_traffic_series(populated_db, store):
    app = ConsoleApp(store, refresh_seconds=999)
    async with app.run_test() as pilot:
        await pilot.pause()
        buckets = _DEFAULT_GRAPH_WINDOW.buckets
        calls_graph = app.query_one("#calls-graph", Sparkline)
        # One zero-filled bucket per time slice of the window (fixture traces
        # sit at ts=0, far outside it). Fine buckets on purpose: more points
        # than columns -> thin per-column bars, not fat multi-column blocks.
        assert len(calls_graph.data) == buckets
        assert buckets >= 120  # stays finer than a typical pane width
        assert sum(calls_graph.data) == 0
        label = str(app.query_one("#calls-graph-label", Label).content)
        assert "calls" in label and "0 total" in label
        # A call lands NOW -> the newest bucket ticks up on the next refresh.
        _insert_call(populated_db, ts=time.time())
        app.action_refresh()
        await pilot.pause()
        assert sum(app.query_one("#calls-graph", Sparkline).data) == 1
        label = str(app.query_one("#calls-graph-label", Label).content)
        assert "1 total" in label
        cost = str(app.query_one("#cost-graph-label", Label).content)
        assert "cost" in cost and "$" in cost
        # Latency and token graphs ride the same series: the inserted call has
        # latency 10ms (helper default) and no enrichment (0 tokens).
        assert sum(app.query_one("#latency-graph", Sparkline).data) == 10.0
        latency = str(app.query_one("#latency-graph-label", Label).content)
        assert "latency" in latency and "avg 10ms" in latency
        tokens = str(app.query_one("#tokens-graph-label", Label).content)
        assert "tokens" in tokens and "0 total" in tokens


def test_graph_axis_labels_start_middle_and_now():
    hour = [{"ts": 1_770_000_000 + i * 20} for i in range(180)]
    axis = graph_axis(hour, 70)
    assert len(axis) == 70  # exactly the width it was given
    assert axis.endswith("now")
    start = time.strftime("%H:%M", time.localtime(hour[0]["ts"]))
    middle = time.strftime("%H:%M", time.localtime(hour[90]["ts"]))
    assert axis.startswith(start)
    assert middle in axis
    # The midpoint label sits in the middle, not wherever the text lands.
    assert abs(axis.index(middle) + len(middle) / 2 - 35) <= 1


def test_graph_axis_switches_to_dates_past_a_day_and_gives_up_when_tiny():
    week = [{"ts": 1_770_000_000 + i * 3600} for i in range(180)]
    axis = graph_axis(week, 70)
    assert time.strftime("%d %b", time.localtime(week[0]["ts"])) in axis
    assert ":" not in axis  # dates, not clock times
    # Too narrow for three labels: keep the ends, drop the middle.
    narrow = graph_axis(week, 24)
    assert len(narrow) == 24 and narrow.endswith("now")
    # No room at all, or nothing to label.
    assert graph_axis(week, 12) == ""
    assert graph_axis([], 70) == ""
    assert graph_axis([{"ts": 1.0}], 70) == ""


def test_graph_label_states_the_peak_and_what_a_bucket_is_worth():
    assert (
        graph_label("calls", "last 30m", "130 total", "12", 10)
        == "calls · last 30m · 130 total · peak 12/10s"
    )
    # A peak means nothing without the bucket width, which changes per window.
    assert graph_label("cost", "all time", "$8", "$0.31", 3600).endswith("/1h")
    assert graph_label("tokens", "last 7d", "4k", "90", 86_400).endswith("/1d")
    assert graph_label("calls", "last 1d", "9", "2", 480).endswith("/8m")


async def test_the_graphs_carry_a_shared_axis_and_peaks(store):
    app = ConsoleApp(store, refresh_seconds=999)
    async with app.run_test(size=(160, 36)) as pilot:
        await pilot.pause()
        await pilot.pause()  # the axis is drawn once the pane has a width
        axis = str(app.query_one("#graph-axis", Static).content)
        assert axis.endswith("now")
        # One axis for the stack, spanning the width of the bars above it.
        assert len(axis) == app.query_one("#graph-axis").content_size.width
        for kind in ("calls", "cost", "latency", "tokens"):
            label = str(app.query_one(f"#{kind}-graph-label", Label).content)
            assert "peak" in label, label


async def test_graph_window_picker_switches_every_graph_to_the_new_span(store):
    app = ConsoleApp(store, refresh_seconds=999)
    async with app.run_test() as pilot:
        await pilot.pause()
        label = app.query_one("#calls-graph-label", Label)
        assert "last 30m" in str(label.content)  # the default window
        assert (
            len(app.query_one("#calls-graph", Sparkline).data)
            == _DEFAULT_GRAPH_WINDOW.buckets
        )
        await pilot.press("w")
        await pilot.pause()
        app.screen.query_one("#graph-window-6h", Button).press()
        await pilot.pause()
        six_hours = next(w for w in _GRAPH_WINDOWS if w.label == "6h")
        assert app._graph_window == six_hours
        # Picking redraws at once — it does not wait for the refresh tick.
        assert "last 6h" in str(label.content)
        assert "last 6h" in str(
            app.query_one("#latency-graph-label", Label).content
        )
        assert (
            len(app.query_one("#cost-graph", Sparkline).data)
            == six_hours.buckets
        )


async def test_table_window_scopes_only_the_traffic_aggregates(
    populated_db, store
):
    app = ConsoleApp(store, refresh_seconds=999)
    async with app.run_test() as pilot:
        await pilot.pause()
        # Fixture traffic sits at ts=0, so all-time (the default) shows it and
        # any trailing window must not.
        assert app.query_one("#usecases", DataTable).row_count >= 1
        assert "all time" in str(
            app.query_one("#label-usecases", Label).content
        )
        lifecycle = app.query_one("#experiments", DataTable).row_count
        await pilot.press("t")
        await pilot.pause()
        app.screen.query_one("#table-window-1h", Button).press()
        await pilot.pause()
        assert app._table_window.seconds == 3600
        assert app.query_one("#usecases", DataTable).row_count == 0
        assert app.query_one("#models", DataTable).row_count == 0
        assert app.query_one("#tasks", DataTable).row_count == 0
        for widget_id in ("label-usecases", "label-models", "label-tasks"):
            assert "last 1h" in str(
                app.query_one(f"#{widget_id}", Label).content
            )
        # Lifecycle lists and the tail-style feed stay unscoped: a running
        # experiment idle for an hour must not vanish from the console.
        assert app.query_one("#experiments", DataTable).row_count == lifecycle
        assert app.query_one("#calls", DataTable).row_count >= 1
        # ...and the graphs keep their own window.
        assert app._graph_window == _DEFAULT_GRAPH_WINDOW
        assert "last 30m" in str(
            app.query_one("#calls-graph-label", Label).content
        )


async def test_table_window_all_restores_the_full_history(store):
    app = ConsoleApp(store, refresh_seconds=999)
    async with app.run_test() as pilot:
        await pilot.pause()
        rows = app.query_one("#usecases", DataTable).row_count
        await pilot.press("t")
        await pilot.pause()
        app.screen.query_one("#table-window-10m", Button).press()
        await pilot.pause()
        assert app.query_one("#usecases", DataTable).row_count == 0
        await pilot.press("t")
        await pilot.pause()
        app.screen.query_one("#table-window-all", Button).press()
        await pilot.pause()
        assert app.query_one("#usecases", DataTable).row_count == rows
        assert app._table_window == _DEFAULT_TABLE_WINDOW


async def test_graph_window_picker_cancels_without_changing_the_window(store):
    app = ConsoleApp(store, refresh_seconds=999)
    async with app.run_test() as pilot:
        await pilot.pause()
        await pilot.press("w")
        await pilot.pause()
        await pilot.press("escape")  # no Cancel button: escape closes it
        await pilot.pause()
        assert app._graph_window == _DEFAULT_GRAPH_WINDOW
        assert "last 30m" in str(
            app.query_one("#calls-graph-label", Label).content
        )


# --- keyboard-first forms ---------------------------------------------------


def test_field_matches_finds_names_by_any_part_not_just_the_prefix():
    """Inline ghost text can only complete a PREFIX, and every name here is
    prefixed by convention, so the match line carries the real search."""
    use_cases = (
        "tag:newspaper_editor",
        "tag:newspaper_quant",
        "tag:dreamer",
    )
    assert field_matches("news", use_cases) == (
        "tag:newspaper_editor · tag:newspaper_quant"
    )
    assert field_matches("DREAM", use_cases) == "tag:dreamer"  # case-blind
    # Nothing to add: empty field, or already an exact name.
    assert field_matches("", use_cases) == ""
    assert field_matches("  tag:dreamer ", use_cases) == ""
    # An unknown name is allowed — it is a completion, not a closed list.
    assert field_matches("brand-new", use_cases) == (
        "no match — will be used as typed"
    )
    many = tuple(f"tag:role_{i}" for i in range(9))
    crowded = field_matches("role", many)
    assert crowded.count("·") == 3 and crowded.endswith("(+5 more)")


@pytest.mark.parametrize(
    "screen",
    [
        OfflineExperimentScreen,
        LiveExperimentScreen,
        ShadowExperimentScreen,
        RouteScreen,
        WorkflowIdentifyScreen,
        WorkflowProposalVerifyScreen,
        WorkflowDiscoveryScopeScreen,
        ConfirmScreen,
        GitConfigScreen,
    ],
)
def test_every_dialog_takes_escape_and_the_arrows(screen):
    """Textual merges BINDINGS only from a class's own __dict__ and only from
    DOMNode subclasses, so a plain mixin is silently ignored — this is the
    guard on KeyboardForm.__init_subclass__ working around that."""
    args = {
        ConfirmScreen: ("message", "confirm"),
        GitConfigScreen: ("message",),
        WorkflowIdentifyScreen: ("family:1",),
    }.get(screen, ())
    keys = screen(*args)._bindings.key_to_bindings
    assert {"escape", "up", "down"} <= set(keys)


async def test_the_offline_form_suggests_recorded_use_cases_and_models(store):
    app = ConsoleApp(store, db_path="x.db", refresh_seconds=999)
    async with app.run_test(size=(150, 40)) as pilot:
        await pilot.pause()
        await pilot.press("o")
        await pilot.pause()
        form = app.screen
        assert isinstance(form, OfflineExperimentScreen)
        # Use-cases come from recorded traffic; models from the price table
        # plus anything actually served.
        assert "tag:editor" in form.use_cases
        assert "claude-haiku-4-5" in form.models
        assert "opus" in form.models  # served, even if unpriced

        candidate = form.query_one("#offline-candidate", Input)
        candidate.focus()
        for char in "haik":
            await pilot.press(char)
        await pilot.pause()
        matches = form.query_one("#offline-candidate-matches", Static)
        assert "haiku" in str(matches.content)
        assert matches.has_class("shown")
        # A model nobody has run is still accepted — that is the usual case
        # for a candidate.
        candidate.value = "brand-new-model"
        await pilot.pause()
        assert "used as typed" in str(
            form.query_one("#offline-candidate-matches", Static).content
        )


async def test_the_arrows_walk_the_form_and_escape_abandons_it(store):
    app = ConsoleApp(store, db_path="x.db", refresh_seconds=999)
    async with app.run_test(size=(150, 40)) as pilot:
        await pilot.pause()
        await pilot.press("o")
        await pilot.pause()
        app.screen.query_one("#offline-use-case", Input).focus()
        await pilot.pause()
        await pilot.press("down")
        await pilot.pause()
        assert app.focused.id == "offline-candidate"
        await pilot.press("up")
        await pilot.pause()
        assert app.focused.id == "offline-use-case"
        await pilot.press("escape")
        await pilot.pause()
        assert not isinstance(app.screen, OfflineExperimentScreen)
        assert app._last_notice == ""  # abandoned, nothing queued


async def test_a_three_field_row_shows_all_three(store):
    """Input defaults to width 100%, so in a Horizontal the first field took
    the row and the other two sat off-screen, unreachable."""
    app = ConsoleApp(store, db_path="x.db", refresh_seconds=999)
    async with app.run_test(size=(150, 40)) as pilot:
        await pilot.pause()
        await pilot.press("o")
        await pilot.pause()
        row = [
            app.screen.query_one(f"#offline-{name}", Input)
            for name in ("limit", "margin", "replicates")
        ]
        assert [field.value for field in row] == ["50", "1.0", "2"]
        xs = [field.region.x for field in row]
        assert xs == sorted(xs) and len(set(xs)) == 3  # side by side
        dialog = app.screen.query_one("#offline-dialog")
        for field in row:
            assert field.region.right <= dialog.region.right


async def test_ctrl_d_pages_the_detail_while_a_list_has_focus(
    populated_db, store
):
    """DataTable owns pageup/pagedown for its cursor, so those keys cannot
    scroll the detail while a list is focused."""
    writer = SqliteTraceStore(populated_db)
    writer._insert(
        _trace(
            "b0",
            BASELINE,
            use_case="tag:editor",
            served_model="opus",
            cost=0.9,
            experiment_id="exp:e",
            body=b'{"system": "' + b"long. " * 400 + b'"}',
        )
    )
    writer.close()
    app = ConsoleApp(store, refresh_seconds=999)
    async with app.run_test(size=(150, 24)) as pilot:
        await pilot.pause()
        calls = app.query_one("#calls", DataTable)
        calls.focus()
        await pilot.pause()
        pane = app.query_one("#detail-pane")
        assert pane.scroll_offset.y == 0
        await pilot.press("ctrl+d")
        await pilot.pause()
        assert pane.scroll_offset.y > 0  # scrolled without expanding first
        assert calls.has_focus  # and without stealing focus from the list
        await pilot.press("ctrl+u")
        await pilot.pause()
        assert pane.scroll_offset.y == 0


# --- expanding the detail pane ---------------------------------------------


async def test_enter_gives_the_detail_the_column_and_escape_gives_it_back(
    store,
):
    app = ConsoleApp(store, refresh_seconds=999)
    async with app.run_test(size=(150, 30)) as pilot:
        await pilot.pause()
        usecases = app.query_one("#usecases", DataTable)
        usecases.focus()
        await pilot.pause()
        showing = app._last_detail
        assert app.query_one("#graphs").display

        await pilot.press("enter")
        await pilot.pause()
        assert app._detail_expanded
        # Everything above the detail yields the column; the sidebar does not
        # move — this is reading one record, not browsing a list.
        for widget_id in (
            "#graphs",
            "#budget-status",
            "#routing-status",
            "#shadow-status",
        ):
            assert not app.query_one(widget_id).display
        assert app.query_one("#usecases", DataTable).display
        # Focus lands on the scroller, so the arrows scroll the record.
        assert app.focused.id == "detail-pane"
        assert app._last_detail == showing  # still the row you opened

        await pilot.press("escape")
        await pilot.pause()
        assert not app._detail_expanded
        assert app.query_one("#graphs").display
        assert app.query_one("#budget-status").display
        assert app.focused.id == "usecases"  # back where you were


async def test_the_detail_scrolls_from_the_keyboard(populated_db, store):
    """The pane was mouse-only: nothing focusable took the scroll keys."""
    writer = SqliteTraceStore(populated_db)
    writer._insert(
        _trace(
            "b0",
            BASELINE,
            use_case="tag:editor",
            served_model="opus",
            cost=0.9,
            experiment_id="exp:e",
            body=b'{"system": "' + b"long. " * 400 + b'"}',
        )
    )
    writer.close()
    app = ConsoleApp(store, refresh_seconds=999)
    async with app.run_test(size=(150, 24)) as pilot:
        await pilot.pause()
        app.query_one("#calls", DataTable).focus()
        await pilot.pause()
        await pilot.press("enter")
        await pilot.pause()
        pane = app.query_one("#detail-pane")
        assert pane.scrollable_content_region.height < pane.virtual_size.height
        assert pane.scroll_offset.y == 0
        await pilot.press("pagedown")
        await pilot.pause()
        assert pane.scroll_offset.y > 0
        moved = pane.scroll_offset.y
        await pilot.press("end")
        await pilot.pause()
        assert pane.scroll_offset.y > moved
        await pilot.press("home")
        await pilot.pause()
        assert pane.scroll_offset.y == 0


async def test_escape_backs_out_of_a_maximized_table_too(store):
    app = ConsoleApp(store, refresh_seconds=999)
    async with app.run_test() as pilot:
        await pilot.pause()
        app.query_one("#models", DataTable).focus()
        await pilot.pause()
        await pilot.press("m")
        await pilot.pause()
        assert app._maximized == "models"
        await pilot.press("escape")
        await pilot.pause()
        assert app._maximized is None
        assert app.query_one("#graphs").display


# --- the header as a liveness indicator ------------------------------------


async def test_the_header_dates_the_last_read_and_lights_the_icon(store):
    """A frozen monitor and a healthy one used to look identical: the header
    said "refresh 3s" either way."""
    app = ConsoleApp(store, db_path="/tmp/router.db", refresh_seconds=3)
    async with app.run_test() as pilot:
        await pilot.pause()
        assert "updated " in app.sub_title
        assert "/tmp/router.db" in app.sub_title
        assert "refresh 3s" in app.sub_title
        icon = app.query_one("HeaderIcon")
        assert icon.icon == "●"  # filled: it means something now
        assert not app.query_one(Header).has_class("stale")
        stamp = app.sub_title.split("updated ")[1].split(" ")[0]
        assert time.strptime(stamp, "%H:%M:%S")  # a real clock time


async def test_a_failed_read_marks_the_header_stale(populated_db):
    ro = SqliteTraceStore(populated_db, read_only=True)
    app = ConsoleApp(ro, db_path=populated_db, refresh_seconds=999)
    async with app.run_test() as pilot:
        await pilot.pause()
        fresh = app.sub_title
        healthy_colour = app.query_one("HeaderIcon").styles.color
        ro.close()  # the next read cannot work
        app.action_refresh()
        await pilot.pause()
        assert "STALE since" in app.sub_title
        # The timestamp stays put: it dates what is still on screen.
        assert fresh.split("updated ")[1].split(" ")[0] in app.sub_title
        assert app.query_one(Header).has_class("stale")
        assert app.query_one("HeaderIcon").styles.color != healthy_colour
        assert "could not read" in app._last_detail


# --- keys and the commands panel -------------------------------------------


def test_every_letter_command_answers_to_both_cases():
    """No command should need a deliberate shift — every letter key carries a
    hidden shifted twin bound to the same action."""
    by_key = {b.key: b for b in ConsoleApp.BINDINGS}
    letters = [k for k in by_key if len(k) == 1 and k.islower()]
    assert letters, "expected letter commands"
    for key in letters:
        assert key.upper() in by_key, f"{key} has no shifted twin"
        assert by_key[key.upper()].action == by_key[key].action
        assert not by_key[key.upper()].show  # the footer lists it once
    # `?` is shifted on most layouts, so f1 opens the panel too.
    assert by_key["f1"].action == by_key["question_mark"].action
    # Shifted twins cannot collide: one action per key across the whole map.
    assert len(by_key) == len(ConsoleApp.BINDINGS)


def test_the_footer_stays_short_and_the_panel_lists_everything():
    shown = [b for b in ConsoleApp.BINDINGS if b.show]
    assert len(shown) <= 6, [b.key for b in shown]  # a legible footer
    text = command_panel_text()
    for binding in ConsoleApp.BINDINGS:
        assert binding.description in text
    for group in ("Monitor", "View", "Experiments", "Workflows"):
        assert group in text
    assert "[" in text and "]" in text and "?" in text  # keys print readably


async def test_the_commands_panel_toggles(store):
    app = ConsoleApp(store, refresh_seconds=999)
    async with app.run_test() as pilot:
        await pilot.pause()
        panel = app.query_one("#commands")
        assert not panel.has_class("open")
        await pilot.press("question_mark")
        await pilot.pause()
        assert panel.has_class("open")
        body = app.query_one("#commands-body", Static)
        assert "Graph window" in str(body.content)
        await pilot.press("question_mark")
        await pilot.pause()
        assert not panel.has_class("open")


async def test_a_shifted_key_runs_the_same_command(store):
    app = ConsoleApp(store, refresh_seconds=999)
    async with app.run_test() as pilot:
        await pilot.pause()
        await pilot.press("W")  # the shifted twin of the graph-window key
        await pilot.pause()
        assert isinstance(app.screen, WindowPickerScreen)
        await pilot.press("escape")
        await pilot.pause()
        await pilot.press("w")  # ...and the unshifted key opens the same thing
        await pilot.pause()
        assert isinstance(app.screen, WindowPickerScreen)


async def test_the_picker_moves_on_arrows_and_closes_on_escape(store):
    app = ConsoleApp(store, refresh_seconds=999)
    async with app.run_test() as pilot:
        await pilot.pause()
        await pilot.press("w")
        await pilot.pause()
        # It opens on the current window, so arrows move from where you are.
        assert app.focused.id == "graph-window-30m"
        await pilot.press("right")
        await pilot.pause()
        assert app.focused.id == "graph-window-1h"
        await pilot.press("left")
        await pilot.press("left")
        await pilot.pause()
        assert app.focused.id == "graph-window-10m"
        await pilot.press("enter")  # the focused choice is the one picked
        await pilot.pause()
        assert app._graph_window.label == "10m"

        await pilot.press("w")
        await pilot.pause()
        await pilot.press("escape")
        await pilot.pause()
        assert app._graph_window.label == "10m"  # escape changes nothing
        assert not isinstance(app.screen, WindowPickerScreen)


async def test_all_is_the_last_choice_in_both_pickers(store):
    app = ConsoleApp(store, refresh_seconds=999)
    async with app.run_test() as pilot:
        await pilot.pause()
        await pilot.press("w")
        await pilot.pause()
        labels = [
            str(b.label) for b in app.screen.query("#window-choices Button")
        ]
        assert labels[-1] == "all"
        app.screen.query_one("#graph-window-all", Button).press()
        await pilot.pause()
        assert app._graph_window.is_all
        assert "all time" in str(
            app.query_one("#calls-graph-label", Label).content
        )
        assert len(app.query_one("#calls-graph", Sparkline).data) == 180


# --- sidebar compaction ---------------------------------------------------


async def test_empty_panes_collapse_to_one_line(store):
    """On a small terminal ten equal panes crowd out the two with data."""
    app = ConsoleApp(store, refresh_seconds=999)
    async with app.run_test(size=(120, 30)) as pilot:
        await pilot.pause()
        # The fixture has an experiment plus use-cases, models, tasks and
        # calls; jobs, shadows, workflows and discovered are bare.
        for table_id in ("experiments", "usecases", "models", "tasks", "calls"):
            assert app.query_one(f"#{table_id}", DataTable).display
            assert app.query_one(f"#label-{table_id}", Label).display
        for table_id in ("jobs", "shadows", "workflows"):
            assert not app.query_one(f"#{table_id}", DataTable).display
            assert not app.query_one(f"#label-{table_id}", Label).display
        note = str(app.query_one("#empty-note", Static).content)
        assert app.query_one("#empty-note", Static).display
        for name in ("jobs", "shadows", "workflows"):
            assert name in note
        assert "calls" not in note  # it has rows, so it is not collapsed
        # The feed gets the space the empty panes gave up.
        assert app.query_one("#calls", DataTable).size.height > 1


async def test_moving_the_cursor_in_an_empty_pane_does_not_crash(populated_db):
    """A DataTable with no rows posts RowHighlighted(cursor_row=-1,
    row_key=None) when the cursor moves in it — reported from the box as an
    AttributeError on `event.row_key.value`."""
    writer = SqliteTraceStore(populated_db)
    writer.create_job(Job("replay_eval", {}, job_id="job:doomed"))
    writer.close()
    ro = SqliteTraceStore(populated_db, read_only=True)
    try:
        app = ConsoleApp(ro, db_path=populated_db, refresh_seconds=999)
        async with app.run_test() as pilot:
            await pilot.pause()
            jobs = app.query_one("#jobs", DataTable)
            assert jobs.row_count == 1
            jobs.focus()
            await pilot.pause()
            # The job goes away under the cursor; the pane stays visible
            # because it is focused, and empty.
            writer = SqliteTraceStore(populated_db)
            writer._conn.execute("DELETE FROM jobs")
            writer._conn.commit()
            writer.close()
            app.action_refresh()
            await pilot.pause()
            assert jobs.row_count == 0 and jobs.display
            for key in ("up", "down", "home", "end"):
                await pilot.press(key)
                await pilot.pause()
                assert app._exception is None, app._exception
            assert app._selected["jobs"] is None
    finally:
        ro.close()


async def test_a_pane_reappears_when_it_gets_a_row(populated_db, store):
    app = ConsoleApp(store, refresh_seconds=999)
    async with app.run_test() as pilot:
        await pilot.pause()
        jobs = app.query_one("#jobs", DataTable)
        assert not jobs.display
        writer = SqliteTraceStore(populated_db)
        writer.create_job(Job("replay_eval", {}, job_id="job:new"))
        writer.close()
        app.action_refresh()
        await pilot.pause()
        assert jobs.display
        assert "jobs" not in str(app.query_one("#empty-note", Static).content)


async def test_the_focused_pane_stays_visible_even_when_empty(store):
    """The cursor must never sit on something that isn't drawn."""
    app = ConsoleApp(store, refresh_seconds=999)
    async with app.run_test() as pilot:
        await pilot.pause()
        shadows = app.query_one("#shadows", DataTable)
        assert not shadows.display
        shadows.display = True  # only a visible widget can take focus
        shadows.focus()
        await pilot.pause()
        app.action_refresh()
        await pilot.pause()
        assert shadows.display
        assert "shadows" not in str(
            app.query_one("#empty-note", Static).content
        )


async def test_maximize_owns_visibility_while_it_is_on(store):
    app = ConsoleApp(store, refresh_seconds=999)
    async with app.run_test() as pilot:
        await pilot.pause()
        app.query_one("#models", DataTable).focus()
        await pilot.pause()
        await pilot.press("m")
        await pilot.pause()
        assert app.query_one("#models", DataTable).display
        assert not app.query_one("#usecases", DataTable).display  # has rows
        assert not app.query_one("#empty-note", Static).display
        # A refresh under maximize must not un-hide the other panes.
        app.action_refresh()
        await pilot.pause()
        assert not app.query_one("#usecases", DataTable).display
        await pilot.press("m")
        await pilot.pause()
        assert app.query_one("#usecases", DataTable).display  # compaction back
        assert not app.query_one("#jobs", DataTable).display
        assert app.query_one("#empty-note", Static).display


# --- pagination ---------------------------------------------------------------


def test_page_heading_shows_the_range_and_where_paging_can_go():
    # One page that holds everything says nothing — a range would be noise.
    assert page_heading("Calls (live)", 0, 12, False) == "Calls (live)"
    assert page_heading("Calls (live)", 0, 50, True) == "Calls (live) · 1–50 ›"
    assert (
        page_heading("Calls (live)", 1, 50, True) == "Calls (live) · ‹ 51–100 ›"
    )
    assert (
        page_heading("Calls (live)", 2, 30, False) == "Calls (live) · ‹ 101–130"
    )
    assert (
        page_heading("Jobs", 3, 0, False) == "Jobs · ‹ empty page 4"
    )  # rows deleted under a held page


@pytest.fixture
def many_calls_db(tmp_path):
    """130 calls — more than two full pages of the feed."""
    db = str(tmp_path / "paged.db")
    writer = SqliteTraceStore(db)
    for i in range(130):
        writer._insert(
            _trace(
                f"task-{i}",
                None,
                use_case="tag:editor",
                served_model="opus",
                cost=0.01,
                experiment_id=None,
                ts=float(i),
            )
        )
    writer.close()
    return db


async def test_call_feed_pages_and_stops_at_both_ends(many_calls_db):
    ro = SqliteTraceStore(many_calls_db, read_only=True)
    try:
        app = ConsoleApp(ro, refresh_seconds=999)
        async with app.run_test() as pilot:
            await pilot.pause()
            calls = app.query_one("#calls", DataTable)
            label = app.query_one("#label-calls", Label)
            calls.focus()
            await pilot.pause()
            assert calls.row_count == _PAGE_SIZE
            assert str(label.content) == "Calls (live) · 1–50 ›"
            first_page_top = app._selected["calls"]

            await pilot.press("left_square_bracket")  # already at the newest
            await pilot.pause()
            assert app._page["calls"] == 0
            assert app._selected["calls"] == first_page_top

            await pilot.press("right_square_bracket")
            await pilot.pause()
            assert app._page["calls"] == 1
            assert str(label.content) == "Calls (live) · ‹ 51–100 ›"
            assert app._selected["calls"] != first_page_top

            await pilot.press("right_square_bracket")
            await pilot.pause()
            assert calls.row_count == 30  # 130 calls = two full pages + 30
            assert str(label.content) == "Calls (live) · ‹ 101–130"

            await pilot.press("right_square_bracket")  # no page 4 to go to
            await pilot.pause()
            assert app._page["calls"] == 2
            assert calls.row_count == 30
    finally:
        ro.close()


async def test_paging_the_feed_pauses_the_tail_until_page_one(many_calls_db):
    ro = SqliteTraceStore(many_calls_db, read_only=True)
    try:
        app = ConsoleApp(ro, refresh_seconds=999)
        async with app.run_test() as pilot:
            await pilot.pause()
            app.query_one("#calls", DataTable).focus()
            await pilot.pause()
            assert app._follow_calls  # page 1 tails the newest call
            await pilot.press("right_square_bracket")
            await pilot.pause()
            assert not app._follow_calls  # reading an older page, not tailing
            # A new call arrives: the held page must not jump to the tail.
            held = app._selected["calls"]
            writer = SqliteTraceStore(many_calls_db)
            writer._insert(
                _trace(
                    "task-new",
                    None,
                    use_case="tag:editor",
                    served_model="opus",
                    cost=0.01,
                    experiment_id=None,
                    ts=999.0,
                )
            )
            writer.close()
            app.action_refresh()
            await pilot.pause()
            assert not app._follow_calls
            assert app._selected["calls"] == held
            await pilot.press("left_square_bracket")
            await pilot.pause()
            assert app._follow_calls  # back on page 1, tailing again
    finally:
        ro.close()


async def test_a_paged_out_discovery_job_still_fills_the_discovered_table(
    tmp_path,
):
    """The discovered families come from the newest succeeded discovery job,
    found by kind — not by scanning the jobs page, which it can fall off."""
    db_path = str(tmp_path / "paged-discovery.db")
    writer = SqliteTraceStore(db_path)
    try:
        for index, task_id in enumerate(("legacy-a", "legacy-b")):
            for step, (use_case, model) in enumerate(
                (("tag:researcher", "model-a"), ("tag:writer", "model-b"))
            ):
                writer._insert(
                    _trace(
                        task_id,
                        None,
                        use_case=use_case,
                        served_model=model,
                        cost=0.01,
                        experiment_id=None,
                        ts=float(index * 2 + step),
                    )
                )
    finally:
        writer.close()

    ro = SqliteTraceStore(db_path, read_only=True)
    try:
        app = ConsoleApp(ro, db_path=db_path, refresh_seconds=999)
        async with app.run_test() as pilot:
            await pilot.pause()
            await pilot.press("u")
            await pilot.pause()
            worker_store = SqliteTraceStore(db_path)
            try:
                Worker(
                    worker_store,
                    {
                        discovery_job.KIND: discovery_job.run_workflow_discovery_job
                    },
                    worker_id="worker:paged-discovery",
                ).run_once()
                # Bury it under more than a full page of newer jobs.
                for i in range(_PAGE_SIZE + 5):
                    worker_store.create_job(
                        Job("replay_eval", {}, job_id=f"job:filler-{i}")
                    )
            finally:
                worker_store.close()
            await pilot.press("r")
            await pilot.pause()
            jobs = app.query_one("#jobs", DataTable)
            assert jobs.row_count == _PAGE_SIZE
            assert app._has_more["jobs"]
            assert app.query_one("#discovered", DataTable).row_count == 1
    finally:
        ro.close()


async def test_paging_is_refused_on_lists_that_do_not_page(store):
    app = ConsoleApp(store, refresh_seconds=999)
    async with app.run_test() as pilot:
        await pilot.pause()
        app.query_one("#usecases", DataTable).focus()
        await pilot.pause()
        await pilot.press("right_square_bracket")
        await pilot.pause()
        assert "Focus jobs, workflows or calls to page" in app._last_notice
        assert app._page == {"jobs": 0, "workflows": 0, "calls": 0}


async def test_markup_hostile_content_does_not_crash(populated_db, store):
    """Bodies, task ids and tags are untrusted input: "[/broken" must render
    literally everywhere, not parse as Textual markup (which raises)."""
    hostile_body = (
        b'{"system": "call with finish_type=\'failure\' [/broken [red]x"}'
    )
    writer = SqliteTraceStore(populated_db)
    writer._insert(
        _trace(
            "[/x]evil-task",  # lands in the tasks table cells
            BASELINE,
            use_case="tag:[/y]evil",  # lands in use-case + calls cells
            served_model="opus",
            cost=0.1,
            experiment_id=None,
            body=hostile_body,  # lands in the detail pane
        )
    )
    writer.close()
    app = ConsoleApp(store, refresh_seconds=999)
    async with app.run_test() as pilot:
        await pilot.pause()  # tables fill without a MarkupError
        assert app.query_one("#tasks", DataTable).row_count >= 1
        app.query_one("#calls", DataTable).focus()
        await pilot.pause()  # newest call (the hostile one) renders in full
        assert "trace #" in app._last_detail
        assert "[/broken" in app._last_detail  # shown literally


# --- maximize ----------------------------------------------------------------


async def test_m_maximizes_the_focused_table_and_keeps_the_detail(store):
    app = ConsoleApp(store, refresh_seconds=999)
    async with app.run_test() as pilot:
        await pilot.pause()
        calls = app.query_one("#calls", DataTable)
        calls.focus()
        await pilot.pause()
        await pilot.press("m")
        assert app._maximized == "calls"
        # The other tables (and their headings) are hidden...
        assert not app.query_one("#experiments", DataTable).display
        assert not app.query_one("#tasks", DataTable).display
        assert not app.query_one("#label-usecases").display
        # ...the maximized table and the DETAIL pane both stay visible, and
        # the graphs yield their rows to the detail.
        assert calls.display
        assert app.query_one("#detail").display
        assert not app.query_one("#graphs").display
        await pilot.press("m")
        assert app._maximized is None
        assert app.query_one("#experiments", DataTable).display  # restored
        assert app.query_one("#graphs").display


async def test_selection_drives_the_detail_while_maximized(store):
    # The point of maximizing WITH the detail: arrowing through the big list
    # must keep updating the right-hand pane.
    app = ConsoleApp(store, refresh_seconds=999)
    async with app.run_test() as pilot:
        await pilot.pause()
        calls = app.query_one("#calls", DataTable)
        calls.focus()
        await pilot.pause()
        await pilot.press("m")
        calls.move_cursor(row=2)
        await pilot.pause()
        assert f"trace #{app._selected['calls']}" in app._last_detail


async def test_the_feed_keeps_refreshing_while_maximized(populated_db, store):
    app = ConsoleApp(store, refresh_seconds=999)
    async with app.run_test() as pilot:
        await pilot.pause()
        calls = app.query_one("#calls", DataTable)
        calls.focus()
        await pilot.pause()
        await pilot.press("m")
        assert app._maximized == "calls"
        _insert_call(populated_db)
        app.action_refresh()
        await pilot.pause()
        assert calls.row_count == 5  # the new call landed in the big pane
        assert app._selected["calls"] == str(
            app._calls[0]["id"]
        )  # still follows
        assert app._maximized == "calls"  # refresh didn't un-maximize


async def test_empty_db_shows_the_placeholder(tmp_path):
    db = str(tmp_path / "empty.db")
    SqliteTraceStore(db).close()  # schema, no rows
    ro = SqliteTraceStore(db, read_only=True)
    try:
        app = ConsoleApp(ro, refresh_seconds=999)
        async with app.run_test() as pilot:
            await pilot.pause()
            assert app._last_detail == _EMPTY
    finally:
        ro.close()


async def test_console_queues_a_reviewed_offline_experiment(populated_db):
    ro = SqliteTraceStore(populated_db, read_only=True)
    try:
        app = ConsoleApp(ro, db_path=populated_db, refresh_seconds=999)
        async with app.run_test() as pilot:
            await pilot.pause()
            await pilot.press("o")
            await pilot.pause()
            screen = app.screen
            screen.query_one("#offline-candidate", Input).value = "local-model"
            screen.query_one("#offline-baseline", Input).value = "opus"
            screen.query_one("#offline-limit", Input).value = "3"
            screen.query_one("#offline-review", Button).press()
            await pilot.pause()
            assert "total upstream calls" in str(
                app.screen.query_one("Static").content
            )
            app.screen.query_one("#confirm-yes", Button).press()
            await pilot.pause()
            assert "Queued job:" in app._last_notice
            assert app.query_one("#jobs", DataTable).row_count == 1
    finally:
        ro.close()
    writer = SqliteTraceStore(populated_db)
    try:
        job = writer.jobs()[0]
        assert job.config["candidate_model"] == "local-model"
        assert len(job.config["trace_ids"]) == 3
    finally:
        writer.close()


async def test_console_cancels_the_selected_job(populated_db):
    writer = SqliteTraceStore(populated_db)
    writer.create_job(Job("replay_eval", {}, job_id="job:cancel"))
    writer.close()
    ro = SqliteTraceStore(populated_db, read_only=True)
    try:
        app = ConsoleApp(ro, db_path=populated_db, refresh_seconds=999)
        async with app.run_test() as pilot:
            await pilot.pause()
            app.query_one("#jobs", DataTable).focus()
            await pilot.pause()
            await pilot.press("x")
            await pilot.pause()
            app.screen.query_one("#confirm-yes", Button).press()
            await pilot.pause()
            assert "Cancellation requested" in app._last_notice
    finally:
        ro.close()
    reader = SqliteTraceStore(populated_db)
    try:
        assert reader.job("job:cancel").status == "cancelled"
    finally:
        reader.close()


async def test_console_starts_a_reviewed_live_experiment(populated_db):
    writer = SqliteTraceStore(populated_db)
    writer.stop_experiment("exp:e")
    writer.close()
    ro = SqliteTraceStore(populated_db, read_only=True)
    try:
        app = ConsoleApp(ro, db_path=populated_db, refresh_seconds=999)
        async with app.run_test() as pilot:
            await pilot.pause()
            await pilot.press("e")
            await pilot.pause()
            screen = app.screen
            screen.query_one("#live-candidate", Input).value = "local-model"
            screen.query_one("#live-split", Input).value = "25"
            screen.query_one("#live-review", Button).press()
            await pilot.pause()
            review = str(app.screen.query_one("Static").content)
            assert "25% candidate / 75% baseline" in review
            app.screen.query_one("#confirm-yes", Button).press()
            await pilot.pause()
            assert "Started exp:" in app._last_notice
    finally:
        ro.close()
    reader = SqliteTraceStore(populated_db)
    try:
        running = reader.running_experiments()["tag:editor"]
        assert running.candidate_model == "local-model"
        assert running.split_pct == 25
    finally:
        reader.close()


async def test_console_stops_selected_live_experiment(populated_db):
    ro = SqliteTraceStore(populated_db, read_only=True)
    try:
        app = ConsoleApp(ro, db_path=populated_db, refresh_seconds=999)
        async with app.run_test() as pilot:
            await pilot.pause()
            app.query_one("#experiments", DataTable).focus()
            await pilot.pause()
            await pilot.press("s")
            await pilot.pause()
            app.screen.query_one("#confirm-yes", Button).press()
            await pilot.pause()
            assert "Stopped exp:e" in app._last_notice
    finally:
        ro.close()
    reader = SqliteTraceStore(populated_db)
    try:
        assert not reader.experiment("exp:e").is_running
    finally:
        reader.close()


async def test_console_starts_and_stops_a_reviewed_shadow(populated_db):
    writer = SqliteTraceStore(populated_db)
    writer.stop_experiment("exp:e")
    writer.close()
    ro = SqliteTraceStore(populated_db, read_only=True)
    try:
        app = ConsoleApp(ro, db_path=populated_db, refresh_seconds=999)
        async with app.run_test() as pilot:
            await pilot.pause()
            await pilot.press("h")
            await pilot.pause()
            screen = app.screen
            screen.query_one("#shadow-candidate", Input).value = "local-model"
            screen.query_one("#shadow-sample", Input).value = "25"
            screen.query_one("#shadow-review", Button).press()
            await pilot.pause()
            review = str(app.screen.query_one("Static").content)
            assert "never served to users" in review
            app.screen.query_one("#confirm-yes", Button).press()
            await pilot.pause()
            assert "Started shadow:" in app._last_notice
            assert app.query_one("#shadows", DataTable).row_count == 1

            app.query_one("#shadows", DataTable).focus()
            await pilot.pause()
            await pilot.press("z")
            await pilot.pause()
            app.screen.query_one("#confirm-yes", Button).press()
            await pilot.pause()
            assert "Stopped shadow:" in app._last_notice
    finally:
        ro.close()
    reader = SqliteTraceStore(populated_db)
    try:
        shadow = reader.shadow_experiments()[0]
        assert shadow.candidate_model == "local-model"
        assert shadow.sample_pct == 25
        assert not shadow.is_running
    finally:
        reader.close()


def test_shadow_detail_compares_latest_pair_and_marks_attrition(populated_db):
    writer = SqliteTraceStore(populated_db)
    writer.stop_experiment("exp:e")
    shadow = ShadowExperiment(
        "tag:editor", "local-model", 100, shadow_id="shadow:inspect"
    )
    writer.create_shadow_experiment(shadow)
    actual = _trace(
        "paired",
        None,
        use_case="tag:editor",
        served_model="opus",
        cost=0.1,
        experiment_id=None,
        body=b'{"messages":[{"content":"actual input"}]}',
    )
    actual.shadow_experiment_id = shadow.shadow_id
    actual.shadow_pair_id = "pair:complete"
    actual.shadow_role = "actual"
    writer._insert(actual)
    candidate = _trace(
        "paired",
        None,
        use_case="tag:editor",
        served_model="local-model",
        cost=0.01,
        experiment_id=None,
        ts=1.0,
        body=b'{"messages":[{"content":"candidate input"}]}',
    )
    candidate.shadow_experiment_id = shadow.shadow_id
    candidate.shadow_pair_id = "pair:complete"
    candidate.shadow_role = "candidate"
    writer._insert(candidate)
    actual.shadow_pair_id = "pair:attrition"
    actual.ts = 2.0
    writer._insert(actual)
    writer.close()

    reader = SqliteTraceStore(populated_db, read_only=True)
    try:
        pairs = reader.shadow_pairs(shadow.shadow_id)
        assert [pair["pair_id"] for pair in pairs] == [
            "pair:attrition",
            "pair:complete",
        ]
        assert pairs[1]["actual_id"] is not None
        assert pairs[1]["candidate_id"] is not None
        text = shadow_detail(reader, shadow)
        assert "latest pair: pair:attrition" in text
        assert "ACTUAL (served to user)" in text
        assert "CANDIDATE (never served)" in text
        assert "missing trace (attrition)" in text
    finally:
        reader.close()


async def test_console_adopts_selected_experiment_candidate(populated_db):
    ro = SqliteTraceStore(populated_db, read_only=True)
    try:
        app = ConsoleApp(ro, db_path=populated_db, refresh_seconds=999)
        async with app.run_test() as pilot:
            await pilot.pause()
            app.query_one("#experiments", DataTable).focus()
            await pilot.pause()
            await pilot.press("a")
            await pilot.pause()
            assert "100%" in str(app.screen.query_one("Static").content)
            app.screen.query_one("#confirm-yes", Button).press()
            await pilot.pause()
            assert "Routing tag:editor -> claude-haiku-4-5" in app._last_notice
    finally:
        ro.close()
    reader = SqliteTraceStore(populated_db)
    try:
        route = reader.routes()[0]
        assert route.model == "claude-haiku-4-5"
        assert route.note == "adopted from exp:e"
        assert not reader.experiment("exp:e").is_running
    finally:
        reader.close()


async def test_console_sets_and_clears_a_persistent_route(populated_db):
    ro = SqliteTraceStore(populated_db, read_only=True)
    try:
        app = ConsoleApp(ro, db_path=populated_db, refresh_seconds=999)
        async with app.run_test() as pilot:
            await pilot.pause()
            app.query_one("#usecases", DataTable).focus()
            await pilot.pause()
            await pilot.press("p")
            await pilot.pause()
            app.screen.query_one("#route-model", Input).value = "local-model"
            app.screen.query_one("#route-note", Input).value = "manual test"
            app.screen.query_one("#route-review", Button).press()
            await pilot.pause()
            review = str(app.screen.query_one("Static").content)
            assert "WARNING: exp:e is running" in review
            app.screen.query_one("#confirm-yes", Button).press()
            await pilot.pause()
            assert "Routing tag:editor -> local-model" in app._last_notice

            await pilot.press("c")
            await pilot.pause()
            app.screen.query_one("#confirm-yes", Button).press()
            await pilot.pause()
            assert "Cleared route for tag:editor" in app._last_notice
    finally:
        ro.close()
    reader = SqliteTraceStore(populated_db)
    try:
        assert reader.routes() == []
    finally:
        reader.close()


def _git(repo, *args):
    subprocess.run(
        ["git", "-C", str(repo), *args],
        check=True,
        capture_output=True,
        text=True,
    )


def _committed_routing_config(tmp_path):
    repo = tmp_path / "routing-repo"
    repo.mkdir()
    _git(repo, "init", "-q")
    _git(repo, "config", "user.email", "test@example.invalid")
    _git(repo, "config", "user.name", "Test")
    path = repo / "routing.yaml"
    path.write_text(
        "version: 1\nroutes:\n  tag:editor:\n    model: local-model\n"
        "    previous_model: opus\n",
        encoding="utf-8",
    )
    _git(repo, "add", "routing.yaml")
    _git(repo, "commit", "-qm", "routing")
    return repo, path


async def test_console_previews_and_activates_git_routing_config(
    tmp_path, populated_db
):
    repo, path = _committed_routing_config(tmp_path)
    ro = SqliteTraceStore(populated_db, read_only=True)
    try:
        app = ConsoleApp(
            ro,
            db_path=populated_db,
            refresh_seconds=999,
            routing_config_path=str(path),
            routing_repo=str(repo),
        )
        async with app.run_test() as pilot:
            await pilot.pause()
            assert "Git routing: none activated" in str(
                app.query_one("#routing-status").content
            )
            await pilot.press("g")
            await pilot.pause()
            preview = str(
                app.screen.query_one("#git-diff-body Static", Static).content
            )
            assert "+++ desired" in preview
            assert "+    model: local-model" in preview
            app.screen.query_one("#git-yes", Button).press()
            await pilot.pause()
            assert "Activated routing.yaml" in app._last_notice
            status = str(app.query_one("#routing-status").content)
            assert "Git routing:" in status and "none activated" not in status
    finally:
        ro.close()
    reader = SqliteTraceStore(populated_db)
    try:
        assert reader.routes()[0].model == "local-model"
        assert not reader.experiment("exp:e").is_running
        assert reader.control_revision().source_path == "routing.yaml"
    finally:
        reader.close()


async def test_console_refuses_config_changed_after_preview(
    tmp_path, populated_db
):
    repo, path = _committed_routing_config(tmp_path)
    ro = SqliteTraceStore(populated_db, read_only=True)
    try:
        app = ConsoleApp(
            ro,
            db_path=populated_db,
            refresh_seconds=999,
            routing_config_path=str(path),
            routing_repo=str(repo),
        )
        async with app.run_test() as pilot:
            await pilot.pause()
            await pilot.press("g")
            await pilot.pause()
            path.write_text(path.read_text() + "# changed\n", encoding="utf-8")
            app.screen.query_one("#git-yes", Button).press()
            await pilot.pause()
            assert "could not activate" in app._last_notice
            assert "uncommitted changes" in app._last_notice
    finally:
        ro.close()
    reader = SqliteTraceStore(populated_db)
    try:
        assert reader.control_revision() is None
    finally:
        reader.close()


async def test_console_discovers_identifies_and_verifies_workflow_family(
    tmp_path,
):
    db_path = tmp_path / "workflow-discovery.db"
    store = SqliteTraceStore(db_path)
    try:
        for index, task_id in enumerate(("legacy-a", "legacy-b")):
            store._insert(
                _trace(
                    task_id,
                    None,
                    use_case="tag:researcher",
                    served_model="model-a",
                    cost=0.01,
                    experiment_id=None,
                    ts=float(index * 2),
                )
            )
            store._insert(
                _trace(
                    task_id,
                    None,
                    use_case="tag:writer",
                    served_model="model-b",
                    cost=0.02,
                    experiment_id=None,
                    ts=float(index * 2 + 1),
                )
            )
    finally:
        store.close()

    proposal_path = tmp_path / "workflow-identification.json"
    ro = SqliteTraceStore(db_path, read_only=True)
    try:
        app = ConsoleApp(ro, db_path=db_path, refresh_seconds=999)
        async with app.run_test() as pilot:
            await pilot.pause()
            await pilot.press("u")
            await pilot.pause()
            job_reader = SqliteTraceStore(db_path)
            try:
                queued = job_reader.jobs()[0]
            finally:
                job_reader.close()
            assert queued.kind == discovery_job.KIND
            assert "frozen trace(s)" in app._last_notice
            worker_store = SqliteTraceStore(db_path)
            try:
                Worker(
                    worker_store,
                    {
                        discovery_job.KIND: discovery_job.run_workflow_discovery_job
                    },
                    worker_id="worker:console-discovery",
                ).run_once()
            finally:
                worker_store.close()
            await pilot.press("r")
            await pilot.pause()
            discovered = app.query_one("#discovered", DataTable)
            assert discovered.row_count == 1

            # A second completed snapshot can be compared with the previous
            # one entirely from the Jobs table.
            await pilot.press("u")
            await pilot.pause()
            second_reader = SqliteTraceStore(db_path)
            try:
                second_job = second_reader.jobs()[0]
            finally:
                second_reader.close()
            second_worker = SqliteTraceStore(db_path)
            try:
                Worker(
                    second_worker,
                    {
                        discovery_job.KIND: discovery_job.run_workflow_discovery_job
                    },
                    worker_id="worker:console-comparison",
                ).run_once()
            finally:
                second_worker.close()
            await pilot.press("r")
            await pilot.pause()
            jobs = app.query_one("#jobs", DataTable)
            jobs.focus()
            jobs.move_cursor(row=0)
            await pilot.pause()
            assert app._selected["jobs"] == second_job.job_id
            await pilot.press("j")  # compare discoveries
            await pilot.pause()
            assert "workflow discovery snapshot comparison" in app._last_detail

            discovered.focus()
            discovered.move_cursor(row=0)
            await pilot.pause()
            assert "member task digests" in app._last_detail
            assert "discovered family projection" in app._last_detail
            assert "path frequencies:" in app._last_detail

            await pilot.press("l")
            await pilot.pause()
            assert isinstance(app.screen, DiscoveredWorkflowDagScreen)
            dag = app.screen.query_one("#discovered-dag", Static)
            assert "INFERRED WORKFLOW DAG" in str(dag.render())
            await pilot.press("escape")
            await pilot.pause()

            await pilot.press("i")
            await pilot.pause()
            app.screen.query_one("#identify-workflow", Input).value = (
                "legacy-article"
            )
            app.screen.query_one("#identify-version", Input).value = (
                "observed:v1"
            )
            app.screen.query_one("#identify-output", Input).value = str(
                proposal_path
            )
            app.screen.query_one("#identify-create", Button).press()
            await pilot.pause()
            assert proposal_path.exists()
            assert "nothing activated" in app._last_notice

            await pilot.press("y")
            await pilot.pause()
            app.screen.query_one("#proposal-verify-path", Input).value = str(
                proposal_path
            )
            app.screen.query_one("#proposal-verify-run", Button).press()
            await pilot.pause()
            assert "Verified inert workflow proposal" in app._last_notice
            assert "nothing activated" in app._last_notice
    finally:
        ro.close()

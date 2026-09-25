"""The Experiment domain model and its persistence (both store backends).

Slice 1 of the live A/B tripwire engine: create/stop/list experiments and read
the running snapshot, with the two invariants the design leans on — one running
experiment per use-case, and immutability of a running experiment's split.
"""

from __future__ import annotations

import pytest

from ctrlrtn.policy.experiment import (
    DEFAULT_MAX_CALLS_PER_TASK,
    RUNNING,
    STOPPED,
    Experiment,
)
from ctrlrtn.policy.route import Route
from ctrlrtn.recorder.store import InMemoryTraceStore, SqliteTraceStore

# --- the pure model -------------------------------------------------------


def test_experiment_defaults_are_sensible():
    exp = Experiment("fp:abc", "claude-haiku-4-5", 10)
    assert exp.is_running
    assert exp.status == RUNNING
    assert exp.experiment_id.startswith("exp:")
    assert exp.max_calls_per_task == DEFAULT_MAX_CALLS_PER_TASK
    assert exp.created_epoch > 0
    assert exp.candidate_provider is None


@pytest.mark.parametrize("split", [0, 100, -1, 101])
def test_split_must_leave_both_arms_nonempty(split):
    with pytest.raises(ValueError, match="split_pct"):
        Experiment("fp:abc", "claude-haiku-4-5", split)


def test_missing_use_case_or_candidate_is_rejected():
    with pytest.raises(ValueError, match="use_case_key"):
        Experiment("", "claude-haiku-4-5", 10)
    with pytest.raises(ValueError, match="candidate_model"):
        Experiment("fp:abc", "", 10)


def test_workflow_and_step_scopes_validate_and_round_trip(store):
    with pytest.raises(ValueError, match="must be set together"):
        Experiment("fp:abc", "candidate", 10, workflow="pipeline")
    with pytest.raises(ValueError, match="step requires"):
        Experiment("fp:abc", "candidate", 10, step="draft")
    workflow_exp = Experiment(
        "fp:workflow",
        "candidate",
        10,
        workflow="pipeline",
        workflow_version="v1",
    )
    store.create_experiment(workflow_exp)
    workflow_scope = store.running_experiments()["fp:workflow"].scope
    assert workflow_scope.is_workflow_scoped
    assert not workflow_scope.is_step_scoped
    assert workflow_scope.label.endswith("pipeline@v1")
    exp = Experiment(
        "fp:abc",
        "candidate",
        10,
        workflow="pipeline",
        workflow_version="v1",
        step="draft",
    )
    store.create_experiment(exp)
    assert store.running_experiments()["fp:abc"].scope.label.endswith(
        "pipeline@v1/draft"
    )


def test_stopped_returns_a_stopped_copy_without_mutating():
    exp = Experiment("fp:abc", "claude-haiku-4-5", 10)
    stopped = exp.stopped()
    assert stopped.status == STOPPED and not stopped.is_running
    assert stopped.experiment_id == exp.experiment_id  # same experiment
    assert exp.is_running  # original untouched (frozen)


# --- persistence: run against both store backends -------------------------


@pytest.fixture(params=["memory", "sqlite"])
def store(request, tmp_path):
    if request.param == "memory":
        return InMemoryTraceStore()
    return SqliteTraceStore(str(tmp_path / "t.db"))


def test_create_then_read_running_snapshot(store):
    exp = Experiment("fp:editor", "claude-haiku-4-5", 20)
    store.create_experiment(exp)
    snap = store.running_experiments()
    assert set(snap) == {"fp:editor"}
    got = snap["fp:editor"]
    assert got.experiment_id == exp.experiment_id
    assert got.candidate_model == "claude-haiku-4-5"
    assert got.split_pct == 20


def test_one_running_experiment_per_use_case(store):
    store.create_experiment(Experiment("fp:editor", "claude-haiku-4-5", 20))
    with pytest.raises(ValueError, match="fp:editor"):
        store.create_experiment(Experiment("fp:editor", "claude-3-5-haiku", 50))


def test_stopping_frees_the_use_case_for_a_new_experiment(store):
    first = Experiment("fp:editor", "claude-haiku-4-5", 20)
    store.create_experiment(first)
    assert store.stop_experiment(first.experiment_id) is True
    assert store.running_experiments() == {}
    # a fresh experiment_id (the way to "change the split") is now allowed
    store.create_experiment(Experiment("fp:editor", "claude-haiku-4-5", 50))
    assert store.running_experiments()["fp:editor"].split_pct == 50


def test_stop_is_idempotent_and_reports_no_op(store):
    exp = Experiment("fp:editor", "claude-haiku-4-5", 20)
    store.create_experiment(exp)
    assert store.stop_experiment(exp.experiment_id) is True
    assert store.stop_experiment(exp.experiment_id) is False  # already stopped
    assert store.stop_experiment("exp:nope") is False


def test_experiments_lists_stopped_and_running_newest_first(store):
    a = Experiment("fp:a", "claude-haiku-4-5", 10, created_epoch=100.0)
    b = Experiment("fp:b", "claude-haiku-4-5", 10, created_epoch=200.0)
    store.create_experiment(a)
    store.create_experiment(b)
    store.stop_experiment(a.experiment_id)
    rows = store.experiments()
    assert [e.use_case_key for e in rows] == ["fp:b", "fp:a"]  # by created desc
    assert rows[1].status == STOPPED  # stopped ones still listed


def test_experiments_survive_reopen_for_sqlite(tmp_path):
    path = str(tmp_path / "persist.db")
    exp = Experiment("fp:editor", "claude-haiku-4-5", 25)
    SqliteTraceStore(path).create_experiment(exp)
    reopened = SqliteTraceStore(path)  # new connection, same file
    snap = reopened.running_experiments()
    assert snap["fp:editor"].experiment_id == exp.experiment_id
    assert snap["fp:editor"].split_pct == 25


def test_all_fields_round_trip_through_sqlite(tmp_path):
    # Guards against a column-order drift in INSERT/SELECT/_row_to_experiment.
    path = str(tmp_path / "rt.db")
    exp = Experiment(
        "fp:editor",
        "claude-haiku-4-5",
        25,
        experiment_id="exp:fixed01",
        created_epoch=1234.5,
        max_calls_per_task=42,
        candidate_provider="ollama",
    )
    SqliteTraceStore(path).create_experiment(exp)
    got = SqliteTraceStore(path).running_experiments()["fp:editor"]
    assert got == exp  # frozen dataclass equality: every field round-tripped


def test_provider_columns_migrate_into_existing_control_tables(tmp_path):
    import sqlite3

    path = str(tmp_path / "old-control.db")
    con = sqlite3.connect(path)
    con.execute(
        "CREATE TABLE experiments (experiment_id TEXT PRIMARY KEY, ts REAL "
        "NOT NULL, use_case_key TEXT NOT NULL, candidate_model TEXT NOT NULL, "
        "split_pct INTEGER NOT NULL, status TEXT NOT NULL, "
        "max_calls_per_task INTEGER NOT NULL, "
        "max_cost_usd_per_task REAL NOT NULL)"
    )
    con.execute(
        "CREATE TABLE routes (use_case_key TEXT PRIMARY KEY, model TEXT NOT "
        "NULL, previous_model TEXT, note TEXT, ts REAL NOT NULL)"
    )
    con.close()

    store = SqliteTraceStore(path)
    try:
        experiment_cols = {
            row[1]
            for row in store._conn.execute("PRAGMA table_info(experiments)")
        }
        route_cols = {
            row[1] for row in store._conn.execute("PRAGMA table_info(routes)")
        }
        assert "candidate_provider" in experiment_cols
        assert "max_cost_usd_per_task" not in experiment_cols
        assert "provider" in route_cols
    finally:
        store.close()


def test_read_only_store_reads_pre_provider_control_tables(tmp_path):
    import sqlite3

    path = str(tmp_path / "pre-provider.db")
    writer = SqliteTraceStore(path)
    writer.create_experiment(Experiment("fp:x", "model", 50))
    writer.set_route(Route("fp:x", "model"))
    writer.close()
    con = sqlite3.connect(path)
    con.execute("ALTER TABLE experiments DROP COLUMN candidate_provider")
    con.execute("ALTER TABLE routes DROP COLUMN provider")
    con.close()

    reader = SqliteTraceStore(path, read_only=True)
    try:
        assert reader.experiments()[0].candidate_provider is None
        assert reader.routes()[0].provider is None
    finally:
        reader.close()


def test_candidate_provider_must_be_nonempty_when_set():
    with pytest.raises(ValueError, match="candidate_provider"):
        Experiment("fp:abc", "model", 50, candidate_provider="")


def test_duplicate_experiment_id_rejected_on_both_backends(store):
    store.create_experiment(
        Experiment("fp:a", "claude-haiku-4-5", 10, experiment_id="exp:dup")
    )
    # A different use-case, so this is the id collision, not the one-running
    # invariant — both backends report it the same way.
    with pytest.raises(ValueError, match="already exists"):
        store.create_experiment(
            Experiment("fp:b", "claude-haiku-4-5", 10, experiment_id="exp:dup")
        )


def test_equal_epoch_lists_in_insertion_order_on_both_backends(store):
    first = Experiment("fp:a", "claude-haiku-4-5", 10, created_epoch=500.0)
    second = Experiment("fp:b", "claude-haiku-4-5", 10, created_epoch=500.0)
    store.create_experiment(first)
    store.create_experiment(second)
    # Wall-clock ties must not make the order backend-dependent: newest-inserted
    # first, deterministically, on both.
    assert [e.experiment_id for e in store.experiments()] == [
        second.experiment_id,
        first.experiment_id,
    ]

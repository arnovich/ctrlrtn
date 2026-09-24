"""The schema's shape and its creation order are contracts, not conventions.

Most reorderings inside ``initialize()`` fail loudly -- an index over a column
that migrate() has not added yet raises. Two do not:

* a trigger moved above the table its WHEN clause reads still succeeds,
  because SQLite resolves trigger bodies lazily;
* an index moved before the migration pass silently never covers the added
  column on a pre-existing database.

So the ordering was protected only by a comment. These tests protect it.
"""

from __future__ import annotations

import sqlite3
import threading

import pytest

from ctrlrtn.recorder.sqlite import schema
from ctrlrtn.recorder.sqlite.store import SqliteTraceStore

# The M0 schema: traces only, none of the later additive columns.
_M0 = """
CREATE TABLE traces (
    id INTEGER PRIMARY KEY AUTOINCREMENT, ts REAL NOT NULL,
    method TEXT NOT NULL, path TEXT NOT NULL, query TEXT NOT NULL,
    status_code INTEGER NOT NULL, latency_ms REAL NOT NULL, model TEXT,
    input_tokens INTEGER, output_tokens INTEGER, use_case_key TEXT,
    request_headers TEXT NOT NULL, request_body BLOB,
    response_headers TEXT NOT NULL, response_body BLOB)
"""


def _objects(connection: sqlite3.Connection) -> dict[str, set[str]]:
    rows = connection.execute(
        "SELECT type, name FROM sqlite_master WHERE name NOT LIKE 'sqlite_%'"
    )
    out: dict[str, set[str]] = {}
    for kind, name in rows:
        out.setdefault(kind, set()).add(name)
    return out


def test_fresh_and_m0_databases_converge_on_one_schema(tmp_path):
    """An upgraded old database is indistinguishable from a fresh one."""
    fresh = sqlite3.connect(str(tmp_path / "fresh.db"))
    schema.initialize(fresh)

    old_path = tmp_path / "old.db"
    old = sqlite3.connect(str(old_path))
    old.execute(_M0)
    old.execute(
        "INSERT INTO traces (ts, method, path, query, status_code,"
        " latency_ms, request_headers, response_headers)"
        " VALUES (1.0,'POST','/v1/x','',200,12.0,'{}','{}')"
    )
    old.commit()
    schema.initialize(old)

    assert _objects(old) == _objects(fresh)
    assert schema.columns(old, "traces") == schema.columns(fresh, "traces")
    # The upgrade preserves data; it does not rebuild the table.
    assert old.execute("SELECT COUNT(*) FROM traces").fetchone()[0] == 1


def test_every_declared_object_is_actually_created(tmp_path):
    """Guards against a DDL constant being defined but never executed."""
    conn = sqlite3.connect(str(tmp_path / "router.db"))
    schema.initialize(conn)
    created = _objects(conn)
    assert "traces" in created["table"]
    assert "jobs" in created["table"]
    assert "shadow_experiments" in created["table"]
    assert "workflow_events" in created["table"]
    # Triggers and indexes are the objects a reorder would silently drop.
    assert created.get("trigger"), "shadow exclusivity triggers missing"
    assert any(name.startswith("ix_") for name in created.get("index", ()))


def test_discovery_indexes_cover_migration_added_columns(tmp_path):
    """The post-migration indexes must be built after the columns exist.

    Creating them before migrate() would raise on a pre-existing database;
    this pins the ordering by upgrading an M0 file, where those columns are
    added rather than present from the start.
    """
    path = tmp_path / "old.db"
    conn = sqlite3.connect(str(path))
    conn.execute(_M0)
    conn.commit()
    schema.initialize(conn)
    indexed = {
        row[0]
        for row in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='index'"
            " AND sql IS NOT NULL"
        )
    }
    assert indexed, "no named indexes were created over the upgraded table"
    assert "workflow" in schema.columns(conn, "traces")


def test_initialize_is_idempotent(tmp_path):
    conn = sqlite3.connect(str(tmp_path / "router.db"))
    schema.initialize(conn)
    before = _objects(conn), schema.columns(conn, "traces")
    schema.initialize(conn)
    assert (_objects(conn), schema.columns(conn, "traces")) == before


def test_a_partial_schema_is_never_left_durable(tmp_path, monkeypatch):
    """A failure part-way must roll back, not leave half the tables behind."""
    path = tmp_path / "router.db"
    real = schema.migrate

    def boom(connection):
        real(connection)
        raise sqlite3.OperationalError("injected failure")

    monkeypatch.setattr(schema, "migrate", boom)
    conn = sqlite3.connect(str(path))
    with pytest.raises(sqlite3.OperationalError, match="injected"):
        schema.initialize(conn)
    conn.close()

    survivor = sqlite3.connect(str(path))
    assert _objects(survivor).get("table", set()) == set()


def _open_after_barrier(barrier, path, errors, timeout) -> None:
    """Open and close one store once every thread has reached the barrier."""
    try:
        barrier.wait(timeout=timeout)
        SqliteTraceStore(str(path)).close()
    except BaseException as exc:  # noqa: BLE001 - reported by the caller
        errors.append(exc)


def test_concurrent_cold_start_upgrades_one_database(tmp_path):
    """Two processes-worth of opens against one M0 file must both succeed.

    Previously both read PRAGMA table_info, both saw the column missing, and
    the loser raised 'duplicate column name'.
    """
    for attempt in range(20):
        path = tmp_path / f"shared-{attempt}.db"
        seed = sqlite3.connect(str(path))
        seed.execute(_M0)
        seed.commit()
        seed.close()

        errors: list[BaseException] = []
        barrier = threading.Barrier(4)
        threads = [
            threading.Thread(
                target=_open_after_barrier, args=(barrier, path, errors, 10)
            )
            for _ in range(4)
        ]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=30)

        assert not errors, f"concurrent cold start failed: {errors}"
    final = sqlite3.connect(str(path))
    assert "control_revision" in schema.columns(final, "traces")
    final.close()

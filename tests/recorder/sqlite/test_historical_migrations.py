"""Every frozen historical schema must converge without losing data."""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from ctrlrtn.recorder.sqlite.store import SqliteTraceStore

FIXTURES = Path(__file__).parents[2] / "fixtures" / "schema"


def _signature(path: Path) -> tuple[dict[str, set[str]], dict[str, set[str]]]:
    connection = sqlite3.connect(path)
    try:
        objects: dict[str, set[str]] = {}
        for kind, name in connection.execute(
            "SELECT type, name FROM sqlite_master "
            "WHERE name NOT LIKE 'sqlite_%'"
        ):
            objects.setdefault(kind, set()).add(name)
        columns = {
            table: {
                row[1]
                for row in connection.execute(f'PRAGMA table_info("{table}")')
            }
            for table in objects.get("table", set())
        }
        return objects, columns
    finally:
        connection.close()


@pytest.mark.parametrize(
    "fixture", sorted(FIXTURES.glob("*.sql")), ids=lambda path: path.stem
)
def test_historical_schema_converges_and_preserves_rows(tmp_path, fixture):
    historical = tmp_path / f"{fixture.stem}.db"
    connection = sqlite3.connect(historical)
    connection.executescript(fixture.read_text())
    connection.close()

    store = SqliteTraceStore(historical)
    try:
        assert store.count() == 1
        assert store.recent()[0]["path"] == f"/fixture-{fixture.stem[:2]}"
        if fixture.stem == "v1_control":
            assert store.experiment("exp:fixture-v1") is not None
            assert store.routes()[0].model == "model-v1"
    finally:
        store.close()

    first_signature = _signature(historical)
    reopened = SqliteTraceStore(historical)
    reopened.close()
    assert _signature(historical) == first_signature

    fresh = tmp_path / "fresh.db"
    SqliteTraceStore(fresh).close()
    assert _signature(historical) == _signature(fresh)

    reader = SqliteTraceStore(historical, read_only=True)
    try:
        assert reader.count() == 1
    finally:
        reader.close()

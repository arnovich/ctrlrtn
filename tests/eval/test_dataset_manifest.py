from __future__ import annotations

import copy
import hashlib
import json
from datetime import UTC, datetime

import pytest

from ctrlrtn.eval.dataset_manifest import (
    DatasetManifestError,
    create_dataset_manifest,
    verify_dataset_bindings,
    verify_dataset_manifest,
)
from ctrlrtn.recorder.sqlite.store import SqliteTraceStore
from ctrlrtn.recorder.trace import Trace

NOW = datetime(2026, 8, 2, tzinfo=UTC)


def _rows() -> list[dict]:
    return [
        {
            "id": 1,
            "task_id": "task:a",
            "request_body": b"request-a1",
            "response_body": b"response-a1",
        },
        {
            "id": 2,
            "task_id": "task:a",
            "request_body": b"request-a2",
            "response_body": b"response-a2",
        },
        {
            "id": 3,
            "task_id": "task:b",
            "request_body": b"request-b",
            "response_body": b"response-b",
        },
        {
            "id": 4,
            "task_id": None,
            "request_body": b"request-independent",
            "response_body": b"response-independent",
        },
    ]


def _manifest(rows=None) -> dict:
    return create_dataset_manifest(
        rows or _rows(),
        use_case="tag:editor",
        train_percent=67,
        salt="campaign-one",
        created_at=NOW,
    )


def test_manifest_is_deterministic_payload_free_and_task_clustered():
    first = _manifest()
    second = _manifest(list(reversed(_rows())))
    assert first == second
    assert verify_dataset_manifest(first) == first
    assert first["counts"] == {
        "traces": 4,
        "clusters": 3,
        "train_traces": 2,
        "evaluation_traces": 2,
        "train_clusters": 2,
        "evaluation_clusters": 1,
    }
    by_id = {entry["trace_id"]: entry for entry in first["entries"]}
    assert by_id[1]["cluster_sha256"] == by_id[2]["cluster_sha256"]
    assert by_id[1]["split"] == by_id[2]["split"]
    encoded = json.dumps(first)
    assert "task:a" not in encoded
    assert "request-a1" not in encoded
    assert (
        by_id[1]["request_sha256"] == hashlib.sha256(b"request-a1").hexdigest()
    )


def test_manifest_rejects_tampering_overlap_and_live_payload_change():
    manifest = _manifest()
    changed = copy.deepcopy(manifest)
    changed["entries"][0]["split"] = (
        "evaluation" if changed["entries"][0]["split"] == "train" else "train"
    )
    with pytest.raises(DatasetManifestError, match="digest"):
        verify_dataset_manifest(changed)

    changed = copy.deepcopy(_rows())
    changed[0]["request_body"] = b"altered"
    with pytest.raises(DatasetManifestError, match="payload changed"):
        verify_dataset_manifest(manifest, changed)

    pruned = copy.deepcopy(_rows())
    pruned[0]["request_body"] = None
    with pytest.raises(DatasetManifestError, match="payload was pruned"):
        verify_dataset_manifest(manifest, pruned)


def test_manifest_requires_two_clusters_and_available_response():
    with pytest.raises(DatasetManifestError, match="two clusters"):
        _manifest(_rows()[:2])
    rows = _rows()
    rows[0]["response_body"] = None
    with pytest.raises(DatasetManifestError, match="unavailable or pruned"):
        _manifest(rows)


def test_selected_binding_verifier_rejects_missing_and_changed_rows():
    manifest = _manifest()
    entries = [manifest["entries"][0]]
    row = next(row for row in _rows() if row["id"] == entries[0]["trace_id"])
    verify_dataset_bindings(entries, [row])
    with pytest.raises(DatasetManifestError, match="unavailable"):
        verify_dataset_bindings(entries, [])


def _seed(store: SqliteTraceStore, task_id: str | None, marker: str) -> None:
    store._insert(
        Trace(
            method="POST",
            path="/v1/messages",
            query="",
            request_headers={},
            request_body=f"request-{marker}".encode(),
            status_code=200,
            response_headers={},
            response_body=f"response-{marker}".encode(),
            latency_ms=1,
            use_case_key="tag:editor",
            task_id=task_id,
        )
    )


def test_cli_creates_and_verifies_live_lineage_without_payloads(
    tmp_path, monkeypatch, capsys
):
    from ctrlrtn.cli import commands as cli

    database = str(tmp_path / "router.db")
    output = tmp_path / "dataset.json"
    monkeypatch.setenv("CTRLRTN_DB", database)
    store = SqliteTraceStore(database)
    _seed(store, "task:a", "secret-a")
    _seed(store, "task:b", "secret-b")
    _seed(store, None, "secret-c")
    store.close()

    cli.main(
        [
            "dataset",
            "create",
            "tag:editor",
            str(output),
            "--train-percent",
            "67",
            "--salt",
            "test",
        ]
    )
    assert (
        "No request or response payloads were exported"
        in capsys.readouterr().out
    )
    raw = output.read_text()
    assert "secret-a" not in raw

    cli.main(["dataset", "verify", str(output)])
    assert "3 exact live trace payload binding" in capsys.readouterr().out

    store = SqliteTraceStore(database)
    store._conn.execute("UPDATE traces SET request_body = NULL WHERE id = 1")
    store._conn.commit()
    store.close()
    with pytest.raises(SystemExit, match="2"):
        cli.main(["dataset", "verify", str(output)])
    assert "payload was pruned" in capsys.readouterr().err

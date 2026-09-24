"""Repeated concurrency and lifecycle invariants for nightly CI."""

from __future__ import annotations

import asyncio
import sqlite3
import threading

import pytest

from ctrlrtn.jobs import Job, Worker
from ctrlrtn.recorder.memory_store import InMemoryTraceStore
from ctrlrtn.recorder.recorder import Recorder
from ctrlrtn.recorder.sqlite.store import SqliteTraceStore
from ctrlrtn.recorder.trace import Trace

pytestmark = pytest.mark.stress

_M0 = """
CREATE TABLE traces (
    id INTEGER PRIMARY KEY AUTOINCREMENT, ts REAL NOT NULL,
    method TEXT NOT NULL, path TEXT NOT NULL, query TEXT NOT NULL,
    status_code INTEGER NOT NULL, latency_ms REAL NOT NULL, model TEXT,
    input_tokens INTEGER, output_tokens INTEGER, use_case_key TEXT,
    request_headers TEXT NOT NULL, request_body BLOB,
    response_headers TEXT NOT NULL, response_body BLOB)
"""


def _trace(index: int) -> Trace:
    return Trace(
        method="POST",
        path=f"/stress/{index}",
        query="",
        request_headers={},
        request_body=b"{}",
        status_code=200,
        response_headers={},
        response_body=b"{}",
        latency_ms=1.0,
    )


def _open_after_barrier(barrier, path, errors) -> None:
    """Open and close one store once every thread has reached the barrier."""
    try:
        barrier.wait(timeout=5)
        SqliteTraceStore(path).close()
    except BaseException as exc:  # noqa: BLE001 - asserted by the caller
        errors.append(exc)


def test_eight_concurrent_cold_starts_converge_repeatedly(tmp_path):
    for attempt in range(10):
        path = tmp_path / f"cold-{attempt}.db"
        seed = sqlite3.connect(path)
        seed.execute(_M0)
        seed.commit()
        seed.close()
        barrier = threading.Barrier(8)
        errors: list[BaseException] = []
        threads = [
            threading.Thread(
                target=_open_after_barrier, args=(barrier, path, errors)
            )
            for _ in range(8)
        ]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=15)
        assert all(not thread.is_alive() for thread in threads)
        assert not errors


def test_competing_workers_complete_every_job_exactly_once(tmp_path):
    path = tmp_path / "workers.db"
    seed = SqliteTraceStore(path)
    for index in range(30):
        seed.create_job(Job("stress", {"index": index}, job_id=f"job:{index}"))
    seed.close()

    barrier = threading.Barrier(6)
    completed: list[int] = []
    completed_lock = threading.Lock()
    errors: list[BaseException] = []

    def handler(context, config):
        with completed_lock:
            completed.append(config["index"])
        return {"index": config["index"]}

    def run(worker_index: int) -> None:
        store = SqliteTraceStore(path)
        try:
            barrier.wait(timeout=5)
            worker = Worker(
                store,
                {"stress": handler},
                worker_id=f"worker:{worker_index}",
                heartbeat_interval=0.05,
            )
            while worker.run_once():
                pass
        except BaseException as exc:  # noqa: BLE001 - asserted below
            errors.append(exc)
        finally:
            store.close()

    threads = [
        threading.Thread(target=run, args=(index,)) for index in range(6)
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=30)

    assert all(not thread.is_alive() for thread in threads)
    assert not errors
    assert sorted(completed) == list(range(30))
    reader = SqliteTraceStore(path, read_only=True)
    try:
        assert all(job.status == "succeeded" for job in reader.jobs(limit=100))
    finally:
        reader.close()


async def test_recorder_repeated_start_drain_and_close_leaves_no_work():
    for attempt in range(25):
        store = InMemoryTraceStore()
        recorder = Recorder(store, drain_timeout=0.5)
        recorder.start()
        for index in range(10):
            assert recorder.enqueue(_trace(attempt * 10 + index))
        await asyncio.wait_for(recorder.aclose(), timeout=1.0)
        assert recorder._worker is None
        assert recorder._queue.empty()
        assert len(store.traces) == 10

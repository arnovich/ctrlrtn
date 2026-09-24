"""Durable job lifecycle, worker ownership, recovery, and cancellation."""

from __future__ import annotations

import sqlite3
import threading
import time

from ctrlrtn.jobs import CANCELLED, FAILED, RUNNING, SUCCEEDED, Job, Worker
from ctrlrtn.recorder.sqlite.store import SqliteTraceStore


def test_job_round_trip_and_read_only_listing(tmp_path):
    path = str(tmp_path / "jobs.db")
    writer = SqliteTraceStore(path)
    writer.create_job(
        Job("replay_eval", {"candidate": "local"}, job_id="job:a")
    )
    writer.close()

    reader = SqliteTraceStore(path, read_only=True)
    try:
        row = reader.jobs()[0]
    finally:
        reader.close()
    assert row.job_id == "job:a"
    assert row.kind == "replay_eval"
    assert row.config == {"candidate": "local"}


def test_jobs_page_by_offset_and_latest_job_looks_past_the_page(tmp_path):
    path = str(tmp_path / "jobs.db")
    writer = SqliteTraceStore(path)
    writer.create_job(Job("discovery", {}, job_id="job:old-discovery"))
    for i in range(5):
        writer.create_job(Job("replay_eval", {}, job_id=f"job:filler-{i}"))
    writer.close()

    reader = SqliteTraceStore(path, read_only=True)
    try:
        first = [job.job_id for job in reader.jobs(limit=2)]
        second = [job.job_id for job in reader.jobs(limit=2, offset=2)]
        assert first == ["job:filler-4", "job:filler-3"]
        assert second == ["job:filler-2", "job:filler-1"]
        assert reader.jobs(limit=2, offset=99) == []
        # The discovery job sits below every page of size 2, yet is found.
        assert reader.latest_job("discovery").job_id == "job:old-discovery"
        assert reader.latest_job("discovery", status=SUCCEEDED) is None
        assert reader.latest_job("no-such-kind") is None
    finally:
        reader.close()


def test_worker_reports_progress_and_completes(tmp_path):
    store = SqliteTraceStore(str(tmp_path / "jobs.db"))
    store.create_job(Job("test", {"n": 3}, job_id="job:a"))

    def run(context, config):
        context.progress(1, config["n"], "working")
        context.progress(3, config["n"], "done")
        return {"processed": 3}

    assert Worker(store, {"test": run}, worker_id="worker:a").run_once()
    row = store.job("job:a")
    assert row is not None
    assert row.status == SUCCEEDED
    assert row.progress_current == 3
    assert row.progress_total == 3
    assert row.result == {"processed": 3}
    assert row.worker_id == "worker:a"
    assert row.attempt == 1


def test_atomic_claim_gives_job_to_only_one_worker(tmp_path):
    path = str(tmp_path / "jobs.db")
    first = SqliteTraceStore(path)
    second = SqliteTraceStore(path)
    first.create_job(Job("test", {}, job_id="job:a"))

    claimed = first.claim_job("worker:a", stale_before=0.0)
    assert claimed is not None and claimed.status == RUNNING
    assert second.claim_job("worker:b", stale_before=0.0) is None


def test_stale_running_job_is_reclaimed(tmp_path):
    store = SqliteTraceStore(str(tmp_path / "jobs.db"))
    store.create_job(Job("test", {}, job_id="job:a"))
    assert store.claim_job("worker:old", stale_before=0.0) is not None
    store._conn.execute(
        "UPDATE jobs SET heartbeat_at = ? WHERE job_id = 'job:a'",
        (time.time() - 1000,),
    )
    store._conn.commit()

    claimed = store.claim_job("worker:new", stale_before=time.time() - 10)
    assert claimed is not None
    assert claimed.worker_id == "worker:new"
    assert claimed.attempt == 2


def test_worker_heartbeats_while_handler_is_busy(tmp_path):
    path = str(tmp_path / "jobs.db")
    store = SqliteTraceStore(path)
    observer = SqliteTraceStore(path)
    store.create_job(Job("slow", {}, job_id="job:a"))
    entered = threading.Event()
    release = threading.Event()

    def slow(context, config):
        entered.set()
        assert release.wait(1)
        return {}

    worker = Worker(
        store,
        {"slow": slow},
        worker_id="worker:a",
        stale_after=0.15,
        heartbeat_interval=0.02,
    )
    thread = threading.Thread(target=worker.run_once)
    thread.start()
    assert entered.wait(1)
    time.sleep(0.2)
    assert (
        observer.claim_job("worker:b", stale_before=time.time() - 0.15) is None
    )
    release.set()
    thread.join(1)
    assert not thread.is_alive()
    assert observer.job("job:a").status == SUCCEEDED


def test_queued_cancellation_is_terminal_and_never_claimed(tmp_path):
    store = SqliteTraceStore(str(tmp_path / "jobs.db"))
    store.create_job(Job("test", {}, job_id="job:a"))
    assert store.request_job_cancel("job:a")
    assert store.job("job:a").status == CANCELLED
    assert store.claim_job("worker:a", stale_before=0.0) is None


def test_running_cancellation_is_seen_at_checkpoint(tmp_path):
    store = SqliteTraceStore(str(tmp_path / "jobs.db"))
    store.create_job(Job("test", {}, job_id="job:a"))

    def run(context, config):
        assert store.request_job_cancel(context.job.job_id)
        return {"must": "not complete"}

    Worker(store, {"test": run}, worker_id="worker:a").run_once()
    row = store.job("job:a")
    assert row.status == CANCELLED
    assert row.result is None


def test_handler_failure_and_unknown_kind_are_persisted(tmp_path):
    store = SqliteTraceStore(str(tmp_path / "jobs.db"))
    store.create_job(Job("broken", {}, job_id="job:a"))

    def broken(context, config):
        raise RuntimeError("boom")

    Worker(store, {"broken": broken}, worker_id="worker:a").run_once()
    assert store.job("job:a").status == FAILED
    assert store.job("job:a").error == "boom"

    store.create_job(Job("unknown", {}, job_id="job:b"))
    Worker(store, {}, worker_id="worker:b").run_once()
    assert store.job("job:b").status == FAILED
    assert "no worker handler" in store.job("job:b").error


def test_existing_database_gets_jobs_table(tmp_path):
    path = str(tmp_path / "old.db")
    conn = sqlite3.connect(path)
    conn.execute("""CREATE TABLE traces (
            id INTEGER PRIMARY KEY AUTOINCREMENT, ts REAL NOT NULL,
            method TEXT NOT NULL, path TEXT NOT NULL, query TEXT NOT NULL,
            status_code INTEGER NOT NULL, latency_ms REAL NOT NULL, model TEXT,
            input_tokens INTEGER, output_tokens INTEGER, use_case_key TEXT,
            request_headers TEXT NOT NULL, request_body BLOB,
            response_headers TEXT NOT NULL, response_body BLOB
        )""")
    conn.commit()
    conn.close()

    store = SqliteTraceStore(path)
    store.create_job(Job("test", {}, job_id="job:a"))
    assert store.jobs()[0].job_id == "job:a"

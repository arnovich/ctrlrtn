"""Background worker, job administration, and retention CLI commands."""

from __future__ import annotations

import argparse
import json
import os
import sqlite3
import time
from collections.abc import Callable
from typing import Any, NoReturn

from ctrlrtn.jobs import Worker
from ctrlrtn.jobs.replay import KIND as REPLAY_JOB_KIND
from ctrlrtn.recorder.sqlite.store import SqliteTraceStore
from ctrlrtn.workflow.discovery_job import KIND as WORKFLOW_DISCOVERY_JOB_KIND

DatabasePath = Callable[[], str]
Fail = Callable[[str], NoReturn]
ReadJson = Callable[[str, str], dict]
StoreFactory = Callable[..., SqliteTraceStore]
JobHandler = Callable[..., Any]


class OperationsCommands:
    """Handlers for offline execution and database maintenance."""

    def __init__(
        self,
        database_path: DatabasePath,
        fail: Fail,
        read_json: ReadJson,
        store_factory: StoreFactory,
        replay_job_handler: JobHandler,
        discovery_job_handler: JobHandler,
    ) -> None:
        self._database_path = database_path
        self._fail = fail
        self._read_json = read_json
        self._store_factory = store_factory
        self._replay_job_handler = replay_job_handler
        self._discovery_job_handler = discovery_job_handler

    def _worker(self, args: argparse.Namespace) -> None:
        if args.poll <= 0:
            self._fail("--poll must be greater than zero")
        if args.stale_after <= 0:
            self._fail("--stale-after must be greater than zero")
        store = self._store_factory(self._database_path())
        handlers = {
            REPLAY_JOB_KIND: self._replay_job_handler,
            WORKFLOW_DISCOVERY_JOB_KIND: self._discovery_job_handler,
        }
        worker = Worker(store, handlers, stale_after=args.stale_after)
        try:
            while True:
                worked = worker.run_once()
                if args.once:
                    return
                if not worked:
                    time.sleep(args.poll)
        except KeyboardInterrupt:
            return
        finally:
            store.close()

    def _jobs_list(self, args: argparse.Namespace) -> None:
        store = self._store_factory(self._database_path())
        try:
            rows = store.jobs(limit=args.limit)
        finally:
            store.close()
        if not rows:
            print("No jobs.")
            return
        print(
            f"{'job':<17} {'kind':<20} {'progress':>10}  "
            f"{'state':<10} message"
        )
        for job in rows:
            progress = (
                f"{job.progress_current}/{job.progress_total}"
                if job.progress_total is not None
                else str(job.progress_current)
            )
            print(
                f"{job.job_id[:17]:<17} {job.kind[:20]:<20} {progress:>10}  "
                f"{job.status:<10} {job.progress_message or job.error or ''}"
            )

    def _jobs_cancel(self, args: argparse.Namespace) -> None:
        store = self._store_factory(self._database_path())
        try:
            changed = store.request_job_cancel(args.job_id)
        finally:
            store.close()
        if not changed:
            self._fail(f"job {args.job_id!r} is not queued or running")
        print(f"Cancellation requested for {args.job_id}.")

    def _jobs_export(self, args: argparse.Namespace) -> None:
        store = self._store_factory(self._database_path())
        try:
            job = store.job(args.job_id)
            invalidation = store.workflow_discovery_invalidation(args.job_id)
        finally:
            store.close()
        if job is None:
            self._fail(f"job {args.job_id!r} does not exist")
        if job.status != "succeeded" or job.result is None:
            self._fail(
                f"job {args.job_id!r} has no completed evidence to export"
            )
        if invalidation and not args.allow_invalidated:
            self._fail(
                f"job {args.job_id!r} source was invalidated by retention "
                f"({invalidation['pruned_traces']} pruned traces); pass "
                "--allow-invalidated to export the immutable stale artifact"
            )
        parent = os.path.dirname(args.path) or "."
        if not os.path.isdir(parent) or not os.access(parent, os.W_OK):
            self._fail(f"export path is not writable: {args.path}")
        try:
            with open(args.path, "w", encoding="utf-8") as handle:
                json.dump(job.result, handle)
                handle.write("\n")
        except OSError as exc:
            self._fail(f"could not export job evidence: {exc}")
        print(f"Wrote {args.path}.")
        if invalidation:
            print(
                "WARNING: exported artifact has retention-invalidated sources."
            )

    def _scrub_credentials(self, args: argparse.Namespace) -> None:
        store = self._store_factory(self._database_path())
        try:
            count = store.scrub_credential_headers()
        finally:
            store.close()
        print(
            f"Scrubbed credential headers from {count} recorded trace(s). "
            "New traces are redacted at capture time automatically."
        )

    def _prune(self, args: argparse.Namespace) -> None:
        if args.older_than_days < 1:
            self._fail("--older-than-days must be at least 1")
        if args.compact and not args.apply:
            self._fail("--compact requires --apply")
        cutoff = time.time() - (args.older_than_days * 86400)
        try:
            store = self._store_factory(
                self._database_path(), maintenance=args.compact
            )
            try:
                result = store.prune_trace_payloads(cutoff, apply=args.apply)
                compaction = store.compact() if args.compact else None
            finally:
                store.close()
        except (OSError, sqlite3.OperationalError, ValueError) as exc:
            self._fail(f"prune failed: {exc}")
        action = "Pruned" if result.applied else "Would prune"
        print(
            f"{action} {result.pruned_traces} trace payload(s) "
            f"({result.payload_bytes} bytes) older than "
            f"{args.older_than_days} day(s)."
        )
        if result.protected_traces:
            print(
                f"Protected {result.protected_traces} payload(s) referenced by "
                "queued or running jobs."
            )
        if result.invalidated_inferred_edges:
            verb = "Invalidated" if result.applied else "Would invalidate"
            print(
                f"{verb} {result.invalidated_inferred_edges} inferred workflow "
                "edge(s) derived from those payloads."
            )
        if result.invalidated_discovery_jobs:
            verb = "Marked" if result.applied else "Would mark"
            print(
                f"{verb} {result.invalidated_discovery_jobs} completed workflow "
                "discovery snapshot(s) as source-invalidated."
            )
        if not result.applied:
            print("Dry run only; pass --apply to erase these payloads.")
        if compaction is not None:
            print(
                f"Compacted database from {compaction.bytes_before} to "
                f"{compaction.bytes_after} bytes "
                f"({compaction.pages_before} to {compaction.pages_after} pages)."
            )

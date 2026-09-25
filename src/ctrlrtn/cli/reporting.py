"""Read-oriented traffic, spend, and enrichment CLI commands."""

from __future__ import annotations

import argparse
import os
import sqlite3
import sys
from collections.abc import Callable
from datetime import UTC, datetime

from ctrlrtn.analysis.report import render_trace
from ctrlrtn.cli.render import (
    render_budget_status,
    render_calls,
    render_rankings,
    render_sessions,
    render_tasks,
)
from ctrlrtn.config import Settings
from ctrlrtn.policy.budget import utc_day_start
from ctrlrtn.recorder.sqlite.store import SqliteTraceStore
from ctrlrtn.telemetry.enrich import enrich_trace

DatabasePath = Callable[[], str]
LoadSettings = Callable[[], Settings]


class ReportingCommands:
    """Handlers that inspect or re-enrich recorded gateway traffic."""

    def __init__(
        self, database_path: DatabasePath, load_runtime_settings: LoadSettings
    ) -> None:
        self._database_path = database_path
        self._load_settings = load_runtime_settings

    def _read_store(self, path: str | None = None) -> SqliteTraceStore:
        """Open the recording read-only; a missing file is an error, never a
        fresh empty database in whatever directory this ran from."""
        path = path or self._database_path()
        try:
            return SqliteTraceStore(path, read_only=True)
        except sqlite3.OperationalError as exc:
            print(
                f"no database at {os.path.abspath(path)} ({exc}); start the "
                "gateway with `ctrlrtn serve` first, or point CTRLRTN_DB at "
                "its recording",
                file=sys.stderr,
            )
            raise SystemExit(2) from None

    def _usecases(self, args: argparse.Namespace) -> None:
        store = self._read_store()
        try:
            print(render_rankings(store.rankings()))
        finally:
            store.close()

    def _spend(self, args: argparse.Namespace) -> None:
        """Print recorded spend; this command never changes routing or traces."""
        today = (
            datetime.now(UTC)
            .replace(hour=0, minute=0, second=0, microsecond=0)
            .timestamp()
        )
        store = self._read_store()
        try:
            total = store.spend_since(0.0)
            daily = store.spend_since(today)
            unpriced_total = store.unpriced_calls_since(0.0)
            unpriced_daily = store.unpriced_calls_since(today)
        finally:
            store.close()
        print(f"Recorded spend: ${total:.4f} total · ${daily:.4f} today (UTC)")
        print(
            f"Unknown-priced calls: {unpriced_total} total · "
            f"{unpriced_daily} today (UTC)"
        )

    def _budget(self, args: argparse.Namespace) -> None:
        """Print configured safeguards beside today's recorded accounting."""
        settings = self._load_settings()
        today = utc_day_start()
        store = self._read_store(settings.db_path)
        try:
            total, by_use_case = store.spend_breakdown_since(today)
            unknown = store.unpriced_calls_since(today)
            blocked = store.terminal_counts_since(today)
            fallback_calls = store.fallback_calls_since(today)
        finally:
            store.close()
        print(
            render_budget_status(
                settings.budget_policy,
                kill_switch=settings.kill_switch,
                daily_total=total,
                daily_by_use_case=by_use_case,
                unknown_priced_calls=unknown,
                blocked=blocked,
                fallback_calls=fallback_calls,
            )
        )

    def _calls(self, args: argparse.Namespace) -> None:
        store = self._read_store()
        try:
            print(render_calls(store.recent(args.limit)))
        finally:
            store.close()

    def _tasks(self, args: argparse.Namespace) -> None:
        store = self._read_store()
        try:
            print(render_tasks(store.tasks(args.limit), args.limit))
        finally:
            store.close()

    def _sessions(self, args: argparse.Namespace) -> None:
        settings = self._load_settings()
        store = self._read_store(settings.db_path)
        try:
            print(
                render_sessions(
                    store.sessions(args.limit),
                    args.limit,
                    session_limit_usd=(
                        settings.budget_policy.session_limit_usd
                    ),
                )
            )
        finally:
            store.close()

    def _show(self, args: argparse.Namespace) -> None:
        store = self._read_store()
        try:
            trace = store.get(args.id)
            print(
                render_trace(trace) if trace else f"No trace with id {args.id}."
            )
        finally:
            store.close()

    def _reenrich(self, args: argparse.Namespace) -> None:
        database_path = self._database_path()
        store = SqliteTraceStore(database_path)
        try:
            updated = store.reenrich(enrich_trace)
            print(f"Re-enriched {updated} rows in {database_path}.")
        finally:
            store.close()

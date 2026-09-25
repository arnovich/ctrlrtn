"""Gateway process and interactive console CLI commands."""

from __future__ import annotations

import argparse
import logging
import os
import sys
from collections.abc import Callable
from typing import Any, NoReturn

import uvicorn

from ctrlrtn import __version__
from ctrlrtn.analysis.report import _fmt_cost
from ctrlrtn.config import Settings, load_settings
from ctrlrtn.gateway.app import create_app
from ctrlrtn.policy.budget import BudgetGate, SpendSnapshot, utc_day_start
from ctrlrtn.recorder.recorder import Recorder
from ctrlrtn.recorder.sqlite.store import SqliteTraceStore
from ctrlrtn.recorder.trace import Trace
from ctrlrtn.telemetry.enrich import enrich_trace

Fail = Callable[[str], NoReturn]
LoadSettings = Callable[..., Settings]
BuildApp = Callable[..., Any]
ConfigureLogging = Callable[[str], int]


def _default_fail(message: str) -> NoReturn:
    import sys

    print(message, file=sys.stderr)
    raise SystemExit(2)


def _fmt_tokens(count: int | None) -> str:
    return "-" if count is None else str(count)


def _access_log(trace: Trace) -> None:
    """Print one compact line per recorded request."""
    print(
        f"{trace.method} {trace.path}  "
        f"{trace.provider or '-'}  "
        f"{trace.use_case_key or '(unkeyed)'}  "
        f"{trace.model or '-'}  {trace.status_code}  "
        f"{trace.latency_ms:.0f}ms  "
        f"in/out={_fmt_tokens(trace.input_tokens)}/"
        f"{_fmt_tokens(trace.output_tokens)}  "
        f"{_fmt_cost(trace.cost_usd)}",
        flush=True,
    )


def build_app(*, settings: Settings | None = None):
    """Wire the production SQLite recorder, budget gate, and gateway app."""
    settings = settings or load_settings()
    store = SqliteTraceStore(settings.db_path)
    budget_snapshot = (
        SpendSnapshot() if settings.budget_policy.enabled else None
    )
    if settings.budget_policy.enabled:
        total, by_use_case = store.spend_breakdown_since(utc_day_start())
        assert budget_snapshot is not None
        budget_snapshot.seed(total, by_use_case)
        if settings.budget_policy.session_limit_usd is not None:
            session_totals, unknown_sessions = store.session_spend_state()
            budget_snapshot.seed_sessions(
                session_totals, unknown_sessions=unknown_sessions
            )
    budget_gate = (
        BudgetGate(settings.budget_policy, budget_snapshot)
        if budget_snapshot is not None
        else None
    )

    def observe(trace: Trace) -> None:
        if budget_gate is not None:
            budget_gate.observe(trace)
        if settings.log_requests:
            _access_log(trace)

    recorder = Recorder(
        store,
        enrich=enrich_trace,
        on_record=(
            observe
            if budget_snapshot is not None or settings.log_requests
            else None
        ),
    )
    return create_app(
        settings,
        recorder=recorder,
        store=store,
        close_store_on_shutdown=True,
        budget_gate=budget_gate,
    )


def configure_logging(level: str, fail: Fail = _default_fail) -> int:
    """Configure the router logger and return uvicorn's numeric log level."""
    numeric = logging.getLevelNamesMapping().get(level.upper())
    if numeric is None:
        fail(
            f"invalid log level: {level!r} "
            "(use debug / info / warning / error / critical)"
        )
    router_logger = logging.getLogger("ctrlrtn")
    router_logger.setLevel(numeric)
    if not router_logger.handlers:
        handler = logging.StreamHandler()
        handler.setFormatter(
            logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s")
        )
        router_logger.addHandler(handler)
    return numeric


class RuntimeCommands:
    """Handlers that start long-lived or interactive router processes."""

    def __init__(
        self,
        load_runtime_settings: LoadSettings,
        fail: Fail,
        app_builder: BuildApp,
        logging_configurer: ConfigureLogging,
    ) -> None:
        self._load_settings = load_runtime_settings
        self._fail = fail
        self._build_app = app_builder
        self._configure_logging = logging_configurer

    def _serve(self, args: argparse.Namespace) -> None:
        settings = self._load_settings(
            overrides={
                "host": args.host,
                "port": args.port,
                "log_requests": args.log_requests,
                "log_level": args.log_level,
            }
        )
        numeric_level = self._configure_logging(settings.log_level)
        print(
            f"ctrlrtn {__version__}: listening on {settings.host}:{settings.port}, "
            f"recording to {os.path.abspath(settings.db_path)}",
            file=sys.stderr,
            flush=True,
        )
        uvicorn.run(
            self._build_app(settings=settings),
            host=settings.host,
            port=settings.port,
            log_level=numeric_level,
            # uvicorn's access line would print the raw query string, and a
            # ?key= credential with it; --log-requests logs the path only.
            access_log=False,
        )

    def _console(self, args: argparse.Namespace) -> None:
        try:
            from ctrlrtn.cli.console import ConsoleUnavailable, run_console
        except ImportError:
            import importlib.util

            if importlib.util.find_spec("textual") is None:
                self._fail(
                    "the console needs the TUI extra — install it with "
                    "`uv pip install -e '.[tui]'` from the checkout."
                )
            raise
        try:
            settings = self._load_settings()
            run_console(
                settings.db_path,
                refresh_seconds=args.refresh,
                budget_policy=settings.budget_policy,
                kill_switch=settings.kill_switch,
                routing_config_path=args.routing_config,
                routing_repo=args.routing_repo,
            )
        except ConsoleUnavailable as exc:
            self._fail(str(exc))

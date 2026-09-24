"""Experiment analysis, calibration, replay, and campaign CLI commands."""

from __future__ import annotations

import argparse
import json
import os
import random
import sqlite3
import sys
import time
from collections.abc import Callable
from typing import Any, NoReturn

import httpx

from ctrlrtn.analysis.campaign import (
    build_campaign_report,
    render_campaign_markdown,
    render_campaign_svg,
    replay_report_json,
)
from ctrlrtn.analysis.propagation import build_propagation_report
from ctrlrtn.analysis.recommend import build_recommendations
from ctrlrtn.analysis.report import render_tripwire
from ctrlrtn.cli.render import (
    render_calibration,
    render_propagation,
    render_recommendations,
    render_replay,
)
from ctrlrtn.eval.calibration import (
    ALIGNED,
    INSUFFICIENT,
    MISALIGNED,
    LabeledPairing,
    calibrate,
)
from ctrlrtn.eval.dataset_manifest import verify_dataset_manifest
from ctrlrtn.eval.judge import Pairing
from ctrlrtn.eval.live import JUDGE_TIMEOUT, REPLAY_TIMEOUT
from ctrlrtn.eval.ni import _MIN_UNITS as MIN_NI_UNITS
from ctrlrtn.eval.replay import (
    ReplaySample,
    extract_input_summary,
    run_replay,
)
from ctrlrtn.eval.tripwire import (
    DEFAULT_MIN_TASKS_PER_ARM,
    GROSS_REGRESSION,
    INCONCLUSIVE,
    NO_DATA,
    NO_GROSS_REGRESSION,
    NOT_EXERCISED,
    UNDERPOWERED,
    run_tripwire,
)
from ctrlrtn.jobs.replay import prepare_replay_job
from ctrlrtn.recorder.sqlite.store import SqliteTraceStore

DatabasePath = Callable[[], str]
Fail = Callable[[str], NoReturn]
StoreFactory = Callable[..., SqliteTraceStore]
LiveFunctionFactory = Callable[..., Callable[..., Any]]

_TRIPWIRE_EXIT = {
    NO_GROSS_REGRESSION: 0,
    GROSS_REGRESSION: 1,
    INCONCLUSIVE: 3,
    UNDERPOWERED: 3,
    NOT_EXERCISED: 4,
    NO_DATA: 4,
}
_CALIBRATION_EXIT = {ALIGNED: 0, MISALIGNED: 1, INSUFFICIENT: 3}


class EvaluationStatusCommands:
    """Render tripwire status, recommendations, and propagation evidence."""

    def _experiment_status(self, args: argparse.Namespace) -> None:
        if args.min_tasks < DEFAULT_MIN_TASKS_PER_ARM:
            self._fail(
                f"--min-tasks must be >= {DEFAULT_MIN_TASKS_PER_ARM} "
                "(a smaller sample can't rule out a gross regression)."
            )
        if not 0.0 < args.gross_margin < 1.0:
            self._fail(
                "--gross-margin must be between 0 and 1 (a failure-rate gap)."
            )
        if args.idle_minutes <= 0:
            self._fail("--idle-minutes must be > 0.")
        store = self._store_factory(self._database_path())
        try:
            experiment = next(
                (
                    item
                    for item in store.experiments(limit=10_000)
                    if item.experiment_id == args.experiment_id
                ),
                None,
            )
            if experiment is None:
                self._fail(
                    f"No experiment with id {args.experiment_id!r} "
                    "(run `experiment list`)."
                )
            rows = store.experiment_task_rows(experiment.experiment_id)
        finally:
            store.close()
        report = run_tripwire(
            rows,
            now=time.time(),
            idle_seconds=args.idle_minutes * 60.0,
            fail_below=args.fail_below,
            gross_margin=args.gross_margin,
            min_tasks_per_arm=args.min_tasks,
        )
        print(render_tripwire(report, experiment))
        raise SystemExit(_TRIPWIRE_EXIT[report.verdict])

    def _recommendations(self, args: argparse.Namespace) -> None:
        store = self._store_factory(self._database_path())
        try:
            recommendations = build_recommendations(
                store.rankings(), store.use_case_models()
            )
            print(render_recommendations(recommendations))
        finally:
            store.close()

    def _propagation(self, args: argparse.Namespace) -> None:
        store = self._store_factory(self._database_path())
        try:
            if args.use_case is not None:
                known = {row.use_case for row in store.rankings()}
                if args.use_case not in known:
                    self._fail(
                        f"Unknown use-case {args.use_case}. Run `ctrlrtn "
                        "usecases` to list keys."
                    )
            rows = store.task_use_case_counts(args.window)
            report = build_propagation_report(
                rows,
                focus_use_case=args.use_case,
                window_calls=args.window,
            )
        finally:
            store.close()
        print(render_propagation(report))
        raise SystemExit(0 if report.propagated else 1)

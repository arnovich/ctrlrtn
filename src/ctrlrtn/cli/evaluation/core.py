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


class EvaluationCommandContext:
    """Handlers that turn recorded evidence into bounded decisions."""

    def __init__(
        self,
        database_path: DatabasePath,
        fail: Fail,
        store_factory: StoreFactory,
        replay_function_factory: LiveFunctionFactory,
        judge_function_factory: LiveFunctionFactory,
    ) -> None:
        self._database_path = database_path
        self._fail = fail
        self._store_factory = store_factory
        self._replay_function_factory = replay_function_factory
        self._judge_function_factory = judge_function_factory

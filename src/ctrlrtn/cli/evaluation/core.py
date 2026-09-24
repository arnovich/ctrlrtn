"""Experiment analysis, calibration, replay, and campaign CLI commands."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any, NoReturn

from ctrlrtn.eval.calibration import (
    ALIGNED,
    INSUFFICIENT,
    MISALIGNED,
)
from ctrlrtn.eval.tripwire import (
    GROSS_REGRESSION,
    INCONCLUSIVE,
    NO_DATA,
    NO_GROSS_REGRESSION,
    NOT_EXERCISED,
    UNDERPOWERED,
)
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

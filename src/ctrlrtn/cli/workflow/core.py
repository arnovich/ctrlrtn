"""CLI adapter for workflow discovery, diagnostics, and analysis."""

from __future__ import annotations

from collections.abc import Callable
from typing import NoReturn

DatabasePath = Callable[[], str]
Fail = Callable[[str], NoReturn]
ReadJson = Callable[[str, str], dict]


class WorkflowCommandContext:
    """Bound terminal handlers for workflow analysis and job submission."""

    def __init__(
        self, database_path: DatabasePath, fail: Fail, read_json: ReadJson
    ) -> None:
        self._database_path = database_path
        self._fail = fail
        self._read_json = read_json

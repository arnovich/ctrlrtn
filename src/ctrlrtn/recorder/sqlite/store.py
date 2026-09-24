"""Stable SQLite store facade composed from capability-focused modules."""

from .connection import SqliteConnection
from .control import ControlSqliteMixin
from .jobs import JobSqliteMixin
from .maintenance import MaintenanceSqliteMixin
from .queries import _BUCKET_SERIES, _MODEL_RANKINGS, _RANKINGS, _where
from .reporting import ReportingSqliteMixin
from .results import (
    DatabaseCompaction,
    TracePayloadPrune,
    WorkflowDiscoveryInputDiagnostics,
    WorkflowTaskErasure,
)
from .traces import TraceSqliteMixin
from .workflow import WorkflowSqliteMixin


class SqliteTraceStore(
    WorkflowSqliteMixin,
    JobSqliteMixin,
    ControlSqliteMixin,
    MaintenanceSqliteMixin,
    TraceSqliteMixin,
    ReportingSqliteMixin,
    SqliteConnection,
):
    """SQLite implementation of the recorder capability protocols."""


__all__ = [
    "_BUCKET_SERIES",
    "_MODEL_RANKINGS",
    "_RANKINGS",
    "_where",
    "DatabaseCompaction",
    "SqliteTraceStore",
    "TracePayloadPrune",
    "WorkflowDiscoveryInputDiagnostics",
    "WorkflowTaskErasure",
]

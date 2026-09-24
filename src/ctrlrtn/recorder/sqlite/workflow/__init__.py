"""Composed SQLite workflow persistence capability."""

from .analysis import WorkflowAnalysisSqliteMixin
from .events import WorkflowEventSqliteMixin
from .reporting import WorkflowReportingSqliteMixin


class WorkflowSqliteMixin(
    WorkflowEventSqliteMixin,
    WorkflowReportingSqliteMixin,
    WorkflowAnalysisSqliteMixin,
):
    """Persist and project workflow observations across focused capabilities."""


__all__ = ["WorkflowSqliteMixin"]

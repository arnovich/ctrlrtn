"""Composed SQLite trace reporting capability."""

from .requests import RequestReportingSqliteMixin
from .tasks import TaskReportingSqliteMixin
from .usage import UsageReportingSqliteMixin


class ReportingSqliteMixin(
    UsageReportingSqliteMixin,
    RequestReportingSqliteMixin,
    TaskReportingSqliteMixin,
):
    """Project recorded traces through focused reporting capabilities."""


__all__ = ["ReportingSqliteMixin"]

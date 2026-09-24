"""Architecture guards for the composed SQLite store."""

from ctrlrtn.recorder.sqlite.connection import SqliteConnection
from ctrlrtn.recorder.sqlite.control import ControlSqliteMixin
from ctrlrtn.recorder.sqlite.control.configuration import (
    ConfigurationControlSqliteMixin,
)
from ctrlrtn.recorder.sqlite.control.experiments import (
    ExperimentControlSqliteMixin,
)
from ctrlrtn.recorder.sqlite.control.fallbacks import (
    FallbackControlSqliteMixin,
)
from ctrlrtn.recorder.sqlite.jobs import JobSqliteMixin
from ctrlrtn.recorder.sqlite.maintenance import MaintenanceSqliteMixin
from ctrlrtn.recorder.sqlite.reporting import ReportingSqliteMixin
from ctrlrtn.recorder.sqlite.reporting.requests import (
    RequestReportingSqliteMixin,
)
from ctrlrtn.recorder.sqlite.reporting.tasks import (
    TaskReportingSqliteMixin,
)
from ctrlrtn.recorder.sqlite.reporting.usage import (
    UsageReportingSqliteMixin,
)
from ctrlrtn.recorder.sqlite.store import SqliteTraceStore
from ctrlrtn.recorder.sqlite.traces import TraceSqliteMixin
from ctrlrtn.recorder.sqlite.workflow import WorkflowSqliteMixin
from ctrlrtn.recorder.sqlite.workflow.analysis import (
    WorkflowAnalysisSqliteMixin,
)
from ctrlrtn.recorder.sqlite.workflow.events import (
    WorkflowEventSqliteMixin,
)
from ctrlrtn.recorder.sqlite.workflow.reporting import (
    WorkflowReportingSqliteMixin,
)


def test_sqlite_store_is_a_capability_facade():
    """Keep persistence behavior out of the public composition module."""
    assert SqliteTraceStore.__bases__ == (
        WorkflowSqliteMixin,
        JobSqliteMixin,
        ControlSqliteMixin,
        MaintenanceSqliteMixin,
        TraceSqliteMixin,
        ReportingSqliteMixin,
        SqliteConnection,
    )
    assert not {
        name
        for name, value in SqliteTraceStore.__dict__.items()
        if callable(value) and not name.startswith("__")
    }


def test_cross_table_transactions_remain_in_one_capability():
    """The module split must not fragment control or erasure transactions."""
    assert (
        SqliteTraceStore.adopt_experiment is ControlSqliteMixin.adopt_experiment
    )
    assert (
        SqliteTraceStore.activate_control_config
        is ControlSqliteMixin.activate_control_config
    )
    assert (
        SqliteTraceStore.erase_workflow_task
        is MaintenanceSqliteMixin.erase_workflow_task
    )


def test_large_sqlite_capabilities_are_composed_from_focused_modules():
    assert WorkflowSqliteMixin.__bases__ == (
        WorkflowEventSqliteMixin,
        WorkflowReportingSqliteMixin,
        WorkflowAnalysisSqliteMixin,
    )
    assert ControlSqliteMixin.__bases__ == (
        ExperimentControlSqliteMixin,
        ConfigurationControlSqliteMixin,
        FallbackControlSqliteMixin,
    )
    assert ReportingSqliteMixin.__bases__ == (
        UsageReportingSqliteMixin,
        RequestReportingSqliteMixin,
        TaskReportingSqliteMixin,
    )


def test_nested_sqlite_facades_do_not_own_behavior():
    for facade in (
        WorkflowSqliteMixin,
        ControlSqliteMixin,
        ReportingSqliteMixin,
    ):
        assert not {
            name
            for name, value in facade.__dict__.items()
            if callable(value) and not name.startswith("__")
        }


def test_composed_store_implements_every_capability():
    """A composition missing a mixin must fail at instantiation, not later."""
    assert not SqliteTraceStore.__abstractmethods__

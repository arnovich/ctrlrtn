"""The typed contract every SQLite capability mixin is composed on."""

from __future__ import annotations

import sqlite3
import threading
from abc import ABC, abstractmethod

from ctrlrtn.jobs import Job
from ctrlrtn.policy.experiment import Experiment
from ctrlrtn.recorder.models import Outcome
from ctrlrtn.recorder.trace import Trace
from ctrlrtn.workflow.identity import WorkflowEvent
from ctrlrtn.workflow.inference import InferredWorkflowEdge
from ctrlrtn.workflow.metrics import WorkflowStepMetric
from ctrlrtn.workflow.tool_operation import ToolOperationEvent

from . import schema
from .queries import _SELECT_EXPERIMENTS, _SELECT_ROUTES


class SqliteCapability(ABC):
    """Typed contract shared by every SQLite capability mixin.

    ``SqliteConnection`` assigns the attributes; the mixins only read them.
    The abstract methods are each implemented by exactly one mixin and are
    declared here so the other mixins (and the ``save*`` entry points) can
    call them on ``self``. Composing a store that leaves one unimplemented
    fails at class instantiation, and mypy reports it as ``[abstract]``.
    """

    _conn: sqlite3.Connection
    _lock: threading.Lock
    _path: str
    _maintenance: bool
    _has_experiment_provider: bool
    _has_experiment_scope: bool
    _has_route_provider: bool

    def _has_column(self, table: str, name: str) -> bool:
        return schema.has_column(self._conn, table, name)

    def _experiment_select(self) -> str:
        query = _SELECT_EXPERIMENTS
        if not self._has_experiment_provider:
            query = query.replace(
                "candidate_provider", "NULL AS candidate_provider"
            )
        if not self._has_experiment_scope:
            query = query.replace(
                "workflow, workflow_version, step",
                "NULL AS workflow, NULL AS workflow_version, NULL AS step",
            )
        return query

    def _route_select(self) -> str:
        if self._has_route_provider:
            return _SELECT_ROUTES
        return _SELECT_ROUTES.replace("provider", "NULL AS provider")

    # Implemented by TraceSqliteMixin.
    @abstractmethod
    def _insert(self, trace: Trace) -> None: ...

    @abstractmethod
    def _insert_outcome(self, outcome: Outcome) -> None: ...

    # Implemented by WorkflowEventSqliteMixin.
    @abstractmethod
    def _insert_workflow_event(self, event: WorkflowEvent) -> None: ...

    @abstractmethod
    def _insert_tool_operation_event(
        self, event: ToolOperationEvent
    ) -> None: ...

    @abstractmethod
    def workflow_events(
        self, task_id: str | None = None
    ) -> list[WorkflowEvent]: ...

    @abstractmethod
    def tool_operation_events(
        self, task_id: str | None = None
    ) -> list[ToolOperationEvent]: ...

    # Implemented by WorkflowReportingSqliteMixin.
    @abstractmethod
    def workflow_step_metrics(
        self,
        workflow: str | None = None,
        workflow_version: str | None = None,
    ) -> list[WorkflowStepMetric]: ...

    # Implemented by WorkflowAnalysisSqliteMixin.
    @abstractmethod
    def inferred_workflow_edges(self) -> list[InferredWorkflowEdge]: ...

    # Implemented by JobSqliteMixin.
    @abstractmethod
    def jobs(self, limit: int = 100, *, offset: int = 0) -> list[Job]: ...

    # Implemented by ExperimentControlSqliteMixin.
    @abstractmethod
    def experiment(self, experiment_id: str) -> Experiment | None: ...

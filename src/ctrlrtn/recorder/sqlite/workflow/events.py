"""SQLite workflow-event, graph, discovery, and inference persistence."""

from __future__ import annotations

import json
import time

from ctrlrtn.workflow.graph import WorkflowGraph, build_workflow_graph
from ctrlrtn.workflow.identity import WorkflowEvent, WorkflowIdentity
from ctrlrtn.workflow.inference import InferredWorkflowEdge
from ctrlrtn.workflow.metrics import (
    WorkflowStepMetric,
    build_workflow_step_metrics,
)
from ctrlrtn.workflow.tool_operation import (
    ToolOperationEvent,
    ToolOperationIdentity,
)

from ..queries import _INSERT_TOOL_EVENT, _INSERT_WORKFLOW_EVENT
from ..results import WorkflowDiscoveryInputDiagnostics


class WorkflowEventSqliteMixin:
    """Persist and project workflow and tool-operation observations."""

    def _insert_tool_operation_event(self, event: ToolOperationEvent) -> None:
        identity = event.identity
        workflow = identity.workflow_identity
        with self._lock:
            self._conn.execute(
                _INSERT_TOOL_EVENT,
                (
                    event.event_id,
                    event.ts,
                    workflow.task_id,
                    workflow.workflow,
                    workflow.workflow_version,
                    workflow.step,
                    workflow.step_run_id,
                    workflow.parent_step_run_id,
                    json.dumps(workflow.dependency_step_run_ids),
                    workflow.attempt,
                    identity.operation,
                    identity.operation_id,
                    identity.attempt_id,
                    identity.attempt,
                    identity.effect,
                    event.status,
                    event.success,
                    event.error_code,
                    event.latency_ms,
                    event.cost_usd,
                ),
            )
            self._conn.commit()

    def tool_operation_events(
        self, task_id: str | None = None
    ) -> list[ToolOperationEvent]:
        query = """SELECT event_id, ts, task_id, workflow, workflow_version,
                          step, step_run_id, parent_step_run_id,
                          dependency_step_run_ids, step_attempt, operation,
                          operation_id, attempt_id, attempt, effect, status,
                          success, error_code, latency_ms, cost_usd
                   FROM tool_operation_events"""
        params: tuple = ()
        if task_id is not None:
            query += " WHERE task_id = ?"
            params = (task_id,)
        query += " ORDER BY id"
        with self._lock:
            rows = self._conn.execute(query, params).fetchall()
        return [
            ToolOperationEvent(
                ToolOperationIdentity(
                    WorkflowIdentity(
                        row[2],
                        row[3],
                        row[4],
                        row[5],
                        row[6],
                        row[7],
                        tuple(json.loads(row[8])),
                        row[9],
                    ),
                    row[10],
                    row[11],
                    row[12],
                    row[13],
                    row[14],
                ),
                row[15],
                event_id=row[0],
                success=None if row[16] is None else bool(row[16]),
                error_code=row[17],
                latency_ms=row[18],
                cost_usd=row[19],
                ts=row[1],
            )
            for row in rows
        ]

    def _insert_workflow_event(self, event: WorkflowEvent) -> None:
        identity = event.identity
        with self._lock:
            self._conn.execute(
                _INSERT_WORKFLOW_EVENT,
                (
                    event.event_id,
                    event.ts,
                    identity.task_id,
                    identity.workflow,
                    identity.workflow_version,
                    identity.step,
                    identity.step_run_id,
                    identity.parent_step_run_id,
                    json.dumps(identity.dependency_step_run_ids),
                    identity.attempt,
                    event.status,
                    event.success,
                    event.score,
                    event.error_code,
                ),
            )
            self._conn.commit()

    def workflow_events(
        self, task_id: str | None = None
    ) -> list[WorkflowEvent]:
        query = """SELECT event_id, ts, task_id, workflow, workflow_version,
                          step, step_run_id, parent_step_run_id,
                          dependency_step_run_ids, attempt, status, success,
                          score, error_code
                   FROM workflow_events"""
        params: tuple = ()
        if task_id is not None:
            query += " WHERE task_id = ?"
            params = (task_id,)
        query += " ORDER BY id"
        with self._lock:
            rows = self._conn.execute(query, params).fetchall()
        return [
            WorkflowEvent(
                event_id=row[0],
                ts=row[1],
                identity=WorkflowIdentity(
                    task_id=row[2],
                    workflow=row[3],
                    workflow_version=row[4],
                    step=row[5],
                    step_run_id=row[6],
                    parent_step_run_id=row[7],
                    dependency_step_run_ids=tuple(json.loads(row[8])),
                    attempt=row[9],
                ),
                status=row[10],
                success=None if row[11] is None else bool(row[11]),
                score=row[12],
                error_code=row[13],
            )
            for row in rows
        ]

    def workflow_diagnostics(self) -> dict:
        """Persistence-level identity and lifecycle consistency signals."""
        with self._lock:
            valid = self._conn.execute(
                "SELECT COUNT(*) FROM traces WHERE workflow IS NOT NULL"
            ).fetchone()[0]
            errors = self._conn.execute(
                """SELECT workflow_identity_error, COUNT(*) FROM traces
                   WHERE workflow_identity_error IS NOT NULL
                   GROUP BY workflow_identity_error"""
            ).fetchall()
            reused = self._conn.execute("""SELECT COUNT(*) FROM (
                       SELECT step_run_id FROM (
                           SELECT task_id, workflow, workflow_version, step,
                                  step_run_id FROM workflow_events
                           UNION
                           SELECT task_id, workflow, workflow_version, step,
                                  step_run_id FROM traces
                           WHERE step_run_id IS NOT NULL
                       ) identities
                       GROUP BY step_run_id
                       HAVING COUNT(DISTINCT task_id || char(31) || workflow ||
                           char(31) || workflow_version || char(31) || step) > 1
                   )""").fetchone()[0]
            conflicts = self._conn.execute("""SELECT COUNT(*) FROM (
                       SELECT task_id, step_run_id FROM workflow_events
                       WHERE status IN ('completed','failed','cancelled','skipped')
                       GROUP BY task_id, step_run_id
                       HAVING COUNT(DISTINCT status) > 1
                   )""").fetchone()[0]
            cross_task_dependencies = self._conn.execute(
                """SELECT COUNT(*) FROM workflow_events e, json_each(
                       e.dependency_step_run_ids) dependency
                   WHERE EXISTS (
                       SELECT 1 FROM workflow_events target
                       WHERE target.step_run_id = dependency.value
                         AND target.task_id <> e.task_id
                   ) AND NOT EXISTS (
                       SELECT 1 FROM workflow_events target
                       WHERE target.step_run_id = dependency.value
                         AND target.task_id = e.task_id
                   )"""
            ).fetchone()[0]
        metrics = self.workflow_step_metrics()
        return {
            "valid_traces": int(valid),
            "invalid_traces": sum(int(row[1]) for row in errors),
            "errors": {row[0]: int(row[1]) for row in errors},
            "reused_step_run_ids": int(reused),
            "conflicting_terminal_runs": int(conflicts),
            "cross_task_dependencies": int(cross_task_dependencies),
            "unreported_step_runs": sum(row.unreported_runs for row in metrics),
            "inconsistent_step_runs": sum(row.inconsistent for row in metrics),
        }

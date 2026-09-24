"""SQLite workflow-event, graph, discovery, and inference persistence."""

from __future__ import annotations

from ctrlrtn.workflow.graph import WorkflowGraph, build_workflow_graph
from ctrlrtn.workflow.metrics import (
    WorkflowStepMetric,
    build_workflow_step_metrics,
)


class WorkflowReportingSqliteMixin:
    """Project workflow observations into diagnostics and read models."""

    def workflow_step_metrics(
        self,
        workflow: str | None = None,
        workflow_version: str | None = None,
    ) -> list[WorkflowStepMetric]:
        clauses = ["workflow IS NOT NULL"]
        params: list[str] = []
        if workflow is not None:
            clauses.append("workflow = ?")
            params.append(workflow)
        if workflow_version is not None:
            clauses.append("workflow_version = ?")
            params.append(workflow_version)
        with self._lock:
            rows = self._conn.execute(
                """SELECT task_id, workflow, workflow_version, step,
                          step_run_id, status_code, cost_usd, input_tokens,
                          output_tokens, latency_ms
                   FROM traces WHERE """ + " AND ".join(clauses),
                tuple(params),
            ).fetchall()
        traces = [
            {
                "task_id": row[0],
                "workflow": row[1],
                "workflow_version": row[2],
                "step": row[3],
                "step_run_id": row[4],
                "status_code": row[5],
                "cost_usd": row[6],
                "input_tokens": row[7],
                "output_tokens": row[8],
                "latency_ms": row[9],
            }
            for row in rows
        ]
        events = [
            event
            for event in self.workflow_events()
            if (workflow is None or event.identity.workflow == workflow)
            and (
                workflow_version is None
                or event.identity.workflow_version == workflow_version
            )
        ]
        return build_workflow_step_metrics(traces, events)

    def workflow_runs(self, limit: int = 50, *, offset: int = 0) -> list[dict]:
        with self._lock:
            rows = self._conn.execute(
                """SELECT task_id, workflow, workflow_version, step_run_id,
                          ts, cost_usd, 1 AS is_trace
                   FROM traces WHERE workflow IS NOT NULL
                   UNION ALL
                   SELECT task_id, workflow, workflow_version, step_run_id,
                          ts, NULL, 0
                   FROM workflow_events"""
            ).fetchall()
        groups: dict[tuple[str, str, str], dict] = {}
        for task, workflow, version, run_id, ts, cost, is_trace in rows:
            group = groups.setdefault(
                (task, workflow, version),
                {
                    "task_id": task,
                    "workflow": workflow,
                    "workflow_version": version,
                    "step_runs": set(),
                    "calls": 0,
                    "cost_usd": 0.0,
                    "last_ts": 0.0,
                },
            )
            group["step_runs"].add(run_id)
            group["last_ts"] = max(group["last_ts"], float(ts))
            if is_trace:
                group["calls"] += 1
                group["cost_usd"] += float(cost or 0.0)
        result = []
        for group in groups.values():
            group["step_runs"] = len(group["step_runs"])
            result.append(group)
        ordered = sorted(result, key=lambda row: -row["last_ts"])
        return ordered[offset : offset + limit]

    def workflow_graph(self, task_id: str) -> WorkflowGraph | None:
        with self._lock:
            rows = self._conn.execute(
                """SELECT id, ts, task_id, workflow, workflow_version, step,
                          step_run_id, step_attempt, cost_usd, latency_ms,
                          provider, model, served_model
                   FROM traces
                   WHERE task_id = ? AND workflow IS NOT NULL ORDER BY id""",
                (task_id,),
            ).fetchall()
        traces = [
            {
                "id": row[0],
                "ts": row[1],
                "task_id": row[2],
                "workflow": row[3],
                "workflow_version": row[4],
                "step": row[5],
                "step_run_id": row[6],
                "step_attempt": row[7],
                "cost_usd": row[8],
                "latency_ms": row[9],
                "provider": row[10],
                "model": row[11],
                "served_model": row[12],
            }
            for row in rows
        ]
        events = self.workflow_events(task_id)
        inferred = [
            edge
            for edge in self.inferred_workflow_edges()
            if edge.task_id == task_id
        ]
        return build_workflow_graph(task_id, traces, events, inferred)

    def workflow_graphs(
        self, workflow: str, workflow_version: str, limit: int = 1000
    ) -> list[WorkflowGraph]:
        """Return recent exact-version task graphs for offline aggregation."""
        if not workflow or not workflow_version:
            raise ValueError("workflow and version must be non-empty")
        if limit < 1:
            raise ValueError("workflow graph limit must be at least 1")
        selected = [
            row
            for row in self.workflow_runs(limit=100000)
            if row["workflow"] == workflow
            and row["workflow_version"] == workflow_version
        ][:limit]
        graphs = [self.workflow_graph(row["task_id"]) for row in selected]
        return [
            graph
            for graph in graphs
            if graph is not None
            and graph.workflow == workflow
            and graph.workflow_version == workflow_version
        ]

    def workflow_step_detail(
        self, task_id: str, step_run_id: str
    ) -> dict | None:
        """Metadata-only connected detail for one exact workflow step run."""
        with self._lock:
            rows = self._conn.execute(
                """SELECT id, ts, workflow, workflow_version, step, step_attempt,
                          status_code, cost_usd, latency_ms, provider,
                          COALESCE(served_model, model), experiment_id, arm,
                          shadow_experiment_id, shadow_role, route_rule_scope,
                          route_rule_key, control_revision
                   FROM traces WHERE task_id = ? AND step_run_id = ? ORDER BY id""",
                (task_id, step_run_id),
            ).fetchall()
        events = [
            event
            for event in self.workflow_events(task_id)
            if event.identity.step_run_id == step_run_id
        ]
        tool_events = [
            event
            for event in self.tool_operation_events(task_id)
            if event.identity.workflow_identity.step_run_id == step_run_id
        ]
        if not rows and not events and not tool_events:
            return None
        if rows:
            identity = (rows[0][2], rows[0][3], rows[0][4], rows[0][5])
        elif events:
            identity = (
                events[0].identity.workflow,
                events[0].identity.workflow_version,
                events[0].identity.step,
                events[0].identity.attempt,
            )
        else:
            identity = (
                tool_events[0].identity.workflow_identity.workflow,
                tool_events[0].identity.workflow_identity.workflow_version,
                tool_events[0].identity.workflow_identity.step,
                tool_events[0].identity.workflow_identity.attempt,
            )
        traces = [
            {
                "id": row[0],
                "ts": row[1],
                "status_code": row[6],
                "cost_usd": row[7] or 0.0,
                "latency_ms": row[8] or 0.0,
                "provider": row[9],
                "model": row[10],
                "experiment_id": row[11],
                "arm": row[12],
                "shadow_experiment_id": row[13],
                "shadow_role": row[14],
                "route_rule_scope": row[15],
                "route_rule_key": row[16],
                "control_revision": row[17],
            }
            for row in rows
        ]
        jobs = [
            job
            for job in self.jobs(limit=1000)
            if job.config.get("workflow") == identity[0]
            and job.config.get("workflow_version") == identity[1]
            and job.config.get("step") == identity[2]
        ]
        return {
            "task_id": task_id,
            "step_run_id": step_run_id,
            "workflow": identity[0],
            "workflow_version": identity[1],
            "step": identity[2],
            "attempt": identity[3],
            "traces": traces,
            "events": events,
            "jobs": jobs,
            "tool_events": tool_events,
        }

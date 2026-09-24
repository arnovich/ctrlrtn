"""SQLite workflow-event, graph, discovery, and inference persistence."""

from __future__ import annotations

import json
import time

from ctrlrtn.workflow.inference import InferredWorkflowEdge

from ..connection import SqliteCapability
from ..results import WorkflowDiscoveryInputDiagnostics


class WorkflowAnalysisSqliteMixin(SqliteCapability):
    """Persist workflow inference and discovery inputs and outputs."""

    def workflow_inference_inputs(
        self, task_id: str | None = None
    ) -> tuple[list[dict], set[tuple[str, str, str]], set[tuple[str, str]]]:
        where = " WHERE task_id = ?" if task_id is not None else ""
        params = (task_id,) if task_id is not None else ()
        with self._lock:
            traces = self._conn.execute(
                """SELECT id, task_id, workflow, workflow_version, step_run_id,
                          request_body, response_body
                   FROM traces
                   WHERE workflow IS NOT NULL"""
                + (" AND task_id = ?" if task_id is not None else "")
                + " ORDER BY id",
                params,
            ).fetchall()
            events = self._conn.execute(
                """SELECT task_id, step_run_id, dependency_step_run_ids
                   FROM workflow_events""" + where,
                params,
            ).fetchall()
        trace_rows = [
            {
                "id": row[0],
                "task_id": row[1],
                "workflow": row[2],
                "workflow_version": row[3],
                "step_run_id": row[4],
                "request_body": row[5],
                "response_body": row[6],
            }
            for row in traces
        ]
        dependencies = {
            (row[0], dependency, row[1])
            for row in events
            for dependency in json.loads(row[2])
        }
        explicit_runs = {(row[0], row[1]) for row in events}
        return trace_rows, dependencies, explicit_runs

    def workflow_discovery_inputs(
        self,
        limit: int = 1000,
        *,
        since: float | None = None,
        until: float | None = None,
        provider: str | None = None,
        model: str | None = None,
        experiment_id: str | None = None,
        arm: str | None = None,
    ) -> list[dict]:
        """Return bounded recent legacy traffic for analysis-only discovery.

        Explicitly identified workflows already have authoritative aggregation
        and are excluded. Bodies are read only to recover exact tool links; the
        discovery result retains neither bodies nor raw task IDs. ``limit``
        bounds explicit tasks and, separately, unscoped calls that may be
        conservatively correlated in memory.
        """
        if isinstance(limit, bool) or not isinstance(limit, int) or limit < 1:
            raise ValueError("workflow discovery limit must be at least 1")
        having = []
        parameters: list[object] = []
        if since is not None:
            having.append("MAX(candidate.ts) >= ?")
            parameters.append(since)
        if until is not None:
            having.append("MAX(candidate.ts) < ?")
            parameters.append(until)
        for column, value in (
            ("candidate.provider", provider),
            ("COALESCE(candidate.served_model, candidate.model)", model),
            ("candidate.experiment_id", experiment_id),
            ("candidate.arm", arm),
        ):
            if value is not None:
                having.append(
                    f"SUM(CASE WHEN {column} = ? THEN 1 ELSE 0 END) > 0"
                )
                parameters.append(value)
        if experiment_id is not None:
            having.append("COUNT(DISTINCT candidate.experiment_id) = 1")
        if arm is not None:
            having.append("COUNT(DISTINCT candidate.arm) = 1")
        cohort_having = " HAVING " + " AND ".join(having) if having else ""
        candidate_sql = f"""SELECT candidate.task_id FROM traces AS candidate
                       WHERE candidate.task_id IS NOT NULL
                         AND NOT EXISTS (
                             SELECT 1 FROM traces AS declared
                             WHERE declared.task_id = candidate.task_id
                               AND declared.workflow IS NOT NULL
                         )
                       GROUP BY candidate.task_id{cohort_having}
                       ORDER BY MAX(candidate.id) DESC LIMIT ?"""
        with self._lock:
            rows = list(
                self._conn.execute(
                    f"""SELECT id, ts, task_id, use_case_key, request_body,
                          response_body, provider, model, served_model,
                          input_tokens, output_tokens, cost_usd, latency_ms,
                          status_code, experiment_id, arm
                   FROM traces AS selected
                   WHERE selected.workflow IS NULL
                     AND selected.task_id IN ({candidate_sql})
                   ORDER BY id""",
                    (*parameters, limit),
                ).fetchall()
            )
            if all(
                value is None for value in (provider, model, experiment_id, arm)
            ):
                unscoped_where = ["workflow IS NULL", "task_id IS NULL"]
                unscoped_parameters: list[object] = []
                if since is not None:
                    unscoped_where.append("ts >= ?")
                    unscoped_parameters.append(since)
                if until is not None:
                    unscoped_where.append("ts < ?")
                    unscoped_parameters.append(until)
                rows.extend(
                    self._conn.execute(
                        f"""SELECT id, ts, task_id, use_case_key, request_body,
                              response_body, provider, model, served_model,
                              input_tokens, output_tokens, cost_usd, latency_ms,
                              status_code, experiment_id, arm
                       FROM traces
                       WHERE {' AND '.join(unscoped_where)}
                       ORDER BY id DESC LIMIT ?""",
                        (*unscoped_parameters, limit),
                    ).fetchall()
                )
            rows.sort(key=lambda row: row[0])
        return [
            {
                "id": row[0],
                "ts": row[1],
                "task_id": row[2],
                "use_case_key": row[3],
                "request_body": row[4],
                "response_body": row[5],
                "provider": row[6],
                "model": row[7],
                "served_model": row[8],
                "input_tokens": row[9],
                "output_tokens": row[10],
                "cost_usd": row[11],
                "latency_ms": row[12],
                "status_code": row[13],
                "experiment_id": row[14],
                "arm": row[15],
            }
            for row in rows
        ]

    def workflow_discovery_input_diagnostics(
        self,
        rows: list[dict],
        *,
        since: float | None = None,
        until: float | None = None,
        provider: str | None = None,
        model: str | None = None,
        experiment_id: str | None = None,
        arm: str | None = None,
    ) -> WorkflowDiscoveryInputDiagnostics:
        """Explain the bounded cohort denominator behind a frozen sample."""
        having = []
        parameters: list[object] = []
        if since is not None:
            having.append("MAX(candidate.ts) >= ?")
            parameters.append(since)
        if until is not None:
            having.append("MAX(candidate.ts) < ?")
            parameters.append(until)
        for column, value in (
            ("candidate.provider", provider),
            ("COALESCE(candidate.served_model, candidate.model)", model),
            ("candidate.experiment_id", experiment_id),
            ("candidate.arm", arm),
        ):
            if value is not None:
                having.append(
                    f"SUM(CASE WHEN {column} = ? THEN 1 ELSE 0 END) > 0"
                )
                parameters.append(value)
        if experiment_id is not None:
            having.append("COUNT(DISTINCT candidate.experiment_id) = 1")
        if arm is not None:
            having.append("COUNT(DISTINCT candidate.arm) = 1")
        cohort_having = " HAVING " + " AND ".join(having) if having else ""
        cohort = f"""SELECT candidate.task_id FROM traces AS candidate
                     WHERE candidate.task_id IS NOT NULL
                       AND NOT EXISTS (
                           SELECT 1 FROM traces AS declared
                           WHERE declared.task_id = candidate.task_id
                             AND declared.workflow IS NOT NULL
                       )
                     GROUP BY candidate.task_id{cohort_having}"""
        legacy = """SELECT candidate.task_id FROM traces AS candidate
                    WHERE candidate.task_id IS NOT NULL
                      AND NOT EXISTS (
                          SELECT 1 FROM traces AS declared
                          WHERE declared.task_id = candidate.task_id
                            AND declared.workflow IS NOT NULL
                      )
                    GROUP BY candidate.task_id"""
        unscoped_where = ["workflow IS NULL", "task_id IS NULL"]
        unscoped_parameters: list[object] = []
        if since is not None:
            unscoped_where.append("ts >= ?")
            unscoped_parameters.append(since)
        if until is not None:
            unscoped_where.append("ts < ?")
            unscoped_parameters.append(until)
        with self._lock:
            matching_tasks = int(
                self._conn.execute(
                    f"SELECT COUNT(*) FROM ({cohort})", parameters
                ).fetchone()[0]
            )
            legacy_tasks = int(
                self._conn.execute(
                    f"SELECT COUNT(*) FROM ({legacy})"
                ).fetchone()[0]
            )
            declared_tasks = int(
                self._conn.execute(
                    "SELECT COUNT(DISTINCT task_id) FROM traces "
                    "WHERE workflow IS NOT NULL AND task_id IS NOT NULL"
                ).fetchone()[0]
            )
            all_unscoped = int(
                self._conn.execute(
                    "SELECT COUNT(*) FROM traces "
                    "WHERE workflow IS NULL AND task_id IS NULL"
                ).fetchone()[0]
            )
            matching_unscoped = (
                int(
                    self._conn.execute(
                        "SELECT COUNT(*) FROM traces WHERE "
                        + " AND ".join(unscoped_where),
                        unscoped_parameters,
                    ).fetchone()[0]
                )
                if all(
                    value is None
                    for value in (provider, model, experiment_id, arm)
                )
                else 0
            )
        explicit_ids = {
            row["task_id"] for row in rows if row.get("task_id") is not None
        }
        selected_unscoped = sum(row.get("task_id") is None for row in rows)
        return WorkflowDiscoveryInputDiagnostics(
            available_explicit_tasks=matching_tasks,
            selected_explicit_tasks=len(explicit_ids),
            truncated_explicit_tasks=max(0, matching_tasks - len(explicit_ids)),
            excluded_by_scope_tasks=max(0, legacy_tasks - matching_tasks),
            declared_tasks=declared_tasks,
            available_unscoped_traces=matching_unscoped,
            selected_unscoped_traces=selected_unscoped,
            truncated_unscoped_traces=max(
                0, matching_unscoped - selected_unscoped
            ),
            excluded_unscoped_traces=max(0, all_unscoped - matching_unscoped),
            selected_traces=len(rows),
            pruned_selected_traces=sum(
                row.get("request_body") is None
                and row.get("response_body") is None
                for row in rows
            ),
            unkeyable_selected_traces=sum(
                not row.get("use_case_key") for row in rows
            ),
        )

    def workflow_discovery_inputs_by_ids(
        self, trace_ids: list[int]
    ) -> list[dict]:
        """Reload an exact frozen discovery sample in caller-supplied order."""
        if any(
            isinstance(trace_id, bool)
            or not isinstance(trace_id, int)
            or trace_id < 1
            for trace_id in trace_ids
        ):
            raise ValueError(
                "workflow discovery trace IDs must be positive integers"
            )
        rows = []
        with self._lock:
            for trace_id in trace_ids:
                row = self._conn.execute(
                    """SELECT id, ts, task_id, use_case_key, request_body,
                              response_body, provider, model, served_model,
                              input_tokens, output_tokens, cost_usd, latency_ms,
                              status_code, experiment_id, arm
                       FROM traces WHERE id = ?""",
                    (trace_id,),
                ).fetchone()
                if row is not None:
                    rows.append(row)
        return [
            {
                "id": row[0],
                "ts": row[1],
                "task_id": row[2],
                "use_case_key": row[3],
                "request_body": row[4],
                "response_body": row[5],
                "provider": row[6],
                "model": row[7],
                "served_model": row[8],
                "input_tokens": row[9],
                "output_tokens": row[10],
                "cost_usd": row[11],
                "latency_ms": row[12],
                "status_code": row[13],
                "experiment_id": row[14],
                "arm": row[15],
            }
            for row in rows
        ]

    def replace_inferred_workflow_edges(
        self, edges: tuple[InferredWorkflowEdge, ...], algorithm: str
    ) -> None:
        with self._lock:
            try:
                self._conn.execute(
                    "DELETE FROM inferred_workflow_edges WHERE algorithm = ?",
                    (algorithm,),
                )
                created_at = time.time()
                self._conn.executemany(
                    """INSERT INTO inferred_workflow_edges (
                           edge_id, created_at, algorithm, task_id, workflow,
                           workflow_version, source_step_run_id, target_step_run_id,
                           source_trace_id, target_trace_id, evidence_hash,
                           confidence, confirmation
                       ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    [
                        (
                            edge.edge_id,
                            created_at,
                            edge.algorithm,
                            edge.task_id,
                            edge.workflow,
                            edge.workflow_version,
                            edge.source_step_run_id,
                            edge.target_step_run_id,
                            edge.source_trace_id,
                            edge.target_trace_id,
                            edge.evidence_hash,
                            edge.confidence,
                            edge.confirmation,
                        )
                        for edge in edges
                    ],
                )
                self._conn.commit()
            except Exception:
                self._conn.rollback()
                raise

    def inferred_workflow_edges(self) -> list[InferredWorkflowEdge]:
        with self._lock:
            rows = self._conn.execute(
                """SELECT edge_id, task_id, workflow, workflow_version,
                          source_step_run_id, target_step_run_id,
                          source_trace_id, target_trace_id, evidence_hash,
                          confidence, confirmation, algorithm
                   FROM inferred_workflow_edges ORDER BY created_at, edge_id"""
            ).fetchall()
        return [InferredWorkflowEdge(*row) for row in rows]

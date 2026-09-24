"""SQLite trace, task, session, spend, and experiment reporting queries."""

from __future__ import annotations

import hashlib
import json
import sqlite3
import time

from ctrlrtn.policy.scope import ExperimentScope
from ctrlrtn.recorder.models import UNKEYED as _UNKEYED
from ctrlrtn.recorder.models import UNSESSIONED
from ctrlrtn.recorder.models import UNTASKED as _UNTASKED
from ctrlrtn.recorder.models import (
    ModelRanking,
    SessionSummary,
    TaskSummary,
    UseCaseRanking,
)
from ctrlrtn.workflow.identity import TERMINAL_STATUSES

from ..queries import (
    _BUCKET_SERIES,
    _EXPERIMENT_TASK_ROWS,
    _FALLBACK_CALLS_SINCE,
    _GET,
    _MODEL_RANKINGS,
    _PRICING_IDENTITIES_SINCE,
    _RANKINGS,
    _RANKINGS_ARM_FILTER,
    _RECENT,
    _REQUESTS_FOR_USE_CASE,
    _SESSION_SPEND_STATE,
    _SESSIONS,
    _SPEND_BREAKDOWN_SINCE,
    _SPEND_SINCE,
    _TASK_USE_CASE_COUNTS,
    _TASKS,
    _TERMINAL_COUNTS_SINCE,
    _USE_CASE_MODELS,
    _where,
)


class TaskReportingSqliteMixin:
    """Project task, session, and experiment-task summaries."""

    def tasks(
        self, limit: int = 50, *, since: float | None = None
    ) -> list[TaskSummary]:
        """The costliest tasks, newest outcome attached. ``since`` (a Unix
        timestamp) limits it to a trailing window; the default spans all
        recorded history."""
        params: list = [_UNTASKED]
        clauses = []
        if since is not None:
            clauses.append("t.ts >= ?")
            params.append(since)
        params.append(limit)
        with self._lock:
            rows = self._conn.execute(
                _TASKS.format(where=_where(clauses)), tuple(params)
            ).fetchall()
        return [
            TaskSummary(
                task_id=row[0],
                calls=int(row[1]),
                cost_usd=float(row[2]),
                input_tokens=int(row[3]),
                output_tokens=int(row[4]),
                use_cases=int(row[5]),
                errors=int(row[6] or 0),
                success=None if row[7] is None else bool(row[7]),
                score=None if row[8] is None else float(row[8]),
            )
            for row in rows
        ]

    def sessions(self, limit: int = 50) -> list[SessionSummary]:
        with self._lock:
            rows = self._conn.execute(
                _SESSIONS, (UNSESSIONED, limit)
            ).fetchall()
        return [
            SessionSummary(
                session_id=row[0],
                calls=int(row[1]),
                cost_usd=float(row[2]),
                input_tokens=int(row[3]),
                output_tokens=int(row[4]),
                use_cases=int(row[5]),
                errors=int(row[6] or 0),
                unknown_cost_calls=int(row[7] or 0),
            )
            for row in rows
        ]

    def experiment_task_rows(self, experiment_id: str) -> list[dict]:
        """Per-task aggregates for one experiment, for the tripwire analysis
        (eval/tripwire.py). One dict per task_id: arm served (+ distinct-arm
        count and cross-experiment count for the purity checks), call count,
        first/last ts, ceiling-terminal count, and the latest reported outcome.
        """
        experiment = self.experiment(experiment_id)
        with self._lock:
            rows = self._conn.execute(
                _EXPERIMENT_TASK_ROWS, (experiment_id,)
            ).fetchall()
            step_facts = (
                self._conn.execute(
                    """SELECT DISTINCT t.task_id, t.step_run_id, e.status,
                                      e.success, e.score, e.id
                       FROM traces t
                       LEFT JOIN workflow_events e
                         ON e.task_id = t.task_id
                        AND e.step_run_id = t.step_run_id
                       WHERE t.experiment_id = ?
                       ORDER BY e.id""",
                    (experiment_id,),
                ).fetchall()
                if experiment is not None and experiment.scope.is_step_scoped
                else []
            )
            workflow_facts = (
                self._conn.execute(
                    """SELECT task_id,
                              COUNT(DISTINCT workflow || char(0) ||
                                             workflow_version),
                              SUM(CASE WHEN workflow IS NULL OR
                                            workflow_version IS NULL
                                       THEN 1 ELSE 0 END)
                       FROM traces
                       WHERE experiment_id = ? AND task_id IS NOT NULL
                       GROUP BY task_id""",
                    (experiment_id,),
                ).fetchall()
                if experiment is not None
                and experiment.scope.is_workflow_scoped
                else []
            )
            path_facts = (
                self._conn.execute(
                    """SELECT e.task_id, e.step_run_id, e.step, e.attempt,
                              MIN(e.ts) AS first_ts, MIN(e.id) AS first_id
                       FROM workflow_events e
                       WHERE e.workflow = ? AND e.workflow_version = ?
                         AND e.task_id IN (
                             SELECT DISTINCT task_id FROM traces
                             WHERE experiment_id = ? AND task_id IS NOT NULL
                         )
                       GROUP BY e.task_id, e.step_run_id, e.step, e.attempt
                       ORDER BY e.task_id, first_ts, first_id, e.step_run_id""",
                    (
                        experiment.workflow,
                        experiment.workflow_version,
                        experiment_id,
                    ),
                ).fetchall()
                if experiment is not None
                and experiment.scope.is_workflow_scoped
                and not experiment.scope.is_step_scoped
                else []
            )
        result = [
            {
                "task_id": row[0],
                "calls": int(row[1]),
                "cost": float(row[2] or 0.0),
                "arms": int(row[3]),
                "arm": row[4],
                "served_model": row[5],
                "last_ts": float(row[6]),
                "ceilings": int(row[7] or 0),
                "experiments": int(row[8] or 0),
                "success": None if row[9] is None else bool(row[9]),
                "score": None if row[10] is None else float(row[10]),
            }
            for row in rows
        ]
        if workflow_facts:
            facts_by_task = {
                row[0]: (int(row[1] or 0), int(row[2] or 0))
                for row in workflow_facts
            }
            paths_by_task: dict[str, list[str]] = {}
            for task_id, _, step, attempt, _, _ in path_facts:
                paths_by_task.setdefault(task_id, []).append(
                    f"{step}#{int(attempt)}"
                )
            for row in result:
                identities, missing = facts_by_task.get(row["task_id"], (0, 0))
                path = tuple(paths_by_task.get(row["task_id"], ()))
                row["workflow_identities"] = identities
                row["missing_workflow_identity"] = missing
                row["path"] = path
                row["path_digest"] = (
                    hashlib.sha256(
                        json.dumps(path, separators=(",", ":")).encode()
                    ).hexdigest()[:16]
                    if path
                    else None
                )
        if not step_facts:
            return result
        latest: dict[
            tuple[str, str], tuple[str | None, int | None, float | None]
        ] = {}
        runs_by_task: dict[str, set[str]] = {}
        for task_id, run_id, status, success, score, _ in step_facts:
            if task_id is None or run_id is None:
                continue
            runs_by_task.setdefault(task_id, set()).add(run_id)
            if status in TERMINAL_STATUSES:
                latest[(task_id, run_id)] = (status, success, score)
        for row in result:
            runs = runs_by_task.get(row["task_id"], set())
            outcomes = [latest.get((row["task_id"], run_id)) for run_id in runs]
            explicit = [outcome for outcome in outcomes if outcome is not None]
            successes = [
                None if outcome[1] is None else bool(outcome[1])
                for outcome in explicit
            ]
            row["success"] = (
                False
                if False in successes
                else (
                    True
                    if runs
                    and len(explicit) == len(runs)
                    and all(value is True for value in successes)
                    else None
                )
            )
            scores = [
                outcome[2] for outcome in explicit if outcome[2] is not None
            ]
            row["score"] = sum(scores) / len(scores) if scores else None
        return result

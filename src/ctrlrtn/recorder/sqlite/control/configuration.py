"""SQLite experiment, shadow, routing, and control-config persistence."""

from __future__ import annotations

import json
import sqlite3

from ctrlrtn.control_config import (
    ControlConfig,
    ControlRevision,
    WorkflowDefinition,
    WorkflowStepDefinition,
)
from ctrlrtn.policy.experiment import RUNNING, STOPPED
from ctrlrtn.policy.route import Route, WorkflowRoute

from ..capability import SqliteCapability
from ..queries import (
    _INSERT_EXPERIMENT,
    _SELECT_WORKFLOW_ROUTES,
    _STOP_EXPERIMENT,
    _UPSERT_ROUTE,
    _row_to_experiment,
    _row_to_route,
    _same_experiment,
    _same_route,
)


class ConfigurationControlSqliteMixin(SqliteCapability):
    """Persist routes, workflow definitions, and atomic control revisions."""

    def set_route(self, route: Route) -> None:
        with self._lock:
            self._conn.execute(
                _UPSERT_ROUTE,
                (
                    route.use_case_key,
                    route.model,
                    route.previous_model,
                    route.note,
                    route.ts,
                    route.provider,
                ),
            )
            self._conn.commit()

    def clear_route(self, use_case_key: str) -> bool:
        with self._lock:
            cur = self._conn.execute(
                "DELETE FROM routes WHERE use_case_key = ?", (use_case_key,)
            )
            self._conn.commit()
            return cur.rowcount > 0

    def routes(self) -> list[Route]:
        with self._lock:
            try:
                rows = self._conn.execute(self._route_select()).fetchall()
            except sqlite3.OperationalError as exc:
                if "no such table" in str(exc):
                    # A read-only open (console) of a database created before
                    # routes existed: no DDL ran, no table — and no routes.
                    return []
                raise
        return [_row_to_route(row) for row in rows]

    def workflow_routes(self) -> list[WorkflowRoute]:
        with self._lock:
            try:
                rows = self._conn.execute(_SELECT_WORKFLOW_ROUTES).fetchall()
            except sqlite3.OperationalError as exc:
                if "no such table" in str(exc):
                    return []
                raise
        return [
            WorkflowRoute(
                workflow=row[0],
                workflow_version=row[1],
                step=row[2] or None,
                model=row[3],
                provider=row[4],
                note=row[5],
                ts=row[6],
            )
            for row in rows
        ]

    def workflow_definitions(self) -> list[WorkflowDefinition]:
        with self._lock:
            try:
                rows = self._conn.execute(
                    """SELECT workflow, workflow_version, definition_json
                       FROM workflow_definitions ORDER BY workflow, workflow_version"""
                ).fetchall()
            except sqlite3.OperationalError as exc:
                if "no such table" in str(exc):
                    return []
                raise
        return [
            WorkflowDefinition(
                workflow,
                version,
                tuple(
                    WorkflowStepDefinition(
                        **{
                            **step,
                            "predecessors": tuple(step["predecessors"]),
                        }
                    )
                    for step in json.loads(raw)
                ),
            )
            for workflow, version, raw in rows
        ]

    def control_revision(self) -> ControlRevision | None:
        with self._lock:
            try:
                row = self._conn.execute(
                    """SELECT revision, source_path, document_sha256, activated_at
                       FROM control_config_state WHERE singleton = 1"""
                ).fetchone()
            except sqlite3.OperationalError as exc:
                if "no such table" in str(exc):
                    return None
                raise
        return ControlRevision(*row) if row else None

    def activate_control_config(
        self, config: ControlConfig, revision: ControlRevision
    ) -> None:
        """Atomically make routes/running experiments match ``config``.

        Historical stopped experiments remain as evidence. Unchanged desired
        objects retain their original activation timestamps; any failure rolls
        back the entire reconciliation, including provenance.
        """
        activated = revision.activated_at
        with self._lock:
            try:
                self._conn.execute("BEGIN IMMEDIATE")
                current_experiments = {
                    row[2]: _row_to_experiment(row)
                    for row in self._conn.execute(
                        self._experiment_select() + " WHERE status = ?",
                        (RUNNING,),
                    ).fetchall()
                }
                desired_experiments = {
                    exp.use_case_key: exp for exp in config.experiments
                }
                for use_case, current in current_experiments.items():
                    desired = desired_experiments.get(use_case)
                    if desired is None or not _same_experiment(
                        current, desired
                    ):
                        self._conn.execute(
                            _STOP_EXPERIMENT,
                            (STOPPED, current.experiment_id, RUNNING),
                        )
                for use_case, desired in desired_experiments.items():
                    running = current_experiments.get(use_case)
                    if running is not None and _same_experiment(
                        running, desired
                    ):
                        continue
                    self._conn.execute(
                        _INSERT_EXPERIMENT,
                        (
                            desired.experiment_id,
                            activated,
                            desired.use_case_key,
                            desired.candidate_model,
                            desired.split_pct,
                            RUNNING,
                            desired.max_calls_per_task,
                            desired.candidate_provider,
                            desired.workflow,
                            desired.workflow_version,
                            desired.step,
                        ),
                    )

                current_routes = {
                    row[0]: _row_to_route(row)
                    for row in self._conn.execute(
                        self._route_select()
                    ).fetchall()
                }
                desired_routes = {
                    route.use_case_key: route for route in config.routes
                }
                for use_case in set(current_routes) - set(desired_routes):
                    self._conn.execute(
                        "DELETE FROM routes WHERE use_case_key = ?", (use_case,)
                    )
                for use_case, desired_route in desired_routes.items():
                    current_route = current_routes.get(use_case)
                    if current_route is not None and _same_route(
                        current_route, desired_route
                    ):
                        continue
                    self._conn.execute(
                        _UPSERT_ROUTE,
                        (
                            desired_route.use_case_key,
                            desired_route.model,
                            desired_route.previous_model,
                            desired_route.note,
                            activated,
                            desired_route.provider,
                        ),
                    )

                current_workflow_routes = {
                    (row[0], row[1], row[2] or None): row
                    for row in self._conn.execute(
                        _SELECT_WORKFLOW_ROUTES
                    ).fetchall()
                }
                desired_workflow_routes = {
                    route.key: route for route in config.workflow_routes
                }
                for key in set(current_workflow_routes) - set(
                    desired_workflow_routes
                ):
                    self._conn.execute(
                        """DELETE FROM workflow_routes
                           WHERE workflow = ? AND workflow_version = ? AND step = ?""",
                        (key[0], key[1], key[2] or ""),
                    )
                for key, workflow_route in desired_workflow_routes.items():
                    current_row = current_workflow_routes.get(key)
                    same = current_row is not None and (
                        current_row[3],
                        current_row[4],
                        current_row[5],
                    ) == (
                        workflow_route.model,
                        workflow_route.provider,
                        workflow_route.note,
                    )
                    if same:
                        continue
                    self._conn.execute(
                        """INSERT OR REPLACE INTO workflow_routes (
                               workflow, workflow_version, step, model,
                               provider, note, ts
                           ) VALUES (?, ?, ?, ?, ?, ?, ?)""",
                        (
                            workflow_route.workflow,
                            workflow_route.workflow_version,
                            workflow_route.step or "",
                            workflow_route.model,
                            workflow_route.provider,
                            workflow_route.note,
                            activated,
                        ),
                    )

                self._conn.execute("DELETE FROM workflow_definitions")
                for definition in config.workflows:
                    self._conn.execute(
                        """INSERT INTO workflow_definitions (
                               workflow, workflow_version, definition_json
                           ) VALUES (?, ?, ?)""",
                        (
                            definition.workflow,
                            definition.workflow_version,
                            json.dumps(
                                [
                                    {
                                        "name": step.name,
                                        "predecessors": step.predecessors,
                                        "fan_out": step.fan_out,
                                        "retry": step.retry,
                                        "condition": step.condition,
                                    }
                                    for step in definition.steps
                                ],
                                sort_keys=True,
                            ),
                        ),
                    )
                self._conn.execute(
                    """INSERT OR REPLACE INTO control_config_state (
                           singleton, revision, source_path, document_sha256,
                           activated_at
                       ) VALUES (1, ?, ?, ?, ?)""",
                    (
                        revision.revision,
                        revision.source_path,
                        revision.document_sha256,
                        revision.activated_at,
                    ),
                )
                self._conn.commit()
            except Exception:
                self._conn.rollback()
                raise

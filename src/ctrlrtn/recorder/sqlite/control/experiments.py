"""SQLite experiment, shadow, routing, and control-config persistence."""

from __future__ import annotations

import sqlite3

from ctrlrtn.policy.experiment import RUNNING, STOPPED, Experiment
from ctrlrtn.policy.route import Route
from ctrlrtn.policy.shadow import RUNNING as SHADOW_RUNNING
from ctrlrtn.policy.shadow import STOPPED as SHADOW_STOPPED
from ctrlrtn.policy.shadow import ShadowExperiment, ShadowStats

from ..capability import SqliteCapability
from ..queries import (
    _INSERT_EXPERIMENT,
    _STOP_EXPERIMENT,
    _UPSERT_ROUTE,
    _row_to_experiment,
)


class ExperimentControlSqliteMixin(SqliteCapability):
    """Apply control-plane mutations and serve their read models."""

    def create_experiment(self, experiment: Experiment) -> None:
        with self._lock:
            try:
                self._conn.execute(
                    _INSERT_EXPERIMENT,
                    (
                        experiment.experiment_id,
                        experiment.created_epoch,
                        experiment.use_case_key,
                        experiment.candidate_model,
                        experiment.split_pct,
                        experiment.status,
                        experiment.max_calls_per_task,
                        experiment.candidate_provider,
                        experiment.workflow,
                        experiment.workflow_version,
                        experiment.step,
                    ),
                )
            except sqlite3.IntegrityError as exc:
                # Safe: every writer commits before releasing _lock, so the
                # failed INSERT is the only statement in the open transaction.
                self._conn.rollback()
                if "shadow experiment" in str(exc):
                    raise ValueError(
                        "a running shadow already exists for use-case "
                        f"{experiment.use_case_key!r}"
                    ) from exc
                # SQLite names the offending column: the partial unique index
                # reports "...experiments.use_case_key"; the PK reports
                # "...experiments.experiment_id".
                if "use_case_key" in str(exc):
                    raise ValueError(
                        "a running experiment already exists for use-case "
                        f"{experiment.use_case_key!r}"
                    ) from exc
                raise ValueError(  # PRIMARY KEY: a duplicate experiment_id
                    f"experiment {experiment.experiment_id!r} already exists"
                ) from exc
            self._conn.commit()

    def create_shadow_experiment(self, experiment: ShadowExperiment) -> None:
        with self._lock:
            try:
                self._conn.execute(
                    """INSERT INTO shadow_experiments (
                           shadow_id, ts, use_case_key, candidate_model,
                           sample_pct, status, candidate_provider,
                           workflow, workflow_version, step
                       ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    (
                        experiment.shadow_id,
                        experiment.created_epoch,
                        experiment.use_case_key,
                        experiment.candidate_model,
                        experiment.sample_pct,
                        experiment.status,
                        experiment.candidate_provider,
                        experiment.workflow,
                        experiment.workflow_version,
                        experiment.step,
                    ),
                )
                self._conn.execute(
                    "INSERT INTO shadow_stats (shadow_id) VALUES (?)",
                    (experiment.shadow_id,),
                )
                self._conn.commit()
            except sqlite3.IntegrityError as exc:
                self._conn.rollback()
                if "live experiment" in str(exc):
                    raise ValueError(
                        "a live experiment already exists for this use-case"
                    ) from exc
                raise ValueError(
                    "a running shadow already exists for this use-case, or "
                    "the shadow id was already used"
                ) from exc

    def stop_shadow_experiment(self, shadow_id: str) -> bool:
        with self._lock:
            cur = self._conn.execute(
                """UPDATE shadow_experiments SET status = ?
                   WHERE shadow_id = ? AND status = ?""",
                (SHADOW_STOPPED, shadow_id, SHADOW_RUNNING),
            )
            self._conn.commit()
        return cur.rowcount > 0

    def shadow_experiments(self, limit: int = 50) -> list[ShadowExperiment]:
        scope_columns = self._has_column("shadow_experiments", "workflow")
        scope_select = (
            "workflow, workflow_version, step"
            if scope_columns
            else "NULL AS workflow, NULL AS workflow_version, NULL AS step"
        )
        with self._lock:
            try:
                rows = self._conn.execute(
                    f"""SELECT shadow_id, ts, use_case_key, candidate_model,
                              sample_pct, status, candidate_provider,
                              {scope_select}
                       FROM shadow_experiments ORDER BY rowid DESC LIMIT ?""",
                    (limit,),
                ).fetchall()
            except sqlite3.OperationalError as exc:
                if "no such table" in str(exc):
                    return []
                raise
        return [
            ShadowExperiment(
                use_case_key=row[2],
                candidate_model=row[3],
                sample_pct=row[4],
                shadow_id=row[0],
                candidate_provider=row[6],
                status=row[5],
                created_epoch=row[1],
                workflow=row[7],
                workflow_version=row[8],
                step=row[9],
            )
            for row in rows
        ]

    def running_shadow_experiments(self) -> dict[str, ShadowExperiment]:
        return {
            row.use_case_key: row
            for row in self.shadow_experiments(limit=10000)
            if row.is_running
        }

    def increment_shadow_stats(
        self,
        shadow_id: str,
        *,
        submitted: int = 0,
        completed: int = 0,
        failed: int = 0,
        dropped: int = 0,
    ) -> None:
        with self._lock:
            self._conn.execute(
                """UPDATE shadow_stats SET submitted = submitted + ?,
                       completed = completed + ?, failed = failed + ?,
                       dropped = dropped + ? WHERE shadow_id = ?""",
                (submitted, completed, failed, dropped, shadow_id),
            )
            self._conn.commit()

    def shadow_stats(self, shadow_id: str) -> ShadowStats | None:
        with self._lock:
            try:
                row = self._conn.execute(
                    """SELECT shadow_id, submitted, completed, failed, dropped
                       FROM shadow_stats WHERE shadow_id = ?""",
                    (shadow_id,),
                ).fetchone()
            except sqlite3.OperationalError as exc:
                if "no such table" in str(exc):
                    return None
                raise
        return ShadowStats(*row) if row else None

    def shadow_pairs(self, shadow_id: str, limit: int = 20) -> list[dict]:
        """Newest mirrored pairs, retaining one-sided rows as attrition."""
        with self._lock:
            rows = self._conn.execute(
                """SELECT shadow_pair_id,
                          MAX(CASE WHEN shadow_role = 'actual' THEN id END),
                          MAX(CASE WHEN shadow_role = 'candidate' THEN id END),
                          MAX(CASE WHEN shadow_role = 'actual'
                                   THEN status_code END),
                          MAX(CASE WHEN shadow_role = 'candidate'
                                   THEN status_code END),
                          MAX(ts)
                   FROM traces
                   WHERE shadow_experiment_id = ? AND shadow_pair_id IS NOT NULL
                   GROUP BY shadow_pair_id
                   ORDER BY MAX(ts) DESC LIMIT ?""",
                (shadow_id, limit),
            ).fetchall()
        return [
            {
                "pair_id": row[0],
                "actual_id": row[1],
                "candidate_id": row[2],
                "actual_status": row[3],
                "candidate_status": row[4],
                "ts": row[5],
            }
            for row in rows
        ]

    def stop_experiment(self, experiment_id: str) -> bool:
        with self._lock:
            cur = self._conn.execute(
                _STOP_EXPERIMENT, (STOPPED, experiment_id, RUNNING)
            )
            self._conn.commit()
            return cur.rowcount > 0

    def adopt_experiment(self, experiment_id: str, route: Route) -> bool:
        """Stop one experiment and install its candidate route atomically."""
        with self._lock:
            row = self._conn.execute(
                self._experiment_select() + " WHERE experiment_id = ?",
                (experiment_id,),
            ).fetchone()
            if row is None:
                return False
            experiment = _row_to_experiment(row)
            if (
                route.use_case_key != experiment.use_case_key
                or route.model != experiment.candidate_model
                or route.provider != experiment.candidate_provider
            ):
                raise ValueError("adoption route does not match the experiment")
            try:
                self._conn.execute(
                    _STOP_EXPERIMENT, (STOPPED, experiment_id, RUNNING)
                )
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
            except Exception:
                self._conn.rollback()
                raise
            return True

    def running_experiments(self) -> dict[str, Experiment]:
        with self._lock:
            rows = self._conn.execute(
                self._experiment_select() + " WHERE status = ?", (RUNNING,)
            ).fetchall()
        return {row[2]: _row_to_experiment(row) for row in rows}

    def experiments(self, limit: int = 50) -> list[Experiment]:
        with self._lock:
            # rowid DESC = insertion order (monotonic), deterministic under
            # LIMIT even when two experiments share a wall-clock created_epoch.
            rows = self._conn.execute(
                self._experiment_select() + " ORDER BY rowid DESC LIMIT ?",
                (limit,),
            ).fetchall()
        return [_row_to_experiment(row) for row in rows]

    def experiment(self, experiment_id: str) -> Experiment | None:
        """One experiment by id, however old (no LIMIT-window miss)."""
        with self._lock:
            row = self._conn.execute(
                self._experiment_select() + " WHERE experiment_id = ?",
                (experiment_id,),
            ).fetchone()
        return _row_to_experiment(row) if row is not None else None

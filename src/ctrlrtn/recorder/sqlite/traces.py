"""SQLite trace and outcome writes plus trace re-enrichment."""

from __future__ import annotations

import json
from collections.abc import Callable

from ctrlrtn.recorder.models import Outcome
from ctrlrtn.recorder.trace import Trace

from .queries import (
    _INSERT,
    _INSERT_OUTCOME,
    _REENRICH_SELECT,
    _REENRICH_UPDATE,
)


class TraceSqliteMixin:
    """Persist raw trace facts without owning connection lifecycle."""

    def _insert_outcome(self, outcome: Outcome) -> None:
        with self._lock:
            self._conn.execute(
                _INSERT_OUTCOME,
                (
                    outcome.ts,
                    outcome.task_id,
                    outcome.success,
                    outcome.score,
                ),
            )
            self._conn.commit()

    def _insert(self, trace: Trace) -> None:
        with self._lock:
            self._conn.execute(
                _INSERT,
                (
                    trace.ts,
                    trace.method,
                    trace.path,
                    trace.query,
                    trace.status_code,
                    trace.latency_ms,
                    trace.model,
                    trace.input_tokens,
                    trace.output_tokens,
                    trace.cache_read_tokens,
                    trace.cache_write_tokens,
                    trace.cost_usd,
                    trace.use_case_key,
                    trace.task_id,
                    trace.session_id,
                    trace.experiment_id,
                    trace.arm,
                    trace.served_model,
                    trace.terminal_reason,
                    trace.provider,
                    trace.provider_free,
                    trace.budget_fallback,
                    trace.shadow_experiment_id,
                    trace.shadow_pair_id,
                    trace.shadow_role,
                    trace.workflow,
                    trace.workflow_version,
                    trace.step,
                    trace.step_run_id,
                    trace.parent_step_run_id,
                    json.dumps(trace.dependency_step_run_ids),
                    trace.step_attempt,
                    trace.workflow_identity_error,
                    trace.route_rule_scope,
                    trace.route_rule_key,
                    trace.control_revision,
                    json.dumps(trace.request_headers),
                    trace.request_body,
                    json.dumps(trace.response_headers),
                    trace.response_body,
                ),
            )
            self._conn.commit()

    def reenrich(self, enrich: Callable[[Trace], None]) -> int:
        """Recompute the derived columns (identities, model, tokens, cost) for
        every stored row from its retained raw bytes, using the current
        enrichment logic. Returns the number of rows updated. Enrichment is a
        pure function of the stored request/response, so this is idempotent
        WITHIN a code version; do it with the gateway stopped so it does not
        contend on the write lock.

        Across a fingerprint change (e.g. new volatile-span normalization) this
        is instead a deliberate one-time RE-KEY of history: use_case_key values
        change, healing per-day-forked ``fp:`` aggregates. It rewrites traces
        only — it does NOT reconcile the ``experiments`` table, so a running
        experiment still frozen on an old ``fp:`` key must be stopped and
        recreated on the new key separately."""
        with self._lock:
            rows = self._conn.execute(_REENRICH_SELECT).fetchall()
            for row in rows:
                trace = Trace(
                    method=row[2],
                    path=row[3],
                    query=row[4],
                    request_headers=json.loads(row[7]),
                    request_body=row[8],
                    status_code=row[5],
                    response_headers=json.loads(row[9]),
                    response_body=row[10],
                    latency_ms=row[6],
                    # served_model is an immutable serving fact, but enrich reads
                    # it to price the served (not requested) model on re-run.
                    served_model=row[11],
                    provider=row[12],
                    provider_free=bool(row[13]),
                    ts=row[1],
                )
                enrich(trace)
                self._conn.execute(
                    _REENRICH_UPDATE,
                    (
                        trace.use_case_key,
                        trace.model,
                        trace.input_tokens,
                        trace.output_tokens,
                        trace.cache_read_tokens,
                        trace.cache_write_tokens,
                        trace.cost_usd,
                        trace.task_id,
                        trace.session_id,
                        trace.workflow,
                        trace.workflow_version,
                        trace.step,
                        trace.step_run_id,
                        trace.parent_step_run_id,
                        json.dumps(trace.dependency_step_run_ids),
                        trace.step_attempt,
                        trace.workflow_identity_error,
                        row[0],
                    ),
                )
            self._conn.commit()
        return len(rows)

    def count(self) -> int:
        with self._lock:
            row = self._conn.execute("SELECT COUNT(*) FROM traces").fetchone()
        return int(row[0])

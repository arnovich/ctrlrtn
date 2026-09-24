"""SQLite trace, task, session, spend, and experiment reporting queries."""

from __future__ import annotations

import json
import time

from ctrlrtn.policy.scope import ExperimentScope
from ctrlrtn.recorder.models import UNKEYED as _UNKEYED
from ctrlrtn.recorder.models import ModelRanking

from ..queries import (
    _BUCKET_SERIES,
    _GET,
    _MODEL_RANKINGS,
    _RECENT,
    _REQUESTS_FOR_USE_CASE,
    _TASK_USE_CASE_COUNTS,
    _where,
)


class RequestReportingSqliteMixin:
    """Project recorded requests, datasets, models, and bucket series."""

    def investigation_calls(
        self,
        *,
        roles: tuple[str, ...] | None = None,
        since: float | None = None,
        cutoff: float | None = None,
        limit: int = 10_001,
    ) -> list[dict]:
        """Read bounded metrics and outcome evidence in one SQL snapshot.

        Return the latest eligible calls in chronological order. No request
        bodies or headers are loaded. Outcomes obey the same observation
        cutoff; contradictory boolean reports stay visible to the caller.
        """
        if roles == ():
            return []
        if limit < 1:
            raise ValueError("investigation limit must be positive")
        cutoff = time.time() if cutoff is None else cutoff
        params: list = [cutoff, cutoff]
        clauses = ["t.ts <= ?"]
        if since is not None:
            clauses.append("t.ts >= ?")
            params.append(since)
        if roles is not None:
            clauses.append(
                "t.use_case_key IN (" + ",".join("?" for _ in roles) + ")"
            )
            params.extend(roles)
        params.append(limit)
        query = """
            WITH reports AS (
                SELECT task_id, COUNT(*) AS outcome_reports,
                       MIN(success) AS minimum_success,
                       MAX(success) AS maximum_success
                FROM outcomes WHERE ts <= ? GROUP BY task_id
            )
            SELECT t.id, t.ts, t.use_case_key, t.task_id,
                   COALESCE(t.served_model, t.model) AS model,
                   t.status_code, t.latency_ms, t.input_tokens,
                   t.output_tokens, t.cost_usd, t.terminal_reason,
                   COALESCE(r.outcome_reports, 0) AS outcome_reports,
                   r.minimum_success, r.maximum_success
            FROM traces t LEFT JOIN reports r ON r.task_id = t.task_id
            WHERE {where}
            ORDER BY t.ts DESC, t.id DESC LIMIT ?
        """.format(where=" AND ".join(clauses))
        with self._lock:
            cursor = self._conn.execute(query, tuple(params))
            names = [column[0] for column in cursor.description]
            rows = cursor.fetchall()
        return [dict(zip(names, row, strict=True)) for row in reversed(rows)]

    def requests_for_use_case(
        self,
        use_case_key: str,
        limit: int = 50,
        *,
        workflow: str | None = None,
        workflow_version: str | None = None,
        step: str | None = None,
    ) -> list[dict]:
        """Successful recorded requests for one use-case (newest first), as
        ``{request_body, task_id}`` — the inputs to shadow-replay. ``task_id`` is
        passed through as-is (NULL -> None): replay treats None as an independent
        unit, so it must NOT be COALESCEd to a sentinel here (that would fuse all
        untasked samples into one cluster and wreck the NI confidence interval).
        """
        scope = ExperimentScope(use_case_key, workflow, workflow_version, step)
        clause = ""
        if scope.is_workflow_scoped:
            clause = "AND workflow = ? AND workflow_version = ?"
            if scope.is_step_scoped:
                clause += " AND step = ?"
        params = [_UNKEYED, use_case_key]
        if scope.is_workflow_scoped:
            params.extend([workflow, workflow_version])
            if scope.is_step_scoped:
                params.append(step)
        params.append(limit)
        with self._lock:
            rows = self._conn.execute(
                _REQUESTS_FOR_USE_CASE.format(scope=clause), tuple(params)
            ).fetchall()
        return [
            {"id": r[0], "request_body": r[1], "task_id": r[2]} for r in rows
        ]

    def requests_by_ids(self, trace_ids: list[int]) -> list[dict]:
        """Replay inputs in the caller's frozen order.

        Missing rows are an error: silently shrinking a queued evaluation after
        retention/pruning would change its statistical sample.
        """
        rows = []
        with self._lock:
            for trace_id in trace_ids:
                row = self._conn.execute(
                    "SELECT id, request_body, task_id FROM traces WHERE id = ?",
                    (trace_id,),
                ).fetchone()
                if row is None:
                    raise ValueError(
                        f"replay input trace {trace_id} is no longer available"
                    )
                if row[1] is None:
                    raise ValueError(
                        f"replay input trace {trace_id} payload was pruned"
                    )
                rows.append(
                    {"id": row[0], "request_body": row[1], "task_id": row[2]}
                )
        return rows

    def dataset_rows_for_use_case(
        self,
        use_case_key: str,
        limit: int = 1000,
        *,
        workflow: str | None = None,
        workflow_version: str | None = None,
        step: str | None = None,
    ) -> list[dict]:
        """Frozen replay ordering plus response bytes for lineage manifests."""
        selected = self.requests_for_use_case(
            use_case_key,
            limit,
            workflow=workflow,
            workflow_version=workflow_version,
            step=step,
        )
        rows = []
        with self._lock:
            for item in selected:
                row = self._conn.execute(
                    "SELECT id, task_id, request_body, response_body "
                    "FROM traces WHERE id = ?",
                    (item["id"],),
                ).fetchone()
                if row is None:
                    raise ValueError(f"dataset trace {item['id']} disappeared")
                rows.append(
                    {
                        "id": row[0],
                        "task_id": row[1],
                        "request_body": row[2],
                        "response_body": row[3],
                    }
                )
        return rows

    def dataset_rows_by_ids(self, trace_ids: list[int]) -> list[dict]:
        """Read exact manifest payload bindings in caller order."""
        rows = []
        with self._lock:
            for trace_id in trace_ids:
                row = self._conn.execute(
                    "SELECT id, task_id, request_body, response_body "
                    "FROM traces WHERE id = ?",
                    (trace_id,),
                ).fetchone()
                if row is not None:
                    rows.append(
                        {
                            "id": row[0],
                            "task_id": row[1],
                            "request_body": row[2],
                            "response_body": row[3],
                        }
                    )
        return rows

    def task_use_case_counts(self, window: int = 2000) -> list[dict]:
        """Call counts per ``(task_id, use_case_key)`` (NULLs as None) over the
        most recent ``window`` completion calls — the raw input to the
        header-propagation gate."""
        with self._lock:
            rows = self._conn.execute(
                _TASK_USE_CASE_COUNTS, (window,)
            ).fetchall()
        return [
            {"task_id": r[0], "use_case_key": r[1], "calls": r[2]} for r in rows
        ]

    def earliest_trace_ts(self) -> float | None:
        """When recording started, or None on an empty store — the left edge
        of an "all history" graph window. A MIN over the ts index, not a
        scan."""
        with self._lock:
            row = self._conn.execute("SELECT MIN(ts) FROM traces").fetchone()
        return None if row is None or row[0] is None else float(row[0])

    def recent(self, limit: int = 20, *, offset: int = 0) -> list[dict]:
        """Most-recent calls (newest first) — one summary dict each.
        ``offset`` skips that many, for paging back through the feed."""
        with self._lock:
            rows = self._conn.execute(_RECENT, (limit, offset)).fetchall()
        return [
            {
                "id": r[0],
                "ts": r[1],
                "use_case_key": r[2],
                "model": r[3],
                "status_code": r[4],
                "latency_ms": r[5],
                "input_tokens": r[6],
                "output_tokens": r[7],
                "cost_usd": r[8],
                "method": r[9],
                "path": r[10],
                "provider": r[11],
            }
            for r in rows
        ]

    def model_rankings(
        self, *, since: float | None = None
    ) -> list[ModelRanking]:
        """Spend and volume grouped by the model actually served (see
        ``_MODEL_RANKINGS``). Sorted by spend, unpriced/unknown last.
        ``since`` (a Unix timestamp) limits it to a trailing window; the
        default spans all recorded history."""
        params: list = ["(unknown)"]
        clauses = []
        if since is not None:
            clauses.append("ts >= ?")
            params.append(since)
        with self._lock:
            rows = self._conn.execute(
                _MODEL_RANKINGS.format(where=_where(clauses)), tuple(params)
            ).fetchall()
        return [
            ModelRanking(
                model=row[0],
                calls=int(row[1]),
                input_tokens=int(row[2]),
                output_tokens=int(row[3]),
                avg_latency_ms=float(row[4]) if row[4] is not None else 0.0,
                cost_usd=float(row[5]) if row[5] is not None else 0.0,
                cache_read_tokens=int(row[6]),
                cache_write_tokens=int(row[7]),
            )
            for row in rows
        ]

    def bucket_series(
        self,
        *,
        seconds: int = 60,
        buckets: int = 30,
        now: float | None = None,
    ) -> list[dict]:
        """Call counts, cost, average latency and token volume bucketed by
        ``seconds`` over the trailing window, oldest first and zero-filled (a
        quiet bucket is a 0-bar, not a missing point), so the console graphs
        get exactly ``buckets`` points on a real time axis. ``latency_ms`` is
        the bucket's average; ``tokens`` is input+output summed (cache splits
        excluded). ``now`` is injectable for tests."""
        now = time.time() if now is None else now
        last = int(now // seconds)  # the (partial) current bucket
        first = last - buckets + 1
        with self._lock:
            rows = self._conn.execute(
                _BUCKET_SERIES, (seconds, first * float(seconds))
            ).fetchall()
        empty = (0, 0.0, 0.0, 0)
        by_bucket = {r[0]: (r[1], r[2], r[3], r[4]) for r in rows}
        return [
            {
                "ts": b * float(seconds),
                "calls": by_bucket.get(b, empty)[0],
                "cost_usd": by_bucket.get(b, empty)[1],
                "latency_ms": by_bucket.get(b, empty)[2],
                "tokens": by_bucket.get(b, empty)[3],
            }
            for b in range(first, last + 1)
        ]

    def get(self, trace_id: int) -> dict | None:
        """Full detail for one call, or None. Bodies are raw bytes."""
        with self._lock:
            row = self._conn.execute(_GET, (trace_id,)).fetchone()
        if row is None:
            return None
        return {
            "id": row[0],
            "ts": row[1],
            "method": row[2],
            "path": row[3],
            "query": row[4],
            "status_code": row[5],
            "latency_ms": row[6],
            "model": row[7],
            "input_tokens": row[8],
            "output_tokens": row[9],
            "use_case_key": row[10],
            "request_headers": json.loads(row[11]),
            "request_body": row[12],
            "response_headers": json.loads(row[13]),
            "response_body": row[14],
            "cache_read_tokens": row[15],
            "cache_write_tokens": row[16],
            "cost_usd": row[17],
            "task_id": row[18],
            "experiment_id": row[19],
            "arm": row[20],
            "served_model": row[21],
            "terminal_reason": row[22],
            "provider": row[23],
            "provider_free": bool(row[24]),
            "session_id": row[25],
            "budget_fallback": bool(row[26]),
            "shadow_experiment_id": row[27],
            "shadow_pair_id": row[28],
            "shadow_role": row[29],
            "workflow": row[30],
            "workflow_version": row[31],
            "step": row[32],
            "step_run_id": row[33],
            "parent_step_run_id": row[34],
            "dependency_step_run_ids": tuple(json.loads(row[35] or "[]")),
            "step_attempt": row[36],
            "workflow_identity_error": row[37],
            "route_rule_scope": row[38],
            "route_rule_key": row[39],
            "control_revision": row[40],
        }

"""Declarative SQL and row converters shared by SQLite capabilities."""

from __future__ import annotations

import json

from ctrlrtn.jobs import Job
from ctrlrtn.policy.experiment import Experiment
from ctrlrtn.policy.fallback import ApprovedFallback
from ctrlrtn.policy.route import Route

_INSERT_WORKFLOW_EVENT = """
INSERT OR IGNORE INTO workflow_events (
    event_id, ts, task_id, workflow, workflow_version, step, step_run_id,
    parent_step_run_id, dependency_step_run_ids, attempt, status, success,
    score, error_code
) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
"""


_INSERT_TOOL_EVENT = """
INSERT OR IGNORE INTO tool_operation_events (
    event_id, ts, task_id, workflow, workflow_version, step, step_run_id,
    parent_step_run_id, dependency_step_run_ids, step_attempt, operation,
    operation_id, attempt_id, attempt, effect, status, success, error_code,
    latency_ms, cost_usd
) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
"""


_INSERT_OUTCOME = """
INSERT INTO outcomes (ts, task_id, success, score) VALUES (?, ?, ?, ?)
"""


# The "at most one running experiment per use-case" invariant, enforced by the
# DB (a second running insert raises IntegrityError, caught in create).

_INSERT_EXPERIMENT = """
INSERT INTO experiments (
    experiment_id, ts, use_case_key, candidate_model, split_pct, status,
    max_calls_per_task, max_cost_usd_per_task, candidate_provider,
    workflow, workflow_version, step
) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
"""

_STOP_EXPERIMENT = """
UPDATE experiments SET status = ?
WHERE experiment_id = ? AND status = ?
"""

_SELECT_EXPERIMENTS = """
SELECT experiment_id, ts, use_case_key, candidate_model, split_pct, status,
       max_calls_per_task, max_cost_usd_per_task, candidate_provider,
       workflow, workflow_version, step
FROM experiments
"""

# Persistent per-use-case model overrides ("routes"): at most one per use-case
# (PK), replaced wholesale on re-set.


_SELECT_WORKFLOW_ROUTES = """
SELECT workflow, workflow_version, step, model, provider, note, ts
FROM workflow_routes ORDER BY ts DESC, workflow, workflow_version, step
"""


_UPSERT_ROUTE = """
INSERT OR REPLACE INTO routes (
    use_case_key, model, previous_model, note, ts, provider
) VALUES (?, ?, ?, ?, ?, ?)
"""

_SELECT_ROUTES = """
SELECT use_case_key, model, previous_model, note, ts, provider FROM routes
ORDER BY ts DESC
"""


# Everything that asks "what happened since T" — the daily budget rollup, the
# graph buckets, the windowed use-case/model/task tables — filters bare `ts`,
# which no other index leads on (the discovery ones lead on task_id). Without
# this each of those is a full table scan on every console refresh.


_UPSERT_FALLBACK = """
INSERT OR REPLACE INTO approved_fallbacks (
    use_case_key, model, baseline_model, evidence_created, approved_at, provider
) VALUES (?, ?, ?, ?, ?, ?)
"""

_SELECT_FALLBACKS = """
SELECT use_case_key, model, baseline_model, evidence_created, provider,
       approved_at
FROM approved_fallbacks ORDER BY approved_at DESC
"""

# Post-switch monitoring: the use-case's traffic since a moment in time
# (token mix + spend), for pricing "what it would have cost on the old model".
_USE_CASE_USAGE_SINCE = """
SELECT COUNT(*),
       COALESCE(SUM(input_tokens), 0),
       COALESCE(SUM(output_tokens), 0),
       COALESCE(SUM(cache_read_tokens), 0),
       COALESCE(SUM(cache_write_tokens), 0),
       COALESCE(SUM(cost_usd), 0.0)
FROM traces WHERE COALESCE(use_case_key, ?) = ? AND ts >= ?
"""

# The swapped-only variant: calls the ROUTE itself served (served_model set by
# the route, no experiment involved). The savings counterfactual prices only
# these — a client that natively requests the routed model (no-op, served_model
# NULL) or an experiment's candidate arm must not inflate "saved".
_USE_CASE_USAGE_SINCE_SWAPPED = (
    _USE_CASE_USAGE_SINCE.rstrip()
    + " AND served_model = ? AND experiment_id IS NULL\n"
)

# Per-model breakdown WITHIN one use-case (which models served it, at what
# volume/spend) — the served model when the router swapped, else the requested.
_USE_CASE_MODEL_BREAKDOWN = """
SELECT COALESCE(provider, ?) AS provider,
       COALESCE(served_model, model, ?) AS served,
       COUNT(*),
       COALESCE(SUM(cost_usd), 0.0)
FROM traces WHERE COALESCE(use_case_key, ?) = ?
GROUP BY provider, served
ORDER BY COALESCE(SUM(cost_usd), 0.0) DESC, COUNT(*) DESC, served ASC
"""

_SPEND_SINCE = "SELECT COALESCE(SUM(cost_usd), 0.0) FROM traces WHERE ts >= ?"

_SPEND_BREAKDOWN_SINCE = """
SELECT use_case_key, COALESCE(SUM(cost_usd), 0.0)
FROM traces WHERE ts >= ?
GROUP BY use_case_key
"""

_SESSION_SPEND_STATE = """
SELECT session_id, COALESCE(SUM(cost_usd), 0.0),
       SUM(CASE WHEN cost_usd IS NULL AND terminal_reason IS NULL
                     AND provider_free = 0 THEN 1 ELSE 0 END)
FROM traces WHERE session_id IS NOT NULL
GROUP BY session_id
"""

_PRICING_IDENTITIES_SINCE = """
SELECT COALESCE(served_model, model), provider_free
FROM traces WHERE ts >= ? AND terminal_reason IS NULL
"""

_TERMINAL_COUNTS_SINCE = """
SELECT terminal_reason, COUNT(*)
FROM traces
WHERE ts >= ? AND terminal_reason IS NOT NULL
GROUP BY terminal_reason
"""

_FALLBACK_CALLS_SINCE = """
SELECT COUNT(*) FROM traces
WHERE ts >= ? AND budget_fallback = 1 AND terminal_reason IS NULL
"""


def _where(clauses: list[str]) -> str:
    """The ``{where}`` slot of an aggregate query: a WHERE joining the given
    conditions, or nothing at all when there are none."""
    return f"WHERE {' AND '.join(clauses)}" if clauses else ""


def _row_to_route(row: tuple) -> Route:
    return Route(
        use_case_key=row[0],
        model=row[1],
        previous_model=row[2],
        note=row[3],
        ts=row[4],
        provider=row[5],
    )


def _same_route(left: Route, right: Route) -> bool:
    return (
        left.use_case_key,
        left.model,
        left.previous_model,
        left.note,
        left.provider,
    ) == (
        right.use_case_key,
        right.model,
        right.previous_model,
        right.note,
        right.provider,
    )


def _same_experiment(left: Experiment, right: Experiment) -> bool:
    return (
        left.experiment_id,
        left.use_case_key,
        left.candidate_model,
        left.candidate_provider,
        left.split_pct,
        left.max_calls_per_task,
        left.max_cost_usd_per_task,
        left.workflow,
        left.workflow_version,
        left.step,
    ) == (
        right.experiment_id,
        right.use_case_key,
        right.candidate_model,
        right.candidate_provider,
        right.split_pct,
        right.max_calls_per_task,
        right.max_cost_usd_per_task,
        right.workflow,
        right.workflow_version,
        right.step,
    )


def _row_to_job(row: tuple) -> Job:
    return Job(
        job_id=row[0],
        kind=row[1],
        config=json.loads(row[2]),
        status=row[3],
        created_at=row[4],
        updated_at=row[5],
        started_at=row[6],
        finished_at=row[7],
        worker_id=row[8],
        heartbeat_at=row[9],
        progress_current=row[10],
        progress_total=row[11],
        progress_message=row[12],
        result=json.loads(row[13]) if row[13] is not None else None,
        error=row[14],
        cancel_requested=bool(row[15]),
        attempt=row[16],
    )


_SELECT_JOBS = """
SELECT job_id, kind, config_json, status, created_at, updated_at, started_at,
       finished_at, worker_id, heartbeat_at, progress_current, progress_total,
       progress_message, result_json, error, cancel_requested, attempt
FROM jobs
"""


def _row_to_fallback(row: tuple) -> ApprovedFallback:
    return ApprovedFallback(
        use_case_key=row[0],
        model=row[1],
        baseline_model=row[2],
        evidence_created=row[3],
        provider=row[4],
        approved_at=row[5],
    )


def _row_to_experiment(row: tuple) -> Experiment:
    # from_stored: trust already-validated rows, so a later validation change
    # can't crash a load. Column order tracks _SELECT_EXPERIMENTS.
    return Experiment.from_stored(
        experiment_id=row[0],
        created_epoch=row[1],
        use_case_key=row[2],
        candidate_model=row[3],
        split_pct=row[4],
        status=row[5],
        max_calls_per_task=row[6],
        max_cost_usd_per_task=row[7],
        candidate_provider=row[8],
        workflow=row[9],
        workflow_version=row[10],
        step=row[11],
    )


# Column order here must match the value tuple in _insert() exactly (it differs
# from _GET's order, so the two can't be diffed against each other).
_INSERT = """
INSERT INTO traces (
    ts, method, path, query, status_code, latency_ms, model,
    input_tokens, output_tokens, cache_read_tokens, cache_write_tokens,
    cost_usd, use_case_key, task_id, session_id, experiment_id, arm, served_model,
    terminal_reason, provider, provider_free, budget_fallback,
    shadow_experiment_id, shadow_pair_id, shadow_role,
    workflow, workflow_version, step, step_run_id, parent_step_run_id,
    dependency_step_run_ids, step_attempt, workflow_identity_error,
    route_rule_scope, route_rule_key, control_revision,
    request_headers, request_body, response_headers, response_body
) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
"""

# baseline_only: candidate-arm traffic (a running/finished experiment's cheap
# calls) excluded, so "what does this use-case cost today" stays the incumbent.
_RANKINGS_ARM_FILTER = "arm IS NULL OR arm <> 'candidate'"

# {where} is an optional "WHERE ..." the caller composes (arm filter, trailing
# window, or both); empty for the all-history default.
_RANKINGS = """
SELECT
    COALESCE(use_case_key, ?) AS use_case,
    COUNT(*) AS calls,
    COALESCE(SUM(input_tokens), 0) AS input_tokens,
    COALESCE(SUM(output_tokens), 0) AS output_tokens,
    AVG(latency_ms) AS avg_latency_ms,
    COALESCE(SUM(cost_usd), 0.0) AS cost_usd,
    COALESCE(SUM(cache_read_tokens), 0) AS cache_read_tokens,
    COALESCE(SUM(cache_write_tokens), 0) AS cache_write_tokens
FROM traces
{where}
GROUP BY use_case
ORDER BY COALESCE(SUM(cost_usd), 0.0) DESC,
         (COALESCE(SUM(input_tokens), 0)
          + COALESCE(SUM(output_tokens), 0)) DESC,
         use_case ASC
"""

# The most recent *non-null* model per use-case, by (ts, id) so async insert
# reordering can't return an older model and a failed call without a model
# can't blank out a known one.
_USE_CASE_MODELS = """
SELECT use_case, model FROM (
    SELECT COALESCE(use_case_key, ?) AS use_case, model,
           ROW_NUMBER() OVER (
               PARTITION BY use_case_key ORDER BY ts DESC, id DESC
           ) AS rn
    FROM traces
    WHERE model IS NOT NULL
)
WHERE rn = 1
"""

_RECENT = """
SELECT id, ts, use_case_key, model, status_code, latency_ms,
       input_tokens, output_tokens, cost_usd, method, path, provider
FROM traces ORDER BY id DESC LIMIT ? OFFSET ?
"""

# Spend split by the model actually served (arm swaps visible): served_model
# when the router swapped, else the requested model, else "(unknown)".
_MODEL_RANKINGS = """
SELECT
    COALESCE(served_model, model, ?) AS served,
    COUNT(*) AS calls,
    COALESCE(SUM(input_tokens), 0),
    COALESCE(SUM(output_tokens), 0),
    AVG(latency_ms),
    COALESCE(SUM(cost_usd), 0.0),
    COALESCE(SUM(cache_read_tokens), 0),
    COALESCE(SUM(cache_write_tokens), 0)
FROM traces
{where}
GROUP BY served
ORDER BY COALESCE(SUM(cost_usd), 0.0) DESC, COUNT(*) DESC, served ASC
"""

# Time-bucketed traffic for the console graphs. SUM/AVG ignore NULLs
# (unenriched/error calls still count as calls, at zero cost/tokens).
_BUCKET_SERIES = """
SELECT CAST(ts / ? AS INTEGER) AS bucket,
       COUNT(*),
       COALESCE(SUM(cost_usd), 0),
       COALESCE(AVG(latency_ms), 0),
       COALESCE(SUM(COALESCE(input_tokens, 0) + COALESCE(output_tokens, 0)), 0)
FROM traces WHERE ts >= ? GROUP BY bucket
"""

# Successful recorded requests for one use-case — the inputs to replay. Only
# 2xx (a recorded 4xx/5xx is not a useful replay input). COALESCE mirrors the
# sibling queries so the "(unkeyed)" bucket is reachable.
#
# Sampled ROUND-ROBIN across tasks (every task's newest call first, then each
# task's second-newest, ...) rather than plain newest-N: the NI test
# cluster-resamples by task, so a sample bunched into the last few tasks caps
# its independent units at that handful and is underpowered regardless of how
# many CALLS it has. Untasked calls (NULL task_id) each form their own
# partition — they are independent units and must not fuse into one
# pseudo-task. Deterministic (id is monotonic).
_REQUESTS_FOR_USE_CASE = """
SELECT id, request_body, task_id, path FROM (
    SELECT request_body, task_id, id, path,
           ROW_NUMBER() OVER (
               PARTITION BY COALESCE(task_id, 'row:' || id)
               ORDER BY id DESC
           ) AS nth
    FROM traces
    WHERE COALESCE(use_case_key, ?) = ?
      {scope}
      AND status_code >= 200 AND status_code < 300
)
ORDER BY nth ASC, id DESC LIMIT ?
"""

# Calls per (task, use-case) for the header-propagation gate, over the most
# recent `window` *completion* calls only (a recent window keeps the verdict
# current instead of latching on old history; 2xx POSTs to the chat endpoints
# exclude GET /v1/models, OPTIONS preflights, count_tokens, and error retries
# that would distort the tagged fraction). task_id/use_case NULLs pass through
# as None so the report can tell untasked calls from a real task and not treat
# an unkeyed call as a sub-agent — do NOT COALESCE them away here.
_TASK_USE_CASE_COUNTS = """
SELECT task_id, use_case_key, COUNT(*) AS calls FROM (
    SELECT task_id, use_case_key FROM traces
    WHERE status_code >= 200 AND status_code < 300 AND method = 'POST'
      AND path IN ('/v1/messages', '/v1/chat/completions')
    ORDER BY id DESC LIMIT ?
)
GROUP BY task_id, use_case_key
"""

# Column order here must match the row[..] indices in get() exactly (it differs
# from _INSERT's order, so verify each independently).
_GET = """
SELECT id, ts, method, path, query, status_code, latency_ms, model,
       input_tokens, output_tokens, use_case_key, request_headers,
       request_body, response_headers, response_body,
       cache_read_tokens, cache_write_tokens, cost_usd, task_id,
       experiment_id, arm, served_model, terminal_reason, provider,
       provider_free, session_id, budget_fallback, shadow_experiment_id,
       shadow_pair_id, shadow_role, workflow, workflow_version, step,
       step_run_id, parent_step_run_id, dependency_step_run_ids, step_attempt,
       workflow_identity_error, route_rule_scope, route_rule_key,
       control_revision
FROM traces WHERE id = ?
"""

# Per task: spend/error rollup from the calls, plus the latest app-reported
# outcome (joined in; rn=1 picks the most recent BY ARRIVAL — id, not the
# non-monotonic wall-clock ts). Each task matches at most one outcome row, so
# COUNT(*) is not inflated and MAX just reads that single constant value per
# group (NULL when none). The rn=1 filter lives in ON (not WHERE) so tasks
# with no outcome survive the LEFT JOIN — moving it would break that.
_TASKS = """
SELECT COALESCE(t.task_id, ?) AS task,
       COUNT(*) AS calls,
       COALESCE(SUM(t.cost_usd), 0.0) AS cost_usd,
       COALESCE(SUM(t.input_tokens), 0) AS input_tokens,
       COALESCE(SUM(t.output_tokens), 0) AS output_tokens,
       COUNT(DISTINCT t.use_case_key) AS use_cases,
       SUM(CASE WHEN t.status_code >= 400 THEN 1 ELSE 0 END) AS errors,
       MAX(o.success) AS success,
       MAX(o.score) AS score
FROM traces t
LEFT JOIN (
    SELECT task_id, success, score,
           ROW_NUMBER() OVER (
               PARTITION BY task_id ORDER BY id DESC
           ) AS rn
    FROM outcomes
) o ON o.task_id = t.task_id AND o.rn = 1
{where}
GROUP BY task
ORDER BY cost_usd DESC, calls DESC, task ASC
LIMIT ?
"""

_SESSIONS = """
SELECT COALESCE(session_id, ?) AS session,
       COUNT(*) AS calls,
       COALESCE(SUM(cost_usd), 0.0) AS cost_usd,
       COALESCE(SUM(input_tokens), 0) AS input_tokens,
       COALESCE(SUM(output_tokens), 0) AS output_tokens,
       COUNT(DISTINCT use_case_key) AS use_cases,
       SUM(CASE WHEN status_code >= 400 THEN 1 ELSE 0 END) AS errors,
       SUM(CASE WHEN cost_usd IS NULL AND terminal_reason IS NULL
                     AND provider_free = 0 THEN 1 ELSE 0 END) AS unknown_cost_calls
FROM traces
GROUP BY session
ORDER BY cost_usd DESC, calls DESC, session ASC
LIMIT ?
"""

# Per-task aggregates for ONE experiment, feeding the live A/B tripwire
# (eval/tripwire.py). `arms` (distinct served arms) and `experiments` (distinct
# experiment_ids across ALL of the task's traces, not just this one) are the
# purity signals — a mixed-arm or cross-experiment task is contaminated. The
# ceiling count marks divergence terminals; the outcome is joined as in _TASKS.
_EXPERIMENT_TASK_ROWS = """
SELECT t.task_id AS task_id,
       COUNT(*) AS calls,
       COALESCE(SUM(t.cost_usd), 0.0) AS cost,
       COUNT(DISTINCT t.arm) AS arms,
       MAX(t.arm) AS arm,
       MAX(t.served_model) AS served_model,
       MAX(t.ts) AS last_ts,
       SUM(CASE WHEN t.terminal_reason = 'ceiling' THEN 1 ELSE 0 END)
           AS ceilings,
       (SELECT COUNT(DISTINCT x.experiment_id)
          FROM traces x WHERE x.task_id = t.task_id) AS experiments,
       MAX(o.success) AS success,
       MAX(o.score) AS score
FROM traces t
LEFT JOIN (
    SELECT task_id, success, score,
           ROW_NUMBER() OVER (
               PARTITION BY task_id ORDER BY id DESC
           ) AS rn
    FROM outcomes
) o ON o.task_id = t.task_id AND o.rn = 1
WHERE t.experiment_id = ? AND t.task_id IS NOT NULL
GROUP BY t.task_id
"""

_REENRICH_SELECT = """
SELECT id, ts, method, path, query, status_code, latency_ms,
       request_headers, request_body, response_headers, response_body,
       served_model, provider, provider_free
FROM traces
"""

_REENRICH_UPDATE = """
UPDATE traces
SET use_case_key = ?, model = ?, input_tokens = ?, output_tokens = ?,
    cache_read_tokens = ?, cache_write_tokens = ?, cost_usd = ?, task_id = ?,
    session_id = ?, workflow = ?, workflow_version = ?, step = ?, step_run_id = ?,
    parent_step_run_id = ?, dependency_step_run_ids = ?, step_attempt = ?,
    workflow_identity_error = ?
WHERE id = ?
"""

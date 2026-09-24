"""Read-model loading and detail rendering for the Textual console."""

from __future__ import annotations

import time

from ctrlrtn.analysis.report import (
    _fmt_cost,
    _fmt_outcome,
    render_trace,
    render_tripwire,
)
from ctrlrtn.cli.render import render_budget_status
from ctrlrtn.cli.tui.models import (
    _DEFAULT_GRAPH_WINDOW,
    _DEFAULT_TABLE_WINDOW,
    _PAGE_SIZE,
    _PAGED_TABLES,
    GraphWindow,
    TableWindow,
)
from ctrlrtn.control_config import ControlRevision
from ctrlrtn.eval.tripwire import run_tripwire
from ctrlrtn.jobs import Job
from ctrlrtn.policy.budget import BudgetPolicy, utc_day_start
from ctrlrtn.policy.experiment import Experiment
from ctrlrtn.policy.route import route_savings
from ctrlrtn.policy.shadow import ShadowExperiment
from ctrlrtn.recorder.models import (
    ModelRanking,
    TaskSummary,
    UseCaseRanking,
)
from ctrlrtn.recorder.sqlite.store import SqliteTraceStore
from ctrlrtn.workflow.discovery import render_workflow_discovery
from ctrlrtn.workflow.discovery_job import KIND as WORKFLOW_DISCOVERY_JOB_KIND
from ctrlrtn.workflow.discovery_job import (
    WorkflowDiscoveryJobError,
    render_workflow_discovery_selection,
    report_from_workflow_discovery_artifact,
)


def load_state(
    store: SqliteTraceStore,
    *,
    budget_policy: BudgetPolicy | None = None,
    kill_switch: bool = False,
    graph_window: GraphWindow = _DEFAULT_GRAPH_WINDOW,
    table_window: TableWindow = _DEFAULT_TABLE_WINDOW,
    pages: dict[str, int] | None = None,
    now: float | None = None,
) -> dict:
    """Load the complete read-only snapshot rendered by one refresh tick."""
    policy = budget_policy or BudgetPolicy()
    today = utc_day_start(now)
    clock = time.time() if now is None else now
    since = table_window.since(clock)
    grid = graph_window.resolve(
        store.earliest_trace_ts() if graph_window.is_all else None, clock
    )
    daily_total, daily_by_use_case = store.spend_breakdown_since(today)
    budget_args = {
        "kill_switch": kill_switch,
        "daily_total": daily_total,
        "daily_by_use_case": daily_by_use_case,
        "unknown_priced_calls": store.unpriced_calls_since(today),
        "blocked": store.terminal_counts_since(today),
        "fallback_calls": store.fallback_calls_since(today),
    }
    shadows = store.shadow_experiments(limit=200)
    offsets = {
        table: max(0, (pages or {}).get(table, 0)) * _PAGE_SIZE
        for table in _PAGED_TABLES
    }
    probe = _PAGE_SIZE + 1
    paged = {
        "jobs": store.jobs(limit=probe, offset=offsets["jobs"]),
        "workflows": store.workflow_runs(
            limit=probe, offset=offsets["workflows"]
        ),
        "calls": store.recent(limit=probe, offset=offsets["calls"]),
    }
    return {
        **{table: rows[:_PAGE_SIZE] for table, rows in paged.items()},
        "has_more": {
            table: len(rows) > _PAGE_SIZE for table, rows in paged.items()
        },
        "page_offsets": offsets,
        "discovery_job": store.latest_job(
            WORKFLOW_DISCOVERY_JOB_KIND, status="succeeded"
        ),
        "experiments": store.experiments(limit=200),
        "shadows": shadows,
        "shadow_stats": {
            row.shadow_id: store.shadow_stats(row.shadow_id) for row in shadows
        },
        "rankings": store.rankings(since=since),
        "tasks": store.tasks(limit=100, since=since),
        "models": store.model_rankings(since=since),
        "series": store.bucket_series(
            seconds=grid.bucket_seconds, buckets=grid.buckets
        ),
        "budget_status": render_budget_status(policy, **budget_args),
        "budget_summary": render_budget_status(
            policy, compact=True, **budget_args
        ),
        "control_revision": store.control_revision(),
    }


def routing_status(
    revision: ControlRevision | None, config_path: str, repo: str
) -> str:
    active = (
        "none activated"
        if revision is None
        else f"{revision.revision[:12]} · {revision.source_path}"
    )
    return f"Git routing: {active} · desired {config_path} · repo {repo} · g preview"


def shadow_status(shadows: list[ShadowExperiment], stats: dict) -> str:
    running = [row for row in shadows if row.is_running]
    submitted = sum(
        stats[row.shadow_id].submitted
        for row in running
        if stats.get(row.shadow_id)
    )
    completed = sum(
        stats[row.shadow_id].completed
        for row in running
        if stats.get(row.shadow_id)
    )
    failed = sum(
        stats[row.shadow_id].failed
        for row in running
        if stats.get(row.shadow_id)
    )
    dropped = sum(
        stats[row.shadow_id].dropped
        for row in running
        if stats.get(row.shadow_id)
    )
    return (
        f"Shadows: {len(running)} running · submitted {submitted} · "
        f"completed {completed} · failed {failed} · dropped {dropped}"
    )


def experiment_detail(store: SqliteTraceStore, experiment: Experiment) -> str:
    rows = store.experiment_task_rows(experiment.experiment_id)
    report = run_tripwire(rows, now=time.time())
    return render_tripwire(report, experiment)


def shadow_detail(store: SqliteTraceStore, experiment: ShadowExperiment) -> str:
    stats = store.shadow_stats(experiment.shadow_id)
    lines = [
        f"shadow: {experiment.shadow_id}",
        "",
        f"  use-case     {experiment.use_case_key}",
        f"  scope        {experiment.scope.label}",
        f"  candidate    {experiment.candidate_model}",
        f"  provider     {experiment.candidate_provider or 'baseline provider'}",
        f"  sample       {experiment.sample_pct}%",
        f"  state        {experiment.status}",
        "",
        f"  submitted    {stats.submitted if stats else 0}",
        f"  completed    {stats.completed if stats else 0}",
        f"  failed       {stats.failed if stats else 0}",
        f"  dropped      {stats.dropped if stats else 0}",
    ]
    pairs = store.shadow_pairs(experiment.shadow_id, limit=1)
    if not pairs:
        lines.extend(["", "No paired traces recorded yet."])
        return "\n".join(lines)
    pair = pairs[0]
    lines.extend(["", f"latest pair: {pair['pair_id']}"])
    for role, trace_id in (
        ("ACTUAL (served to user)", pair["actual_id"]),
        ("CANDIDATE (never served)", pair["candidate_id"]),
    ):
        lines.extend(["", f"--- {role} ---"])
        if trace_id is None:
            lines.append("missing trace (attrition)")
        else:
            trace = store.get(trace_id)
            lines.append(render_trace(trace) if trace else "trace was removed")
    return "\n".join(lines)


def job_detail(job: Job, invalidation: dict | None = None) -> str:
    total = str(job.progress_total) if job.progress_total is not None else "?"
    lines = [
        f"job: {job.job_id}",
        "",
        f"  kind         {job.kind}",
        f"  status       {job.status}",
        f"  progress     {job.progress_current} / {total}",
        f"  attempt      {job.attempt}",
        f"  worker       {job.worker_id or '-'}",
        f"  cancellation {'requested' if job.cancel_requested else '-'}",
    ]
    if job.progress_message:
        lines.append(f"  message      {job.progress_message}")
    if job.error:
        lines.extend(["", f"error: {job.error}"])
    if job.kind == WORKFLOW_DISCOVERY_JOB_KIND:
        lines.extend(
            [
                "",
                "frozen discovery:",
                f"  traces: {len(job.config.get('trace_ids', []))}",
                f"  input: {job.config.get('input_sha256', '-')}",
                f"  support: {job.config.get('min_support', '-')}",
                f"  similarity: {job.config.get('similarity', '-')}",
                "  scope: "
                + (
                    ", ".join(
                        f"{key}={value}"
                        for key, value in job.config.get("scope", {}).items()
                        if value is not None
                    )
                    or "all legacy traffic"
                ),
            ]
        )
        if job.result:
            try:
                report = report_from_workflow_discovery_artifact(job.result)
            except WorkflowDiscoveryJobError as exc:
                lines.extend(["", f"invalid discovery artifact: {exc}"])
            else:
                lines.extend(
                    [
                        "",
                        render_workflow_discovery_selection(
                            job.result.get("selection", {})
                        ),
                        "",
                        render_workflow_discovery(report),
                    ]
                )
        if invalidation:
            lines.extend(
                [
                    "",
                    "SOURCE INVALIDATED BY RETENTION",
                    f"  pruned traces: {invalidation['pruned_traces']}",
                    f"  invalidated at: {invalidation['invalidated_at']}",
                    "  artifact remains immutable but must not be treated as reproducible.",
                ]
            )
        return "\n".join(lines)
    if job.config:
        lines.extend(["", "config:"])
        lines.extend(f"  {key}: {value}" for key, value in job.config.items())
    if job.result:
        lines.extend(["", "result:"])
        lines.extend(f"  {key}: {value}" for key, value in job.result.items())
    return "\n".join(lines)


def usecase_detail(
    store: SqliteTraceStore,
    rankings: list[UseCaseRanking],
    experiments: list[Experiment],
    use_case: str,
) -> str:
    row = next((item for item in rankings if item.use_case == use_case), None)
    if row is None:
        return f"use-case {use_case} not found"
    experiment = next(
        (
            item
            for item in experiments
            if item.use_case_key == use_case and item.is_running
        ),
        None,
    )
    lines = [
        f"use-case: {use_case}",
        "",
        f"  calls        {row.calls}",
        f"  tokens in/out {row.input_tokens} / {row.output_tokens}",
        f"  avg latency  {row.avg_latency_ms:.0f} ms",
        f"  cost         ${row.cost_usd:.4f}",
        "",
        "  models served:",
    ]
    for part in store.use_case_model_breakdown(use_case):
        lines.append(
            f"    {part['provider'][:12]:<12} {part['model'][:28]:<28} "
            f"{part['calls']:>6} calls  {_fmt_cost(part['cost_usd'])}"
        )
    route = next(
        (item for item in store.routes() if item.use_case_key == use_case), None
    )
    lines.append("")
    if route is not None:
        since = time.strftime("%Y-%m-%d %H:%M", time.localtime(route.ts))
        was = f" (was {route.previous_model})" if route.previous_model else ""
        lines.append(f"  route:       -> {route.model}{was}  since {since}")
        usage = store.use_case_usage_since(
            use_case, route.ts, swapped_to=route.model
        )
        saved = route_savings(usage, route.previous_model)
        lines.append(
            f"    swapped: {usage['calls']} calls · spent "
            f"{_fmt_cost(usage['cost_usd'])} · saved "
            f"{_fmt_cost(saved) if saved is not None else '-'}"
        )
        if route.note:
            lines.append(f"    note: {route.note}")
        if experiment is not None:
            lines.append("    (dormant while the experiment below is running)")
    lines.append(
        f"  experiment:  {experiment.experiment_id}  (candidate "
        f"{experiment.candidate_model}, {experiment.split_pct}% split)"
        if experiment
        else "  experiment:  none running "
        "(start one with `ctrlrtn experiment start`)"
    )
    return "\n".join(lines)


def model_detail(models: list[ModelRanking], model: str) -> str:
    row = next((item for item in models if item.model == model), None)
    if row is None:
        return f"model {model} not found"
    total = sum(item.cost_usd for item in models)
    share = f"{row.cost_usd / total:.0%}" if total > 0 else "-"
    return "\n".join(
        [
            f"model: {model}",
            "",
            f"  calls        {row.calls}",
            f"  tokens in/out {row.input_tokens} / {row.output_tokens}",
            f"  cache r/w    {row.cache_read_tokens} / {row.cache_write_tokens}",
            f"  avg latency  {row.avg_latency_ms:.0f} ms",
            f"  cost         ${row.cost_usd:.4f}  ({share} of recorded spend)",
        ]
    )


def task_detail(tasks: list[TaskSummary], task_id: str) -> str:
    row = next((item for item in tasks if item.task_id == task_id), None)
    if row is None:
        return f"task {task_id} not found"
    return "\n".join(
        [
            f"task: {task_id}",
            "",
            f"  calls        {row.calls}",
            f"  use-cases    {row.use_cases}",
            f"  errors       {row.errors}",
            f"  tokens in/out {row.input_tokens} / {row.output_tokens}",
            f"  cost         ${row.cost_usd:.4f}",
            f"  outcome      {_fmt_outcome(row.success, row.score)}",
        ]
    )


def call_detail(store: SqliteTraceStore, call_id: str) -> str:
    trace = store.get(int(call_id))
    if trace is None:
        return f"call #{call_id} not found"
    return render_trace(trace)

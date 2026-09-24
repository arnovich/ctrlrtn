"""Connected metadata-only workflow step-run inspection."""

from __future__ import annotations


def render_workflow_step_detail(detail: dict) -> str:
    lines = [
        f"step run: {detail['step_run_id']}",
        f"  task          {detail['task_id']}",
        f"  identity      {detail['workflow']}@{detail['workflow_version']}/{detail['step']}",
        f"  attempt       {detail['attempt']}",
        "",
        "lifecycle:",
    ]
    events = detail["events"]
    if not events:
        lines.append("  no explicit lifecycle events")
    for event in events:
        outcome = []
        if event.success is not None:
            outcome.append(f"success={event.success}")
        if event.score is not None:
            outcome.append(f"score={event.score:g}")
        if event.error_code:
            outcome.append(f"error={event.error_code}")
        lines.append(
            f"  {event.status:<10} event={event.event_id} ts={event.ts:g} {' '.join(outcome)}".rstrip()
        )
    lines.extend(["", "traces (metadata only):"])
    traces = detail["traces"]
    if not traces:
        lines.append("  none")
    for trace in traces:
        experiment = (
            trace["experiment_id"] or trace["shadow_experiment_id"] or "-"
        )
        role = trace["arm"] or trace["shadow_role"] or "-"
        route = (
            f"{trace['route_rule_scope']}:{trace['route_rule_key']}"
            if trace["route_rule_scope"]
            else "pass-through/default"
        )
        lines.extend(
            [
                f"  trace #{trace['id']} status={trace['status_code']} provider={trace['provider'] or '-'} model={trace['model'] or '-'}",
                f"    cost=${trace['cost_usd']:.4f} latency={trace['latency_ms']:.0f}ms experiment={experiment} role={role}",
                f"    route={route} revision={trace['control_revision'] or '-'}",
            ]
        )
    lines.extend(["", "tool operations (explicit application facts):"])
    tool_events = detail.get("tool_events", [])
    if not tool_events:
        lines.append("  none")
    for event in tool_events:
        identity = event.identity
        lines.append(
            f"  {identity.operation} operation={identity.operation_id} "
            f"attempt={identity.attempt}:{identity.attempt_id} "
            f"effect={identity.effect} status={event.status}"
        )
        if event.status != "started":
            lines.append(
                f"    success={event.success} error={event.error_code or '-'} "
                f"latency={event.latency_ms or 0:.0f}ms "
                f"cost=${event.cost_usd or 0:.4f}"
            )
    lines.extend(["", "related scoped jobs:"])
    if not detail["jobs"]:
        lines.append("  none")
    for job in detail["jobs"]:
        lines.append(
            f"  {job.job_id} {job.kind} {job.status} progress={job.progress_current}/{job.progress_total or '?'}"
        )
    lines.extend(
        [
            "",
            "Trace bodies remain available only through the existing trace detail view.",
        ]
    )
    return "\n".join(lines)

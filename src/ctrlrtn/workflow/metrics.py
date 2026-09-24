"""Pure, explicit workflow-step attribution and aggregation."""

from __future__ import annotations

from dataclasses import dataclass

from ctrlrtn.workflow.identity import TERMINAL_STATUSES, WorkflowEvent


@dataclass(frozen=True)
class WorkflowStepMetric:
    workflow: str
    workflow_version: str
    step: str
    runs: int
    calls: int
    call_errors: int
    cost_usd: float
    input_tokens: int
    output_tokens: int
    avg_call_latency_ms: float
    avg_run_duration_ms: float | None
    completed: int
    failed: int
    cancelled: int
    skipped: int
    active: int
    inconsistent: int
    reported_outcomes: int
    successful_outcomes: int
    failed_outcomes: int
    avg_score: float | None

    @property
    def unreported_runs(self) -> int:
        return self.runs - self.reported_outcomes


def build_workflow_step_metrics(
    traces: list[dict], events: list[WorkflowEvent]
) -> list[WorkflowStepMetric]:
    """Aggregate only explicit step-run facts; task outcomes are not inputs."""
    runs: dict[tuple[str, str], dict] = {}

    def record(task_id: str, run_id: str) -> dict:
        return runs.setdefault(
            (task_id, run_id),
            {
                "identities": set(),
                "calls": 0,
                "errors": 0,
                "cost": 0.0,
                "input": 0,
                "output": 0,
                "latency": 0.0,
                "events": [],
            },
        )

    for trace in traces:
        task_id = trace.get("task_id")
        run_id = trace.get("step_run_id")
        identity = (
            trace.get("workflow"),
            trace.get("workflow_version"),
            trace.get("step"),
        )
        if not task_id or not run_id or not all(identity):
            continue
        row = record(task_id, run_id)
        row["identities"].add(identity)
        row["calls"] += 1
        row["errors"] += int((trace.get("status_code") or 0) >= 400)
        row["cost"] += trace.get("cost_usd") or 0.0
        row["input"] += trace.get("input_tokens") or 0
        row["output"] += trace.get("output_tokens") or 0
        row["latency"] += trace.get("latency_ms") or 0.0

    for event in events:
        event_identity = event.identity
        row = record(event_identity.task_id, event_identity.step_run_id)
        row["identities"].add(
            (
                event_identity.workflow,
                event_identity.workflow_version,
                event_identity.step,
            )
        )
        row["events"].append(event)

    groups: dict[tuple[str, str, str], dict] = {}
    for row in runs.values():
        if len(row["identities"]) != 1:
            continue  # no stable step can honestly own this run
        identity = next(iter(row["identities"]))
        group = groups.setdefault(
            identity,
            {
                "runs": 0,
                "calls": 0,
                "errors": 0,
                "cost": 0.0,
                "input": 0,
                "output": 0,
                "latency": 0.0,
                "durations": [],
                "statuses": dict.fromkeys(TERMINAL_STATUSES, 0),
                "active": 0,
                "inconsistent": 0,
                "reported": 0,
                "success": 0,
                "failure": 0,
                "scores": [],
            },
        )
        group["runs"] += 1
        for key in ("calls", "errors", "input", "output"):
            group[key] += row[key]
        group["cost"] += row["cost"]
        group["latency"] += row["latency"]

        terminal_statuses = {
            event.status
            for event in row["events"]
            if event.status in TERMINAL_STATUSES
        }
        if len(terminal_statuses) > 1:
            group["inconsistent"] += 1
            continue
        if not terminal_statuses:
            group["active"] += 1
            continue
        status = next(iter(terminal_statuses))
        group["statuses"][status] += 1
        terminal = next(
            event for event in reversed(row["events"]) if event.status == status
        )
        starts = [
            event.ts for event in row["events"] if event.status == "started"
        ]
        if starts and terminal.ts >= min(starts):
            group["durations"].append((terminal.ts - min(starts)) * 1000.0)
        if terminal.success is not None or terminal.score is not None:
            group["reported"] += 1
        if terminal.success is True:
            group["success"] += 1
        elif terminal.success is False:
            group["failure"] += 1
        if terminal.score is not None:
            group["scores"].append(float(terminal.score))

    result = []
    for (workflow, version, step), group in groups.items():
        durations = group["durations"]
        scores = group["scores"]
        result.append(
            WorkflowStepMetric(
                workflow=workflow,
                workflow_version=version,
                step=step,
                runs=group["runs"],
                calls=group["calls"],
                call_errors=group["errors"],
                cost_usd=group["cost"],
                input_tokens=group["input"],
                output_tokens=group["output"],
                avg_call_latency_ms=(
                    group["latency"] / group["calls"] if group["calls"] else 0.0
                ),
                avg_run_duration_ms=(
                    sum(durations) / len(durations) if durations else None
                ),
                completed=group["statuses"]["completed"],
                failed=group["statuses"]["failed"],
                cancelled=group["statuses"]["cancelled"],
                skipped=group["statuses"]["skipped"],
                active=group["active"],
                inconsistent=group["inconsistent"],
                reported_outcomes=group["reported"],
                successful_outcomes=group["success"],
                failed_outcomes=group["failure"],
                avg_score=sum(scores) / len(scores) if scores else None,
            )
        )
    return sorted(
        result,
        key=lambda row: (
            row.workflow,
            row.workflow_version,
            -row.cost_usd,
            row.step,
        ),
    )

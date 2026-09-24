"""Small in-memory store used by tests and local experiments."""

from __future__ import annotations

from ctrlrtn.workflow.identity import WorkflowEvent
from ctrlrtn.workflow.metrics import (
    WorkflowStepMetric,
    build_workflow_step_metrics,
)
from ctrlrtn.workflow.tool_operation import ToolOperationEvent

from .core import MemoryState


class WorkflowMemoryMixin(MemoryState):
    """Store and project workflow and tool-operation observations."""

    async def save_workflow_event(self, event: WorkflowEvent) -> None:
        self._workflow_events.setdefault(event.event_id, event)

    async def save_tool_operation_event(
        self, event: ToolOperationEvent
    ) -> None:
        self._tool_events.setdefault(event.event_id, event)

    def tool_operation_events(
        self, task_id: str | None = None
    ) -> list[ToolOperationEvent]:
        return [
            row
            for row in self._tool_events.values()
            if task_id is None
            or row.identity.workflow_identity.task_id == task_id
        ]

    def workflow_events(
        self, task_id: str | None = None
    ) -> list[WorkflowEvent]:
        rows = list(self._workflow_events.values())
        return [
            row
            for row in rows
            if task_id is None or row.identity.task_id == task_id
        ]

    def workflow_diagnostics(self) -> dict:
        errors: dict[str, int] = {}
        for trace in self.traces:
            if trace.workflow_identity_error:
                errors[trace.workflow_identity_error] = (
                    errors.get(trace.workflow_identity_error, 0) + 1
                )
        by_run: dict[str, list[WorkflowEvent]] = {}
        for event in self._workflow_events.values():
            by_run.setdefault(event.identity.step_run_id, []).append(event)
        terminal = {"completed", "failed", "cancelled", "skipped"}
        metrics = self.workflow_step_metrics()
        run_ids = set(by_run) | {
            trace.step_run_id for trace in self.traces if trace.step_run_id
        }
        return {
            "valid_traces": sum(
                trace.workflow is not None for trace in self.traces
            ),
            "invalid_traces": sum(errors.values()),
            "errors": errors,
            "reused_step_run_ids": sum(
                len(
                    {
                        (
                            event.identity.task_id,
                            event.identity.workflow,
                            event.identity.workflow_version,
                            event.identity.step,
                        )
                        for event in by_run.get(run_id, [])
                    }
                    | {
                        (
                            trace.task_id,
                            trace.workflow,
                            trace.workflow_version,
                            trace.step,
                        )
                        for trace in self.traces
                        if trace.step_run_id == run_id
                    }
                )
                > 1
                for run_id in run_ids
            ),
            "conflicting_terminal_runs": sum(
                len(
                    {
                        event.status
                        for event in events
                        if event.status in terminal
                    }
                )
                > 1
                for events in by_run.values()
            ),
            "cross_task_dependencies": sum(
                dependency in by_run
                and not any(
                    target.identity.task_id == event.identity.task_id
                    for target in by_run[dependency]
                )
                for event in self._workflow_events.values()
                for dependency in event.identity.dependency_step_run_ids
            ),
            "unreported_step_runs": sum(row.unreported_runs for row in metrics),
            "inconsistent_step_runs": sum(row.inconsistent for row in metrics),
        }

    def workflow_step_metrics(
        self,
        workflow: str | None = None,
        workflow_version: str | None = None,
    ) -> list[WorkflowStepMetric]:
        traces = [
            {
                "task_id": trace.task_id,
                "workflow": trace.workflow,
                "workflow_version": trace.workflow_version,
                "step": trace.step,
                "step_run_id": trace.step_run_id,
                "status_code": trace.status_code,
                "cost_usd": trace.cost_usd,
                "input_tokens": trace.input_tokens,
                "output_tokens": trace.output_tokens,
                "latency_ms": trace.latency_ms,
            }
            for trace in self.traces
            if trace.workflow is not None
            and (workflow is None or trace.workflow == workflow)
            and (
                workflow_version is None
                or trace.workflow_version == workflow_version
            )
        ]
        events = [
            event
            for event in self._workflow_events.values()
            if (workflow is None or event.identity.workflow == workflow)
            and (
                workflow_version is None
                or event.identity.workflow_version == workflow_version
            )
        ]
        return build_workflow_step_metrics(traces, events)

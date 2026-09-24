"""Executable behavior contracts shared by every recorder backend."""

from __future__ import annotations

from dataclasses import asdict

import pytest

from ctrlrtn.policy.experiment import Experiment
from ctrlrtn.policy.fallback import ApprovedFallback
from ctrlrtn.policy.route import Route
from ctrlrtn.policy.shadow import ShadowExperiment
from ctrlrtn.recorder.models import Outcome
from ctrlrtn.recorder.store import InMemoryTraceStore, SqliteTraceStore
from ctrlrtn.recorder.trace import Trace
from ctrlrtn.workflow.identity import WorkflowEvent, WorkflowIdentity
from ctrlrtn.workflow.tool_operation import (
    ToolOperationEvent,
    ToolOperationIdentity,
)


def _trace(
    *,
    task: str,
    use_case: str,
    session: str,
    cost: float,
    ts: float,
    terminal: str | None = None,
    fallback: bool = False,
) -> Trace:
    return Trace(
        method="POST",
        path="/v1/messages",
        query="",
        request_headers={},
        request_body=b"{}",
        status_code=429 if terminal else 200,
        response_headers={},
        response_body=b"{}",
        latency_ms=10.0,
        model="requested-model",
        served_model="served-model",
        input_tokens=10,
        output_tokens=2,
        cost_usd=cost,
        use_case_key=use_case,
        task_id=task,
        session_id=session,
        terminal_reason=terminal,
        budget_fallback=fallback,
        ts=ts,
    )


def _normalize(rows: list[object]) -> list[dict]:
    return [asdict(row) for row in rows]


async def _trace_contract(store) -> dict:
    await store.save(
        _trace(
            task="task-a",
            use_case="tag:writer",
            session="session-a",
            cost=0.25,
            ts=10.0,
        )
    )
    await store.save(
        _trace(
            task="task-a",
            use_case="tag:reviewer",
            session="session-a",
            cost=0.10,
            ts=11.0,
            terminal="budget",
            fallback=True,
        )
    )
    await store.save_outcome(
        Outcome("task-a", success=True, score=0.8, ts=12.0)
    )

    identity = WorkflowIdentity(
        "task-a", "pipeline", "v1", "draft", "run-draft"
    )
    workflow_event = WorkflowEvent(
        identity,
        "completed",
        event_id="workflow-event",
        success=True,
        ts=13.0,
    )
    await store.save_workflow_event(workflow_event)
    tool_identity = ToolOperationIdentity(
        identity,
        "search",
        "search:one",
        "attempt-one",
        effect="idempotent",
    )
    tool_event = ToolOperationEvent(
        tool_identity,
        "completed",
        event_id="tool-event",
        success=True,
        ts=14.0,
    )
    await store.save_tool_operation_event(tool_event)

    return {
        "rankings": _normalize(store.rankings()),
        "tasks": _normalize(store.tasks()),
        "sessions": _normalize(store.sessions()),
        "models": store.use_case_models(),
        "spend": store.spend_breakdown_since(0),
        "session_spend": store.session_spend_state(),
        "terminals": store.terminal_counts_since(0),
        "fallbacks": store.fallback_calls_since(0),
        "workflow_events": [
            row.payload() for row in store.workflow_events("task-a")
        ],
        "tool_events": [
            row.payload() for row in store.tool_operation_events("task-a")
        ],
    }


@pytest.mark.contract
async def test_trace_backends_have_identical_observable_behavior(tmp_path):
    memory = InMemoryTraceStore()
    sqlite = SqliteTraceStore(tmp_path / "contract.db")
    try:
        assert await _trace_contract(memory) == await _trace_contract(sqlite)
    finally:
        memory.close()
        sqlite.close()


def _control_contract(store) -> dict:
    experiment = Experiment(
        "tag:writer",
        "candidate",
        25,
        experiment_id="exp:contract",
        created_epoch=10.0,
    )
    store.create_experiment(experiment)
    adopted = store.adopt_experiment(
        experiment.experiment_id,
        Route(
            "tag:writer",
            "candidate",
            previous_model="baseline",
            note="contract",
            ts=11.0,
        ),
    )
    fallback = ApprovedFallback(
        "tag:reviewer",
        "cheap-model",
        "baseline-model",
        evidence_created=5.0,
        approved_at=12.0,
    )
    store.set_fallback(fallback)
    shadow = ShadowExperiment(
        "tag:shadow",
        "shadow-model",
        20,
        shadow_id="shadow:contract",
        created_epoch=13.0,
    )
    store.create_shadow_experiment(shadow)
    store.increment_shadow_stats(
        shadow.shadow_id, submitted=3, completed=2, failed=1, dropped=1
    )
    stopped = store.stop_shadow_experiment(shadow.shadow_id)

    return {
        "adopted": adopted,
        "experiment": asdict(store.experiment(experiment.experiment_id)),
        "running": store.running_experiments(),
        "routes": _normalize(store.routes()),
        "fallbacks": _normalize(store.fallbacks()),
        "shadow": _normalize(store.shadow_experiments()),
        "shadow_stats": asdict(store.shadow_stats(shadow.shadow_id)),
        "shadow_stopped": stopped,
        "repeat_stop": store.stop_shadow_experiment(shadow.shadow_id),
        "clear_fallback": store.clear_fallback(fallback.use_case_key),
        "repeat_clear_fallback": store.clear_fallback(fallback.use_case_key),
        "clear_route": store.clear_route(experiment.use_case_key),
        "repeat_clear_route": store.clear_route(experiment.use_case_key),
    }


@pytest.mark.contract
def test_control_backends_have_identical_state_transitions(tmp_path):
    memory = InMemoryTraceStore()
    sqlite = SqliteTraceStore(tmp_path / "control-contract.db")
    try:
        assert _control_contract(memory) == _control_contract(sqlite)
    finally:
        memory.close()
        sqlite.close()

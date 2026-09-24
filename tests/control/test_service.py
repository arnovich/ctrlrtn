"""Shared operator use cases stay identical across CLI and TUI adapters."""

from __future__ import annotations

import json

from ctrlrtn.control.service import (
    apply_adoption,
    prepare_adoption,
    prepare_route_change,
)
from ctrlrtn.policy.experiment import Experiment
from ctrlrtn.recorder.memory_store import InMemoryTraceStore
from ctrlrtn.recorder.trace import Trace


def _trace(max_tokens: int) -> Trace:
    return Trace(
        method="POST",
        path="/v1/messages",
        query="",
        request_headers={},
        request_body=json.dumps({"max_tokens": max_tokens}).encode(),
        status_code=200,
        response_headers={},
        response_body=b"",
        latency_ms=1,
        use_case_key="tag:editor",
        model="claude-sonnet-4-5",
    )


def test_route_plan_shares_dormancy_and_output_cap_warnings():
    store = InMemoryTraceStore()
    store.traces.append(_trace(128_000))
    store.create_experiment(
        Experiment(
            "tag:editor",
            "claude-haiku-4-5",
            50,
            experiment_id="exp:running",
        )
    )
    plan = prepare_route_change(
        store,
        use_case_key="tag:editor",
        model="claude-haiku-4-5",
        provider_exists=lambda _: True,
    )
    assert plan.dormant_experiment_id == "exp:running"
    assert [notice.code for notice in plan.notices] == ["output_cap"]
    assert "128000" in plan.notices[0].message


def test_adoption_plan_applies_the_candidate_route():
    store = InMemoryTraceStore()
    store.traces.append(_trace(1_000))
    experiment = Experiment(
        "tag:editor",
        "claude-haiku-4-5",
        50,
        experiment_id="exp:adopt",
    )
    store.create_experiment(experiment)
    plan = prepare_adoption(
        store, experiment.experiment_id, provider_exists=lambda _: True
    )
    assert apply_adoption(store, plan)
    assert not store.experiment(experiment.experiment_id).is_running
    assert store.routes() == [plan.route]

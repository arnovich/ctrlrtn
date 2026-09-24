"""Online shadow mirroring stays off-path, paired, bounded, and measurable."""

from __future__ import annotations

import json

import httpx
import pytest
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route as StarletteRoute

from ctrlrtn.cli import commands as cli
from ctrlrtn.config import Settings
from ctrlrtn.gateway.app import create_app
from ctrlrtn.gateway.shadow import ShadowManager
from ctrlrtn.policy.budget import BudgetGate, BudgetPolicy, SpendSnapshot
from ctrlrtn.policy.experiment import Experiment
from ctrlrtn.policy.shadow import ShadowExperiment, selected
from ctrlrtn.recorder.recorder import Recorder
from ctrlrtn.recorder.store import InMemoryTraceStore, SqliteTraceStore
from ctrlrtn.workflow.identity import WorkflowIdentity


def test_shadow_model_validation_and_stable_sampling():
    experiment = ShadowExperiment(
        "tag:editor", "candidate", 25, shadow_id="shadow:test"
    )
    assert selected(experiment, "task-1") == selected(experiment, "task-1")
    with pytest.raises(ValueError, match="1..100"):
        ShadowExperiment("tag:x", "m", 0)
    scoped = ShadowExperiment(
        "tag:editor",
        "candidate",
        25,
        workflow="pipeline",
        workflow_version="v1",
        step="draft",
    )
    matching = WorkflowIdentity(
        "task-1", "pipeline", "v1", "draft", "run-draft"
    )
    other = WorkflowIdentity("task-1", "pipeline", "v1", "review", "run-review")
    assert scoped.scope.matches_headers(matching.headers())
    assert not scoped.scope.matches_headers(other.headers())
    assert not scoped.scope.matches_headers({"x-ctrlrtn-workflow": "pipeline"})


def test_shadow_store_lifecycle_and_stats(tmp_path):
    store = SqliteTraceStore(str(tmp_path / "router.db"))
    experiment = ShadowExperiment(
        "tag:editor",
        "candidate",
        10,
        shadow_id="shadow:test",
        workflow="pipeline",
        workflow_version="v1",
        step="draft",
    )
    try:
        store.create_shadow_experiment(experiment)
        assert store.running_shadow_experiments()["tag:editor"] == experiment
        store.increment_shadow_stats(
            experiment.shadow_id, submitted=3, completed=1, failed=1, dropped=1
        )
        stats = store.shadow_stats(experiment.shadow_id)
        assert (
            stats.submitted,
            stats.completed,
            stats.failed,
            stats.dropped,
        ) == (
            3,
            1,
            1,
            1,
        )
        assert store.stop_shadow_experiment(experiment.shadow_id)
        assert store.running_shadow_experiments() == {}
    finally:
        store.close()


def test_live_split_and_shadow_are_mutually_exclusive(tmp_path):
    store = SqliteTraceStore(str(tmp_path / "router.db"))
    try:
        store.create_experiment(
            Experiment("tag:x", "candidate", 50, experiment_id="exp:x")
        )
        with pytest.raises(ValueError, match="live experiment"):
            store.create_shadow_experiment(
                ShadowExperiment("tag:x", "other", 10, shadow_id="shadow:x")
            )
        store.stop_experiment("exp:x")
        store.create_shadow_experiment(
            ShadowExperiment("tag:x", "other", 10, shadow_id="shadow:x")
        )
        with pytest.raises(ValueError, match="running shadow"):
            store.create_experiment(
                Experiment("tag:x", "candidate", 50, experiment_id="exp:y")
            )
    finally:
        store.close()


def test_shadow_cli_lifecycle(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("CTRLRTN_DB", str(tmp_path / "router.db"))
    cli.main(
        [
            "shadow",
            "start",
            "tag:editor",
            "candidate",
            "--sample",
            "20",
            "--id",
            "shadow:cli",
        ]
    )
    assert "no shadow output is served" in capsys.readouterr().out
    cli.main(["shadow", "list"])
    assert "shadow:cli" in capsys.readouterr().out
    cli.main(["shadow", "stop", "shadow:cli"])
    assert "Stopped shadow experiment" in capsys.readouterr().out


def _upstream(record: list[dict], content: str) -> Starlette:
    async def handle(request: Request):
        payload = json.loads(await request.body())
        payload["_headers"] = dict(request.headers)
        record.append(payload)
        return JSONResponse(
            {
                "content": content,
                "usage": {"input_tokens": 1, "output_tokens": 1},
            }
        )

    return Starlette(
        routes=[StarletteRoute("/{path:path}", handle, methods=["POST"])]
    )


async def test_shadow_mirrors_without_changing_the_served_response():
    actual_calls: list[dict] = []
    shadow_calls: list[dict] = []
    store = InMemoryTraceStore()
    store.create_shadow_experiment(
        ShadowExperiment(
            "tag:editor", "candidate-model", 100, shadow_id="shadow:test"
        )
    )
    recorder = Recorder(store)
    actual_client = httpx.AsyncClient(
        transport=httpx.ASGITransport(app=_upstream(actual_calls, "actual"))
    )
    shadow_client = httpx.AsyncClient(
        transport=httpx.ASGITransport(app=_upstream(shadow_calls, "shadow"))
    )
    app = create_app(
        Settings(upstream_base_url="http://upstream"),
        upstream_client=actual_client,
        shadow_client=shadow_client,
        recorder=recorder,
        store=store,
    )
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://router"
        ) as client:
            response = await client.post(
                "/v1/messages",
                headers={
                    "x-ctrlrtn-route": "editor",
                    "x-ctrlrtn-task": "task-1",
                },
                json={"model": "baseline-model", "messages": []},
            )
            assert response.json()["content"] == "actual"
        await app.state.shadow_manager._queue.join()
        await recorder.join()

    assert actual_calls[0]["model"] == "baseline-model"
    assert shadow_calls[0]["model"] == "candidate-model"
    assert "x-ctrlrtn-task" not in actual_calls[0]["_headers"]
    assert "x-ctrlrtn-task" not in shadow_calls[0]["_headers"]
    actual = next(row for row in store.traces if row.shadow_role == "actual")
    candidate = next(
        row for row in store.traces if row.shadow_role == "candidate"
    )
    assert actual.shadow_pair_id == candidate.shadow_pair_id
    assert actual.shadow_experiment_id == candidate.shadow_experiment_id
    stats = store.shadow_stats("shadow:test")
    assert stats.submitted == 1 and stats.completed == 1
    await actual_client.aclose()
    await shadow_client.aclose()


async def test_locally_rejected_request_never_submits_a_shadow_call():
    actual_calls: list[dict] = []
    shadow_calls: list[dict] = []
    store = InMemoryTraceStore()
    store.create_shadow_experiment(
        ShadowExperiment(
            "tag:editor", "candidate-model", 100, shadow_id="shadow:test"
        )
    )
    recorder = Recorder(store)
    snapshot = SpendSnapshot()
    snapshot.seed(1.0, {})
    actual_client = httpx.AsyncClient(
        transport=httpx.ASGITransport(app=_upstream(actual_calls, "actual"))
    )
    shadow_client = httpx.AsyncClient(
        transport=httpx.ASGITransport(app=_upstream(shadow_calls, "shadow"))
    )
    app = create_app(
        Settings(upstream_base_url="http://upstream"),
        upstream_client=actual_client,
        shadow_client=shadow_client,
        recorder=recorder,
        store=store,
        budget_gate=BudgetGate(BudgetPolicy(global_daily_usd=1.0), snapshot),
    )

    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://router"
        ) as client:
            response = await client.post(
                "/v1/messages",
                headers={
                    "x-ctrlrtn-route": "editor",
                    "x-ctrlrtn-task": "task-1",
                },
                json={"model": "gpt-4o", "messages": []},
            )
        await app.state.shadow_manager._queue.join()
        await recorder.join()

    assert response.status_code == 429
    assert actual_calls == []
    assert shadow_calls == []
    assert all(trace.shadow_pair_id is None for trace in store.traces)
    assert store.shadow_stats("shadow:test").submitted == 0
    await actual_client.aclose()
    await shadow_client.aclose()


async def test_transient_shadow_store_failure_does_not_kill_worker():
    class FlakyStore(InMemoryTraceStore):
        fail_next_increment = True

        def increment_shadow_stats(self, shadow_id, **counts):
            if self.fail_next_increment:
                self.fail_next_increment = False
                raise RuntimeError("transient store failure")
            return super().increment_shadow_stats(shadow_id, **counts)

    calls: list[httpx.Request] = []

    def respond(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        return httpx.Response(200, json={"content": "candidate"})

    store = FlakyStore()
    store.create_shadow_experiment(
        ShadowExperiment(
            "tag:editor", "candidate-model", 100, shadow_id="shadow:test"
        )
    )
    recorder = Recorder(store)
    recorder.start()
    client = httpx.AsyncClient(transport=httpx.MockTransport(respond))
    manager = ShadowManager(
        store,
        recorder,
        Settings().resolver(),
        client=client,
        workers=1,
        refresh_seconds=3600,
    )
    await manager.start()

    def submit(task: str):
        return manager.submit(
            method="POST",
            path="/v1/messages",
            provider_path="/v1/messages",
            query="",
            headers={"x-ctrlrtn-route": "editor", "x-ctrlrtn-task": task},
            body=b'{"model":"baseline","messages":[]}',
            baseline_base_url="http://upstream",
            baseline_api=None,
            baseline_provider=None,
            baseline_free=False,
            baseline_credential=None,
        )

    try:
        assert submit("first") is not None
        await manager._queue.join()
        assert not manager._workers[0].done()

        assert submit("second") is not None
        await manager._queue.join()
        await recorder.join()
    finally:
        await manager.aclose()
        await recorder.aclose()
        await client.aclose()

    assert len(calls) == 1
    stats = store.shadow_stats("shadow:test")
    assert stats.submitted == 1
    assert stats.completed == 1
    assert stats.dropped == 1


def test_whole_workflow_shadow_rejects_baseline_history_splicing():
    with pytest.raises(ValueError, match="candidate-owned continuation"):
        ShadowExperiment(
            "fp:x",
            "candidate",
            10,
            workflow="pipeline",
            workflow_version="v1",
        )

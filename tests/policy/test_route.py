"""Routes: persistent per-use-case model overrides — store, serving, savings."""

from __future__ import annotations

import json
import sqlite3

import httpx
import pytest

from ctrlrtn.config import Settings
from ctrlrtn.control_config import ControlConfig, ControlRevision
from ctrlrtn.gateway.app import create_app
from ctrlrtn.gateway.serving import ExperimentRouter
from ctrlrtn.policy.experiment import Experiment
from ctrlrtn.policy.route import (
    Route,
    RouteDecision,
    WorkflowRoute,
    route_savings,
)
from ctrlrtn.recorder.recorder import Recorder
from ctrlrtn.recorder.store import InMemoryTraceStore, SqliteTraceStore
from ctrlrtn.routing import UpstreamRoute
from ctrlrtn.telemetry.enrich import enrich_trace
from ctrlrtn.workflow.identity import WorkflowIdentity

_UC = "tag:editor"
_SONNET = "claude-sonnet-4-5"
_HAIKU = "claude-haiku-4-5"


def _body(model=_SONNET) -> bytes:
    return json.dumps(
        {"model": model, "messages": [{"role": "user", "content": "hi"}]}
    ).encode()


def _headers(route=_UC.removeprefix("tag:"), task=None) -> dict:
    headers = {"x-ctrlrtn-route": route}
    if task:
        headers["x-ctrlrtn-task"] = task
    return headers


async def _router(store) -> ExperimentRouter:
    router = ExperimentRouter(store, inject_cache=False, refresh_seconds=999)
    await router.refresh()
    return router


# --- store round-trip --------------------------------------------------------


def test_sqlite_route_roundtrip_and_replace(tmp_path):
    db = str(tmp_path / "routes.db")
    store = SqliteTraceStore(db)
    try:
        store.set_route(
            Route(
                _UC,
                _HAIKU,
                previous_model=_SONNET,
                note="adopted",
                ts=5.0,
                provider="ollama",
            )
        )
        (route,) = store.routes()
        assert route.model == _HAIKU and route.previous_model == _SONNET
        assert route.note == "adopted" and route.ts == 5.0
        assert route.provider == "ollama"
        # Re-setting replaces (one route per use-case, PK).
        store.set_route(Route(_UC, _SONNET, ts=6.0))
        (route,) = store.routes()
        assert route.model == _SONNET
        assert store.clear_route(_UC) is True
        assert store.routes() == []
        assert store.clear_route(_UC) is False  # already gone
    finally:
        store.close()
    # And it survives a reopen (persistent, unlike the snapshot).
    store = SqliteTraceStore(db)
    try:
        store.set_route(Route(_UC, _HAIKU))
        store.close()
        reopened = SqliteTraceStore(db)
        assert reopened.routes()[0].model == _HAIKU
        reopened.close()
    finally:
        pass


def test_adopting_an_experiment_is_one_atomic_transaction():
    store = SqliteTraceStore(":memory:")
    experiment = Experiment(_UC, _HAIKU, 50, experiment_id="exp:atomic")
    store.create_experiment(experiment)
    store._conn.execute("""
        CREATE TRIGGER reject_adoption_route
        BEFORE INSERT ON routes
        BEGIN
            SELECT RAISE(ABORT, 'simulated route failure');
        END
        """)
    try:
        with pytest.raises(sqlite3.IntegrityError):
            store.adopt_experiment(
                experiment.experiment_id,
                Route(
                    _UC,
                    _HAIKU,
                    previous_model=_SONNET,
                    note="adopted from exp:atomic",
                ),
            )
        assert store.experiment(experiment.experiment_id).is_running
        assert store.routes() == []
    finally:
        store.close()


def test_route_validates_inputs():
    with pytest.raises(ValueError):
        Route("", _HAIKU)
    with pytest.raises(ValueError):
        Route(_UC, "")
    with pytest.raises(ValueError, match="provider"):
        Route(_UC, _HAIKU, provider="")


# --- serving: the override fires ----------------------------------------------


async def test_route_swaps_the_model_for_all_traffic():
    store = InMemoryTraceStore()
    store.set_route(
        Route(_UC, _HAIKU, previous_model=_SONNET, provider="ollama")
    )
    router = await _router(store)
    # Tasked AND untasked calls are routed (persistent switch, not a split).
    for headers in (_headers(), _headers(task="ed1")):
        forward, serve = router.decide("/v1/messages", headers, _body())
        assert json.loads(forward)["model"] == _HAIKU
        assert isinstance(serve, RouteDecision)
        assert serve.served_model == _HAIKU
        assert serve.provider == "ollama"
        assert serve.original_model == _SONNET
        assert serve.experiment_id is None and serve.arm is None
        assert serve.is_candidate is False


async def test_exact_step_route_precedes_workflow_and_use_case_routes():
    store = InMemoryTraceStore()
    store.set_route(Route(_UC, "use-case-model"))
    store.set_workflow_route(WorkflowRoute("pipeline", "v1", "workflow-model"))
    store.set_workflow_route(
        WorkflowRoute("pipeline", "v1", "step-model", step="draft")
    )
    router = await _router(store)
    identity = WorkflowIdentity(
        "task-1", "pipeline", "v1", "draft", "run-draft"
    )

    forward, decision = router.decide(
        "/v1/messages", {**_headers(), **identity.headers()}, _body()
    )
    assert json.loads(forward)["model"] == "step-model"
    assert decision.rule_scope == "workflow_step"
    assert decision.rule_key == "pipeline@v1/draft"


async def test_workflow_route_requires_complete_exact_version_identity():
    store = InMemoryTraceStore()
    store.set_workflow_route(WorkflowRoute("pipeline", "v1", "workflow-model"))
    router = await _router(store)

    partial = {"x-ctrlrtn-workflow": "pipeline", "x-ctrlrtn-task": "task-1"}
    forward, decision = router.decide("/v1/messages", partial, _body())
    assert forward == _body() and decision is None

    wrong_version = WorkflowIdentity(
        "task-1", "pipeline", "v2", "draft", "run-draft"
    )
    forward, decision = router.decide(
        "/v1/messages", wrong_version.headers(), _body()
    )
    assert forward == _body() and decision is None


async def test_workflow_route_decision_carries_activated_revision(tmp_path):
    store = SqliteTraceStore(str(tmp_path / "workflow-route.db"))
    revision = ControlRevision("a" * 40, "routing.yaml", "b" * 64)
    store.activate_control_config(
        ControlConfig(
            workflow_routes=(WorkflowRoute("pipeline", "v1", "local"),)
        ),
        revision,
    )
    router = await _router(store)
    identity = WorkflowIdentity(
        "task-1", "pipeline", "v1", "draft", "run-draft"
    )
    try:
        _, decision = router.decide("/v1/messages", identity.headers(), _body())
        assert decision.control_revision == "a" * 40
        assert decision.rule_scope == "workflow"
    finally:
        store.close()


async def test_route_switches_to_a_same_api_provider(streaming_upstream):
    record: dict = {}
    store = InMemoryTraceStore()
    store.set_route(Route(_UC, "qwen2.5:0.5b", provider="ollama"))
    settings = Settings(
        routes=(
            UpstreamRoute(
                "openai",
                "http://openai",
                ("/v1/chat/completions",),
                api="openai",
            ),
            UpstreamRoute(
                "ollama",
                "http://ollama",
                ("/ollama",),
                strip_prefix="/ollama",
                api="openai",
                free=True,
            ),
        )
    )
    app = create_app(
        settings,
        upstream_client=httpx.AsyncClient(
            transport=httpx.ASGITransport(app=streaming_upstream(record)),
            base_url="http://upstream",
        ),
        store=store,
    )
    await app.state.experiment_router.refresh()
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://router"
    ) as client:
        response = await client.post(
            "/v1/chat/completions", content=_body(), headers=_headers()
        )

    assert response.status_code == 200
    assert record["headers"]["host"] == "ollama"
    assert record["path"] == "/v1/chat/completions"
    assert json.loads(record["body"])["model"] == "qwen2.5:0.5b"


async def test_route_is_a_noop_when_already_on_the_target():
    store = InMemoryTraceStore()
    store.set_route(Route(_UC, _HAIKU))
    router = await _router(store)
    forward, serve = router.decide("/v1/messages", _headers(), _body(_HAIKU))
    assert serve is None and forward == _body(_HAIKU)


async def test_route_leaves_other_use_cases_alone():
    store = InMemoryTraceStore()
    store.set_route(Route(_UC, _HAIKU))
    router = await _router(store)
    forward, serve = router.decide(
        "/v1/messages", _headers(route="journalist"), _body()
    )
    assert serve is None and json.loads(forward)["model"] == _SONNET


def _task_for_arm(exp: Experiment, arm: str) -> str:
    from ctrlrtn.policy.experiment import assign_arm

    i = 0
    while True:
        task = f"task-{i}"
        if assign_arm(exp, task) == arm:
            return task
        i += 1


async def test_running_experiment_takes_precedence_over_a_route_on_both_arms():
    from ctrlrtn.policy.experiment import BASELINE, CANDIDATE

    store = InMemoryTraceStore()
    store.set_route(Route(_UC, _HAIKU))
    exp = Experiment(_UC, _HAIKU, 50, experiment_id="exp:precedence")
    store.create_experiment(exp)
    router = await _router(store)
    # Candidate arm: the EXPERIMENT swaps (an arm decision, not the route).
    forward, serve = router.decide(
        "/v1/messages", _headers(task=_task_for_arm(exp, CANDIDATE)), _body()
    )
    assert not isinstance(serve, RouteDecision)
    assert serve.experiment_id == "exp:precedence" and serve.is_candidate
    assert json.loads(forward)["model"] == _HAIKU
    # Baseline arm: MUST pass through untouched — a route swallowing the
    # control group would silently destroy the experiment.
    forward, serve = router.decide(
        "/v1/messages", _headers(task=_task_for_arm(exp, BASELINE)), _body()
    )
    assert not isinstance(serve, RouteDecision)
    assert serve.is_baseline
    assert json.loads(forward)["model"] == _SONNET
    # Untasked under a running experiment: pass through — the route stays
    # dormant until the experiment stops.
    _, serve = router.decide("/v1/messages", _headers(), _body())
    assert serve is None


async def test_step_scoped_split_only_assigns_matching_explicit_identity():
    from ctrlrtn.policy.experiment import CANDIDATE

    store = InMemoryTraceStore()
    experiment = Experiment(
        _UC,
        _HAIKU,
        50,
        experiment_id="exp:step",
        workflow="pipeline",
        workflow_version="v1",
        step="draft",
    )
    store.create_experiment(experiment)
    router = await _router(store)
    task = _task_for_arm(experiment, CANDIDATE)
    matching = WorkflowIdentity(task, "pipeline", "v1", "draft", "run-draft")
    other = WorkflowIdentity(task, "pipeline", "v1", "review", "run-review")

    forward, decision = router.decide(
        "/v1/messages", {**_headers(), **matching.headers()}, _body()
    )
    assert decision.experiment_id == "exp:step"
    assert json.loads(forward)["model"] == _HAIKU
    forward, decision = router.decide(
        "/v1/messages", {**_headers(), **other.headers()}, _body()
    )
    assert decision is None and forward == _body()


async def test_route_takes_over_when_the_experiment_stops():
    store = InMemoryTraceStore()
    store.set_route(Route(_UC, _HAIKU))
    store.create_experiment(
        Experiment(_UC, _HAIKU, 50, experiment_id="exp:done")
    )
    router = await _router(store)
    store.stop_experiment("exp:done")
    await router.refresh()
    _, serve = router.decide("/v1/messages", _headers(task="t1"), _body())
    assert isinstance(serve, RouteDecision) and serve.served_model == _HAIKU


async def test_route_clamps_max_tokens_to_the_target_cap():
    # 100% of traffic flows through a route: a recorded max_tokens above the
    # routed model's cap must clamp (truncation), never 400 the use-case.
    store = InMemoryTraceStore()
    store.set_route(Route(_UC, _HAIKU))  # haiku-4-5 caps at 64000
    router = await _router(store)
    over = json.dumps(
        {"model": _SONNET, "max_tokens": 128000, "messages": []}
    ).encode()
    forward, serve = router.decide("/v1/messages", _headers(), over)
    assert serve is not None
    assert json.loads(forward)["max_tokens"] == 64000
    # At or under the cap: untouched.
    under = json.dumps(
        {"model": _SONNET, "max_tokens": 4096, "messages": []}
    ).encode()
    forward, _ = router.decide("/v1/messages", _headers(), under)
    assert json.loads(forward)["max_tokens"] == 4096
    # Unknown target cap: no clamp (never invent a limit).
    store.set_route(Route(_UC, "unpriced-model"))
    await router.refresh()
    forward, _ = router.decide("/v1/messages", _headers(), over)
    assert json.loads(forward)["max_tokens"] == 128000


async def test_unparseable_body_passes_through():
    store = InMemoryTraceStore()
    store.set_route(Route(_UC, _HAIKU))
    router = await _router(store)
    forward, serve = router.decide("/v1/messages", _headers(), b"not json")
    assert serve is None and forward == b"not json"


# --- end-to-end: the trace records the routed model ---------------------------


async def test_routed_call_records_served_model(streaming_upstream):
    record: dict = {}
    store = InMemoryTraceStore()
    store.set_route(Route(_UC, _HAIKU, previous_model=_SONNET))
    recorder = Recorder(store, enrich=enrich_trace)
    app = create_app(
        Settings(upstream_base_url="http://upstream"),
        upstream_client=httpx.AsyncClient(
            transport=httpx.ASGITransport(app=streaming_upstream(record)),
            base_url="http://upstream",
        ),
        recorder=recorder,
        store=store,
    )
    await app.state.experiment_router.refresh()  # ASGITransport skips lifespan
    recorder.start()  # ditto: lifespan didn't run, start the worker manually
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://router"
    ) as client:
        await client.post("/v1/messages", content=_body(), headers=_headers())
    await recorder.join()
    assert json.loads(record["body"])["model"] == _HAIKU  # upstream saw haiku
    trace = store.traces[0]
    assert trace.served_model == _HAIKU
    assert trace.experiment_id is None and trace.arm is None
    assert json.loads(trace.request_body)["model"] == _SONNET  # original kept


# --- savings -------------------------------------------------------------------


def test_route_savings_prices_the_old_model_minus_actual():
    usage = {
        "calls": 10,
        "input_tokens": 1_000_000,
        "output_tokens": 100_000,
        "cache_read_tokens": 0,
        "cache_write_tokens": 0,
        "cost_usd": 1.5,  # actually spent on the routed (cheap) model
    }
    # At sonnet prices: 1M * $3 + 0.1M * $15 = $4.50 -> saved $3.00.
    assert route_savings(usage, _SONNET) == pytest.approx(3.0)
    assert route_savings(usage, None) is None
    assert route_savings(usage, "unpriced-model") is None


def test_use_case_usage_since_windows_by_time():
    store = SqliteTraceStore(":memory:")
    try:
        from ctrlrtn.recorder.trace import Trace

        def t(ts, cost):
            return Trace(
                method="POST",
                path="/v1/messages",
                query="",
                request_headers={},
                request_body=b"{}",
                status_code=200,
                response_headers={},
                response_body=b"",
                latency_ms=1.0,
                use_case_key=_UC,
                input_tokens=100,
                output_tokens=10,
                cost_usd=cost,
                ts=ts,
            )

        store._insert(t(10.0, 1.0))  # before the switch
        store._insert(t(20.0, 0.1))  # after
        store._insert(t(30.0, 0.1))  # after
        usage = store.use_case_usage_since(_UC, 15.0)
        assert usage["calls"] == 2
        assert usage["cost_usd"] == pytest.approx(0.2)
        assert usage["input_tokens"] == 200
    finally:
        store.close()


def test_use_case_model_breakdown_groups_by_served_model():
    store = SqliteTraceStore(":memory:")
    try:
        from ctrlrtn.recorder.trace import Trace

        def t(model, served, cost, provider="anthropic"):
            return Trace(
                method="POST",
                path="/v1/messages",
                query="",
                request_headers={},
                request_body=b"{}",
                status_code=200,
                response_headers={},
                response_body=b"",
                latency_ms=1.0,
                use_case_key=_UC,
                model=model,
                served_model=served,
                cost_usd=cost,
                provider=provider,
            )

        store._insert(t(_SONNET, None, 1.0))  # pass-through: sonnet
        store._insert(t(_SONNET, _HAIKU, 0.1))  # routed: counts as haiku
        store._insert(t(_SONNET, _HAIKU, 0.1))
        store._insert(t(_SONNET, None, 0.0, provider="ollama"))
        rows = store.use_case_model_breakdown(_UC)
        assert rows[0]["model"] == _SONNET and rows[0]["cost_usd"] == 1.0
        assert rows[1]["model"] == _HAIKU and rows[1]["calls"] == 2
        assert rows[2] == {
            "provider": "ollama",
            "model": _SONNET,
            "calls": 1,
            "cost_usd": 0.0,
        }
    finally:
        store.close()


# --- CLI ------------------------------------------------------------------------


def _cli_db(tmp_path, monkeypatch):
    from ctrlrtn.cli import commands as cli

    db = str(tmp_path / "cli.db")
    monkeypatch.setattr(cli, "_db_path", lambda: db)
    config = tmp_path / "config.yaml"
    config.write_text(
        "providers:\n"
        "  ollama:\n"
        "    base_url: http://localhost:11434\n"
        "    api: openai\n"
        "    free: true\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("CTRLRTN_CONFIG", str(config))
    return cli, db


def test_cli_route_set_list_clear(tmp_path, monkeypatch, capsys):
    cli, db = _cli_db(tmp_path, monkeypatch)
    # Seed recorded traffic so the previous model is inferable and the
    # post-switch stats have something to count.
    store = SqliteTraceStore(db)
    from ctrlrtn.recorder.trace import Trace

    store._insert(
        Trace(
            method="POST",
            path="/v1/messages",
            query="",
            request_headers={},
            request_body=b"{}",
            status_code=200,
            response_headers={},
            response_body=b"",
            latency_ms=1.0,
            use_case_key=_UC,
            model=_SONNET,
            input_tokens=100,
            output_tokens=10,
            cost_usd=0.5,
            ts=0.0,
        )
    )
    store.close()

    cli.main(
        [
            "route",
            "set",
            _UC,
            _HAIKU,
            "--provider",
            "ollama",
            "--note",
            "test switch",
        ]
    )
    out = capsys.readouterr().out
    assert f"Routing {_UC} -> {_HAIKU} (was {_SONNET})" in out

    cli.main(["route", "list"])
    out = capsys.readouterr().out
    assert _UC in out and _HAIKU in out and _SONNET in out
    assert "ollama" in out
    assert "test switch" in out and "saved" in out

    cli.main(["route", "clear", _UC])
    assert "back to pass-through" in capsys.readouterr().out
    with pytest.raises(SystemExit) as exit_info:
        cli.main(["route", "clear", _UC])  # already gone
    assert exit_info.value.code == 2


def test_cli_route_adopt_stops_the_experiment_and_routes(
    tmp_path, monkeypatch, capsys
):
    cli, db = _cli_db(tmp_path, monkeypatch)
    store = SqliteTraceStore(db)
    store.create_experiment(
        Experiment(
            _UC,
            _HAIKU,
            50,
            experiment_id="exp:winner",
            candidate_provider="ollama",
        )
    )
    store.close()

    cli.main(["route", "adopt", "exp:winner"])
    out = capsys.readouterr().out
    assert "Stopped experiment exp:winner" in out
    assert f"Routing {_UC} -> {_HAIKU}" in out

    verify = SqliteTraceStore(db)
    try:
        assert verify.running_experiments() == {}
        (route,) = verify.routes()
        assert route.model == _HAIKU
        assert route.provider == "ollama"
        assert route.note == "adopted from exp:winner"
    finally:
        verify.close()


def test_cli_route_set_warns_when_recorded_max_tokens_exceed_the_cap(
    tmp_path, monkeypatch, capsys
):
    cli, db = _cli_db(tmp_path, monkeypatch)
    store = SqliteTraceStore(db)
    from ctrlrtn.recorder.trace import Trace

    store._insert(
        Trace(
            method="POST",
            path="/v1/messages",
            query="",
            request_headers={},
            request_body=json.dumps(
                {"model": "claude-sonnet-5", "max_tokens": 128000}
            ).encode(),
            status_code=200,
            response_headers={},
            response_body=b"",
            latency_ms=1.0,
            use_case_key=_UC,
            model="claude-sonnet-5",
        )
    )
    store.close()
    cli.main(["route", "set", _UC, _HAIKU])
    err = capsys.readouterr().err
    assert "max_tokens up to 128000" in err
    assert "64000" in err and "clamp" in err


def test_cli_route_adopt_unknown_experiment_fails(tmp_path, monkeypatch):
    cli, _ = _cli_db(tmp_path, monkeypatch)
    with pytest.raises(SystemExit) as exit_info:
        cli.main(["route", "adopt", "exp:nope"])
    assert exit_info.value.code == 2


def test_cli_route_set_rejects_an_unknown_provider(
    tmp_path, monkeypatch, capsys
):
    cli, _ = _cli_db(tmp_path, monkeypatch)
    with pytest.raises(SystemExit) as exit_info:
        cli.main(["route", "set", _UC, _HAIKU, "--provider", "missing"])
    assert exit_info.value.code == 2
    assert "unknown provider" in capsys.readouterr().err

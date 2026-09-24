"""Evidence-gated model downgrades before a use-case's hard budget ceiling."""

from __future__ import annotations

import json

import httpx
import pytest

from ctrlrtn.cli import commands as cli
from ctrlrtn.config import Settings
from ctrlrtn.gateway.app import create_app
from ctrlrtn.gateway.serving import ExperimentRouter
from ctrlrtn.policy.budget import BudgetGate, BudgetPolicy, SpendSnapshot
from ctrlrtn.policy.experiment import Experiment
from ctrlrtn.policy.fallback import (
    ApprovedFallback,
    approved_fallback_from_replay,
)
from ctrlrtn.policy.route import Route
from ctrlrtn.recorder.recorder import Recorder
from ctrlrtn.recorder.store import InMemoryTraceStore, SqliteTraceStore
from ctrlrtn.routing import UpstreamRoute
from ctrlrtn.telemetry.enrich import enrich_trace

_USE_CASE = "tag:editor"
_BASELINE = "gpt-4o"
_CANDIDATE = "gpt-4o-mini"


def _body(model: str = _BASELINE) -> bytes:
    return json.dumps({"model": model, "messages": []}).encode()


def _replay(verdict: str = "NON_INFERIOR") -> dict:
    return {
        "use_case": _USE_CASE,
        "baseline_model": _BASELINE,
        "candidate_model": _CANDIDATE,
        "verdict": verdict,
        "created": 123.0,
    }


def test_policy_requires_a_lower_hard_ceiling_for_each_fallback_threshold():
    policy = BudgetPolicy(
        use_case_daily_usd={_USE_CASE: 5.0},
        use_case_fallback_usd={_USE_CASE: 4.0},
    )
    assert policy.enabled

    with pytest.raises(ValueError, match="fallback"):
        BudgetPolicy(use_case_fallback_usd={_USE_CASE: 1.0})
    with pytest.raises(ValueError, match="below"):
        BudgetPolicy(
            use_case_daily_usd={_USE_CASE: 1.0},
            use_case_fallback_usd={_USE_CASE: 1.0},
        )


def test_gate_requests_one_fallback_at_the_soft_threshold_then_enforces_hard_cap():
    snapshot = SpendSnapshot()
    snapshot.seed(4.0, {_USE_CASE: 4.0})
    gate = BudgetGate(
        BudgetPolicy(
            use_case_daily_usd={_USE_CASE: 5.0},
            use_case_fallback_usd={_USE_CASE: 4.0},
        ),
        snapshot,
    )
    headers = {"x-ctrlrtn-route": "editor"}

    requested = gate.check(headers, _body(), _body(), provider_free=False)
    assert requested.error_type == "ctrlrtn_budget_fallback_required"
    assert requested.scope == _USE_CASE
    assert requested.spent_usd == requested.limit_usd == 4.0

    approved = gate.check(
        headers,
        _body(),
        _body(_CANDIDATE),
        provider_free=False,
        fallback_applied=True,
    )
    assert approved.allowed

    snapshot.seed(5.0, {_USE_CASE: 5.0})
    hard_stop = gate.check(
        headers,
        _body(),
        _body(_CANDIDATE),
        provider_free=False,
        fallback_applied=True,
    )
    assert hard_stop.error_type == "ctrlrtn_budget_exceeded"


def test_replay_artifact_is_the_only_approval_gate():
    fallback = approved_fallback_from_replay(_replay(), provider="openai")
    assert fallback == ApprovedFallback(
        _USE_CASE,
        _CANDIDATE,
        _BASELINE,
        evidence_created=123.0,
        provider="openai",
        approved_at=fallback.approved_at,
    )

    with pytest.raises(ValueError, match="NON_INFERIOR"):
        approved_fallback_from_replay(_replay("UNDERPOWERED"))
    invalid = _replay()
    invalid["candidate_model"] = ""
    with pytest.raises(ValueError, match="candidate_model"):
        approved_fallback_from_replay(invalid)
    scoped = _replay()
    scoped["scope"] = {
        "workflow": "pipeline",
        "workflow_version": "v1",
        "step": "draft",
    }
    with pytest.raises(ValueError, match="step-scoped"):
        approved_fallback_from_replay(scoped)


def test_approved_fallback_store_roundtrip_and_replace(tmp_path):
    path = str(tmp_path / "fallback.db")
    store = SqliteTraceStore(path)
    first = ApprovedFallback(
        _USE_CASE,
        _CANDIDATE,
        _BASELINE,
        evidence_created=10.0,
        approved_at=20.0,
        provider="openai",
    )
    try:
        store.set_fallback(first)
        assert store.fallbacks() == [first]
        replacement = ApprovedFallback(
            _USE_CASE,
            "gpt-4.1-mini",
            _BASELINE,
            evidence_created=30.0,
            approved_at=40.0,
        )
        store.set_fallback(replacement)
        assert store.fallbacks() == [replacement]
        assert store.clear_fallback(_USE_CASE)
        assert store.fallbacks() == []
        assert not store.clear_fallback(_USE_CASE)
    finally:
        store.close()


async def test_router_resolves_an_approved_fallback_without_database_io():
    store = InMemoryTraceStore()
    store.set_fallback(ApprovedFallback(_USE_CASE, _CANDIDATE, _BASELINE, 10.0))
    router = ExperimentRouter(store, inject_cache=False)
    await router.refresh()

    forward, decision = router.fallback(
        "/v1/chat/completions",
        {"x-ctrlrtn-route": "editor"},
        _body(),
        _body(),
        "openai",
    )

    assert json.loads(forward)["model"] == _CANDIDATE
    assert decision is not None
    assert decision.is_budget_fallback
    assert decision.served_model == _CANDIDATE


@pytest.mark.parametrize("owner", ["route", "experiment"])
async def test_router_never_overrides_an_existing_traffic_owner(owner):
    store = InMemoryTraceStore()
    store.set_fallback(ApprovedFallback(_USE_CASE, _CANDIDATE, _BASELINE, 10.0))
    if owner == "route":
        store.set_route(Route(_USE_CASE, "gpt-4.1-mini"))
    else:
        store.create_experiment(Experiment(_USE_CASE, _CANDIDATE, 50))
    router = ExperimentRouter(store, inject_cache=False)
    await router.refresh()

    forward, decision = router.fallback(
        "/v1/chat/completions",
        {"x-ctrlrtn-route": "editor"},
        _body(),
        _body(),
        "openai",
    )

    assert forward == _body()
    assert decision is None


async def test_router_rejects_stale_evidence_for_a_different_baseline():
    store = InMemoryTraceStore()
    store.set_fallback(ApprovedFallback(_USE_CASE, _CANDIDATE, _BASELINE, 10.0))
    router = ExperimentRouter(store, inject_cache=False)
    await router.refresh()

    body = _body("gpt-4.1")
    forward, decision = router.fallback(
        "/v1/chat/completions",
        {"x-ctrlrtn-route": "editor"},
        body,
        body,
        "openai",
    )

    assert forward == body
    assert decision is None


async def test_gateway_uses_approved_fallback_and_records_it(
    streaming_upstream,
):
    record: dict = {}
    snapshot = SpendSnapshot()
    snapshot.seed(4.0, {_USE_CASE: 4.0})
    gate = BudgetGate(
        BudgetPolicy(
            use_case_daily_usd={_USE_CASE: 5.0},
            use_case_fallback_usd={_USE_CASE: 4.0},
        ),
        snapshot,
    )
    store = InMemoryTraceStore()
    store.set_fallback(ApprovedFallback(_USE_CASE, _CANDIDATE, _BASELINE, 10.0))
    recorder = Recorder(store, enrich=enrich_trace, on_record=gate.observe)
    app = create_app(
        Settings(upstream_base_url="http://upstream"),
        upstream_client=httpx.AsyncClient(
            transport=httpx.ASGITransport(app=streaming_upstream(record)),
            base_url="http://upstream",
        ),
        store=store,
        recorder=recorder,
        budget_gate=gate,
    )
    await app.state.experiment_router.refresh()

    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://router"
        ) as client:
            response = await client.post(
                "/v1/chat/completions",
                content=_body(),
                headers={"x-ctrlrtn-route": "editor"},
            )
        await recorder.join()

    assert response.status_code == 200
    assert json.loads(record["body"])["model"] == _CANDIDATE
    assert store.traces[0].budget_fallback is True
    assert store.fallback_calls_since(0.0) == 1


async def test_gateway_can_fallback_to_a_named_same_api_provider(
    streaming_upstream,
):
    record: dict = {}
    snapshot = SpendSnapshot()
    snapshot.seed(4.0, {_USE_CASE: 4.0})
    gate = BudgetGate(
        BudgetPolicy(
            use_case_daily_usd={_USE_CASE: 5.0},
            use_case_fallback_usd={_USE_CASE: 4.0},
        ),
        snapshot,
    )
    store = InMemoryTraceStore()
    store.set_fallback(
        ApprovedFallback(
            _USE_CASE, "local-model", _BASELINE, 10.0, provider="ollama"
        )
    )
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
        budget_gate=gate,
    )
    await app.state.experiment_router.refresh()

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://router"
    ) as client:
        response = await client.post(
            "/v1/chat/completions",
            content=_body(),
            headers={"x-ctrlrtn-route": "editor"},
        )

    assert response.status_code == 200
    assert record["headers"]["host"] == "ollama"
    assert json.loads(record["body"])["model"] == "local-model"


async def test_gateway_blocks_at_soft_threshold_without_approved_evidence(
    streaming_upstream,
):
    record: dict = {}
    snapshot = SpendSnapshot()
    snapshot.seed(4.0, {_USE_CASE: 4.0})
    gate = BudgetGate(
        BudgetPolicy(
            use_case_daily_usd={_USE_CASE: 5.0},
            use_case_fallback_usd={_USE_CASE: 4.0},
        ),
        snapshot,
    )
    store = InMemoryTraceStore()
    recorder = Recorder(store, enrich=enrich_trace, on_record=gate.observe)
    app = create_app(
        Settings(upstream_base_url="http://upstream"),
        upstream_client=httpx.AsyncClient(
            transport=httpx.ASGITransport(app=streaming_upstream(record)),
            base_url="http://upstream",
        ),
        store=store,
        recorder=recorder,
        budget_gate=gate,
    )
    await app.state.experiment_router.refresh()

    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://router"
        ) as client:
            response = await client.post(
                "/v1/chat/completions",
                content=_body(),
                headers={"x-ctrlrtn-route": "editor"},
            )
        await recorder.join()

    assert response.status_code == 429
    assert (
        response.json()["error"]["type"]
        == "ctrlrtn_budget_fallback_unavailable"
    )
    assert record == {}
    assert store.traces[0].terminal_reason == "fallback_unavailable"


def test_cli_approves_only_a_non_inferior_replay_artifact(
    tmp_path, monkeypatch, capsys
):
    db = str(tmp_path / "cli.db")
    monkeypatch.setattr(cli, "_db_path", lambda: db)
    artifact = tmp_path / "replay.json"
    artifact.write_text(json.dumps(_replay()), encoding="utf-8")

    cli.main(["fallback", "approve", str(artifact)])
    assert f"Approved {_USE_CASE} -> {_CANDIDATE}" in capsys.readouterr().out
    store = SqliteTraceStore(db)
    try:
        assert store.fallbacks()[0].model == _CANDIDATE
    finally:
        store.close()

    artifact.write_text(json.dumps(_replay("UNDERPOWERED")), encoding="utf-8")
    with pytest.raises(SystemExit) as exit_info:
        cli.main(["fallback", "approve", str(artifact)])
    assert exit_info.value.code == 2

    cli.main(["fallback", "list"])
    assert _CANDIDATE in capsys.readouterr().out
    cli.main(["fallback", "clear", _USE_CASE])
    assert "Cleared" in capsys.readouterr().out


def test_fallback_evidence_fields_are_validated_individually():
    for key in ("use_case", "baseline_model", "candidate_model"):
        broken = _replay()
        broken[key] = 7
        with pytest.raises(ValueError, match=f"{key} must be a non-empty"):
            approved_fallback_from_replay(broken)
    stale = _replay()
    stale["created"] = float("nan")
    with pytest.raises(ValueError, match="created must be finite"):
        approved_fallback_from_replay(stale)
    with pytest.raises(ValueError, match="use_case_key must be a non-empty"):
        ApprovedFallback("", _CANDIDATE, _BASELINE, evidence_created=1.0)

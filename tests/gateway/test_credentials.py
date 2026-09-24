"""Credentials are used to forward and NEVER persisted.

The client's provider key must reach the upstream untouched, and must never
appear in a recorded trace — the SQLite file (and its backups, and every
console/CLI read) must be key-free. Display masking is not enough.
"""

from __future__ import annotations

import httpx

from ctrlrtn.config import Settings
from ctrlrtn.gateway.app import create_app
from ctrlrtn.recorder.recorder import Recorder
from ctrlrtn.recorder.store import InMemoryTraceStore, SqliteTraceStore
from ctrlrtn.recorder.trace import Trace
from ctrlrtn.routing import ProviderCredential, UpstreamRoute

_KEY = "sk-ant-verysecret-0123456789abcdef"


async def test_key_reaches_upstream_but_never_the_trace(streaming_upstream):
    record: dict = {}
    store = InMemoryTraceStore()
    recorder = Recorder(store)
    app = create_app(
        Settings(upstream_base_url="http://upstream"),
        upstream_client=httpx.AsyncClient(
            transport=httpx.ASGITransport(app=streaming_upstream(record)),
            base_url="http://upstream",
        ),
        recorder=recorder,
        store=store,
    )
    recorder.start()  # ASGITransport skips lifespan
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://router"
    ) as client:
        await client.post(
            "/v1/messages",
            content=b'{"model":"m","messages":[]}',
            headers={
                "x-api-key": _KEY,
                "authorization": f"Bearer {_KEY}",
                "cookie": "session=alsosecret",
                "anthropic-version": "2023-06-01",
            },
        )
    await recorder.join()

    # The upstream received the real credentials (pass-through contract)...
    assert record["headers"]["x-api-key"] == _KEY
    assert record["headers"]["authorization"] == f"Bearer {_KEY}"
    # ...and the recorded trace never stored them.
    trace = store.traces[0]
    assert trace.request_headers["x-api-key"] == "[redacted]"
    assert trace.request_headers["authorization"] == "[redacted]"
    assert trace.request_headers["cookie"] == "[redacted]"
    assert _KEY not in str(trace.request_headers)
    # Non-credential headers are kept (they matter for debugging/replay).
    assert trace.request_headers["anthropic-version"] == "2023-06-01"


def _raw_trace(headers: dict, response_headers: dict | None = None) -> Trace:
    return Trace(
        method="POST",
        path="/v1/messages",
        query="",
        request_headers=headers,
        request_body=b"{}",
        status_code=200,
        response_headers=(
            response_headers
            if response_headers is not None
            else {"set-cookie": "s=1", "content-type": "json"}
        ),
        response_body=b"",
        latency_ms=1.0,
    )


def test_scrub_redacts_previously_recorded_credentials(tmp_path):
    db = str(tmp_path / "old.db")
    store = SqliteTraceStore(db)
    try:
        # Simulate a database written before capture-time redaction.
        store._insert(_raw_trace({"x-api-key": _KEY, "accept": "*/*"}))
        store._insert(_raw_trace({"Authorization": f"Bearer {_KEY}"}))
        # Fully clean row (response headers too): must NOT count as scrubbed.
        store._insert(_raw_trace({"accept": "*/*"}, response_headers={}))

        assert store.scrub_credential_headers() == 2
        for row_id in (1, 2):
            row = store.get(row_id)
            assert _KEY not in str(row["request_headers"])
            assert "[redacted]" in str(row["request_headers"])
            assert row["response_headers"]["set-cookie"] == "[redacted]"
            assert row["response_headers"]["content-type"] == "json"
        assert store.get(3)["request_headers"]["accept"] == "*/*"
        # Idempotent: nothing left to rewrite.
        assert store.scrub_credential_headers() == 0
    finally:
        store.close()


def test_cli_scrub_command(tmp_path, monkeypatch, capsys):
    from ctrlrtn.cli import commands as cli

    db = str(tmp_path / "old.db")
    store = SqliteTraceStore(db)
    store._insert(_raw_trace({"x-api-key": _KEY}))
    store.close()
    monkeypatch.setattr(cli, "_db_path", lambda: db)
    cli.main(["scrub-credentials"])
    assert "Scrubbed credential headers from 1" in capsys.readouterr().out


async def test_query_param_credentials_forward_but_never_persist(
    streaming_upstream,
):
    # Some providers authenticate as ?key=... — same contract as headers:
    # the upstream gets the real value, the recorded trace does not.
    record: dict = {}
    store = InMemoryTraceStore()
    recorder = Recorder(store)
    app = create_app(
        Settings(upstream_base_url="http://upstream"),
        upstream_client=httpx.AsyncClient(
            transport=httpx.ASGITransport(app=streaming_upstream(record)),
            base_url="http://upstream",
        ),
        recorder=recorder,
        store=store,
    )
    recorder.start()
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://router"
    ) as client:
        await client.post(
            "/v1beta/models/gemini:generateContent?key=" + _KEY + "&alt=json",
            content=b"{}",
        )
    await recorder.join()
    assert f"key={_KEY}" in record["query"]  # upstream got the real key
    trace = store.traces[0]
    assert _KEY not in trace.query
    assert "alt=json" in trace.query  # non-credential params preserved


async def test_named_provider_uses_its_credential_not_the_clients(
    streaming_upstream, monkeypatch
):
    record: dict = {}
    monkeypatch.setenv("OLLAMA_API_KEY", "ollama-cloud-secret")
    app = create_app(
        Settings(
            routes=(
                UpstreamRoute(
                    "ollama_cloud",
                    "http://upstream",
                    ("/ollama_cloud",),
                    strip_prefix="/ollama_cloud",
                    api="openai",
                    credential=ProviderCredential("OLLAMA_API_KEY"),
                ),
            )
        ),
        upstream_client=httpx.AsyncClient(
            transport=httpx.ASGITransport(app=streaming_upstream(record)),
            base_url="http://upstream",
        ),
    )

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://localhost"
    ) as client:
        response = await client.post(
            "/ollama_cloud/v1/chat/completions?api_key=client-secret&x=1",
            content=b"{}",
            headers={
                "authorization": "Bearer client-secret",
                "x-api-key": "client-secret",
            },
        )

    assert response.status_code == 200
    assert record["headers"]["authorization"] == "Bearer ollama-cloud-secret"
    assert "x-api-key" not in record["headers"]
    assert record["query"] == "x=1"


async def test_missing_named_provider_credential_fails_before_forwarding(
    streaming_upstream, monkeypatch
):
    record: dict = {}
    monkeypatch.delenv("MISSING_CLOUD_TOKEN", raising=False)
    app = create_app(
        Settings(
            routes=(
                UpstreamRoute(
                    "cloud",
                    "http://upstream",
                    ("/cloud",),
                    strip_prefix="/cloud",
                    credential=ProviderCredential("MISSING_CLOUD_TOKEN"),
                ),
            )
        ),
        upstream_client=httpx.AsyncClient(
            transport=httpx.ASGITransport(app=streaming_upstream(record)),
            base_url="http://upstream",
        ),
    )

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://localhost"
    ) as client:
        response = await client.post(
            "/cloud/v1/chat/completions", content=b"{}"
        )

    assert response.status_code == 503
    assert (
        response.json()["error"]["type"]
        == "ctrlrtn_provider_credential_unavailable"
    )
    assert record == {}


def test_redact_query_shapes():
    from ctrlrtn.recorder.redaction import redact_query

    assert redact_query("") == ""
    assert redact_query("a=1&b=2") == "a=1&b=2"  # untouched, byte-identical
    out = redact_query("api_key=SECRET&x=1&TOKEN=S2")
    assert "SECRET" not in out and "S2" not in out
    assert "x=1" in out


def test_strip_query_credentials_removes_the_whole_parameter():
    from ctrlrtn.gateway.redact import strip_query_credentials

    assert strip_query_credentials("api_key=SECRET&x=1&TOKEN=S2") == "x=1"
    assert strip_query_credentials("a=1&b=2") == "a=1&b=2"
    assert strip_query_credentials("") == ""


def test_scrub_also_cleans_query_strings(tmp_path):
    db = str(tmp_path / "old.db")
    store = SqliteTraceStore(db)
    try:
        t = _raw_trace({"accept": "*/*"}, response_headers={})
        t.query = f"key={_KEY}&alt=json"
        store._insert(t)
        assert store.scrub_credential_headers() == 1
        row = store.get(1)
        assert _KEY not in row["query"] and "alt=json" in row["query"]
    finally:
        store.close()


async def test_provider_owned_credential_refuses_an_untrusted_host(
    streaming_upstream, monkeypatch
):
    """A mount that spends the operator's key gets the control plane's
    DNS-rebinding guard: a Host the operator never configured is refused
    before anything is forwarded."""
    record: dict = {}
    monkeypatch.setenv("OLLAMA_API_KEY", "ollama-cloud-secret")
    app = create_app(
        Settings(
            routes=(
                UpstreamRoute(
                    "ollama_cloud",
                    "http://upstream",
                    ("/ollama_cloud",),
                    strip_prefix="/ollama_cloud",
                    api="openai",
                    credential=ProviderCredential("OLLAMA_API_KEY"),
                ),
            ),
            control_hosts=("proxy.internal",),
        ),
        upstream_client=httpx.AsyncClient(
            transport=httpx.ASGITransport(app=streaming_upstream(record)),
            base_url="http://upstream",
        ),
    )

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://router"
    ) as client:
        refused = await client.post(
            "/ollama_cloud/v1/chat/completions",
            content=b"{}",
            headers={"host": "evil.example"},
        )
        allowed = await client.post(
            "/ollama_cloud/v1/chat/completions",
            content=b"{}",
            headers={"host": "proxy.internal"},
        )

    assert refused.status_code == 403
    assert refused.json()["error"]["type"] == "ctrlrtn_untrusted_host"
    assert "headers" not in record or record.get("calls", 1) == 1
    assert allowed.status_code == 200
    assert record["headers"]["authorization"] == "Bearer ollama-cloud-secret"

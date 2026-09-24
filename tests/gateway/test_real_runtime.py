"""Vertical gateway tests using actual TCP, Uvicorn, and ASGI lifespan."""

from __future__ import annotations

import json

import httpx
import pytest
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import StreamingResponse
from starlette.routing import Route

from ctrlrtn.config import Settings
from ctrlrtn.gateway.app import create_app
from ctrlrtn.recorder.recorder import Recorder
from ctrlrtn.recorder.sqlite.store import SqliteTraceStore
from ctrlrtn.telemetry.enrich import enrich_trace
from tests.support.failures import FailFirstTraceStore
from tests.support.runtime import running_server


def _upstream(received: list[dict]) -> Starlette:
    async def messages(request: Request) -> StreamingResponse:
        received.append(
            {
                "path": request.url.path,
                "query": request.url.query,
                "body": await request.body(),
                "task": request.headers.get("x-ctrlrtn-task"),
            }
        )

        async def chunks():
            yield b'data: {"message":{"usage":{"input_tokens":3}}}\n\n'
            yield b'data: {"usage":{"output_tokens":2}}\n\n'
            yield b"data: [DONE]\n\n"

        response = StreamingResponse(chunks(), media_type="text/event-stream")
        response.raw_headers.extend(
            [
                (b"set-cookie", b"session=a; Path=/"),
                (b"set-cookie", b"prefs=b; Path=/"),
            ]
        )
        return response

    return Starlette(routes=[Route("/v1/messages", messages, methods=["POST"])])


@pytest.mark.integration
def test_real_runtime_streams_records_and_runs_lifespan(tmp_path):
    received: list[dict] = []
    with running_server(_upstream(received)) as upstream_url:
        store = SqliteTraceStore(tmp_path / "runtime.db")
        recorder = Recorder(store, enrich=enrich_trace)
        app = create_app(
            Settings(upstream_base_url=upstream_url, timeout=2.0),
            recorder=recorder,
            store=store,
        )
        with running_server(app) as router_url:
            with httpx.Client(base_url=router_url, timeout=3.0) as client:
                response = client.post(
                    "/v1/messages?keep=yes",
                    content=json.dumps(
                        {"model": "claude-haiku-4-5", "messages": []}
                    ),
                    headers={
                        "content-type": "application/json",
                        "x-ctrlrtn-task": "task-runtime",
                        "x-ctrlrtn-session": "session-runtime",
                    },
                )
                assert response.status_code == 200
                assert response.content.endswith(b"data: [DONE]\n\n")
                assert response.headers.get_list("set-cookie") == [
                    "session=a; Path=/",
                    "prefs=b; Path=/",
                ]
                outcome = client.post(
                    "/ctrlrtn/outcome",
                    json={"task_id": "task-runtime", "success": True},
                )
                assert outcome.status_code == 200
                assert client.get("/healthz").text == "ok"

        try:
            assert received == [
                {
                    "path": "/v1/messages",
                    "query": "keep=yes",
                    "body": json.dumps(
                        {"model": "claude-haiku-4-5", "messages": []}
                    ).encode(),
                    "task": None,
                }
            ]
            assert store.count() == 1
            task = next(
                row for row in store.tasks() if row.task_id == "task-runtime"
            )
            assert task.calls == 1
            assert task.success is True
            assert store.sessions()[0].session_id == "session-runtime"
        finally:
            store.close()


@pytest.mark.integration
def test_real_runtime_recorder_failure_is_fail_open_and_worker_recovers():
    received: list[dict] = []
    with running_server(_upstream(received)) as upstream_url:
        store = FailFirstTraceStore()
        recorder = Recorder(store, enrich=enrich_trace)
        app = create_app(
            Settings(upstream_base_url=upstream_url, timeout=2.0),
            recorder=recorder,
            store=store,
        )
        with running_server(app) as router_url:
            with httpx.Client(base_url=router_url, timeout=3.0) as client:
                for task_id in ("failed-write", "surviving-write"):
                    response = client.post(
                        "/v1/messages",
                        json={"model": "claude-haiku-4-5", "messages": []},
                        headers={"x-ctrlrtn-task": task_id},
                    )
                    assert response.status_code == 200

        assert store.save_attempts == 2
        assert len(store.traces) == 1
        assert store.traces[0].task_id == "surviving-write"

"""Slice 7: the client SDK — x-ctrlrtn-task stamping + outcome reporting."""

from __future__ import annotations

import asyncio
import json

import httpx
import pytest

from ctrlrtn import sdk


def _capture_handler(captured: dict):
    def handler(request: httpx.Request) -> httpx.Response:
        captured["task"] = request.headers.get("x-ctrlrtn-task")
        captured["route"] = request.headers.get("x-ctrlrtn-route")
        return httpx.Response(200, json={})

    return handler


# --- header stamping ------------------------------------------------------


def test_http_client_stamps_task_and_route_inside_an_edition():
    captured: dict = {}
    with sdk.edition(task_id="ed-1", default_route="editor"):
        client = sdk.http_client(
            transport=httpx.MockTransport(_capture_handler(captured)),
            base_url="http://up",
        )
        client.post("/v1/messages", json={})
        client.close()
    assert captured["task"] == "ed-1"
    assert captured["route"] == "editor"


def test_no_stamp_outside_an_edition():
    captured: dict = {}
    client = sdk.http_client(
        transport=httpx.MockTransport(_capture_handler(captured)),
        base_url="http://up",
    )
    client.post("/x", json={})
    client.close()
    assert captured["task"] is None


def test_route_block_overrides_the_edition_route():
    captured: dict = {}
    with sdk.edition(task_id="ed", default_route="orchestrator"):
        client = sdk.http_client(
            transport=httpx.MockTransport(_capture_handler(captured)),
            base_url="http://up",
        )
        with sdk.route("analyst"):
            client.post("/x", json={})
        client.close()
    assert captured["task"] == "ed"  # same edition
    assert captured["route"] == "analyst"  # sub-agent route


def test_generated_task_id_is_used_when_not_given():
    captured: dict = {}
    with sdk.edition() as run:
        client = sdk.http_client(
            transport=httpx.MockTransport(_capture_handler(captured)),
            base_url="http://up",
        )
        client.post("/x", json={})
        client.close()
    assert captured["task"] == run.task_id
    assert len(run.task_id) == 32


def test_stamp_explicit_threading():
    headers = sdk.stamp({}, task_id="t", route="r")
    assert (
        headers["x-ctrlrtn-task"] == "t" and headers["x-ctrlrtn-route"] == "r"
    )


async def test_stamp_propagates_across_an_asyncio_task():
    # A sub-agent spawned as a task within the edition inherits the context.
    captured: dict = {}

    async def sub_agent():
        client = sdk.async_http_client(
            transport=httpx.MockTransport(_capture_handler(captured)),
            base_url="http://up",
        )
        await client.post("/x", json={})
        await client.aclose()

    with sdk.edition(task_id="ed-async"):
        await asyncio.create_task(sub_agent())
    assert captured["task"] == "ed-async"


def test_new_task_id_is_unique():
    assert sdk.new_task_id() != sdk.new_task_id()


# --- outcome reporting ----------------------------------------------------


def test_report_outcome_posts_the_right_payload():
    captured: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["url"] = str(request.url)
        captured["ct"] = request.headers.get("content-type")
        captured["json"] = json.loads(request.content)
        return httpx.Response(200, text="ok")

    client = httpx.Client(transport=httpx.MockTransport(handler))
    sdk.report_outcome(
        "http://gw/", "ed-1", success=True, score=0.9, client=client
    )
    client.close()
    assert captured["url"] == "http://gw/ctrlrtn/outcome"
    assert captured["ct"] == "application/json"
    assert captured["json"] == {
        "task_id": "ed-1",
        "success": True,
        "score": 0.9,
    }


def test_report_outcome_uses_current_edition_task_id():
    captured: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["json"] = json.loads(request.content)
        return httpx.Response(200, text="ok")

    client = httpx.Client(transport=httpx.MockTransport(handler))
    with sdk.edition(task_id="ed-ctx"):
        sdk.report_outcome("http://gw", success=False, client=client)
    client.close()
    assert captured["json"] == {"task_id": "ed-ctx", "success": False}


def test_report_outcome_requires_a_task_id():
    with pytest.raises(ValueError, match="task_id"):
        sdk.report_outcome("http://gw", success=True)


def test_report_outcome_requires_a_signal():
    with pytest.raises(ValueError, match="success and/or score"):
        sdk.report_outcome("http://gw", "ed", client=httpx.Client())


async def test_areport_outcome_posts():
    captured: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["json"] = json.loads(request.content)
        return httpx.Response(200, text="ok")

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    await sdk.areport_outcome("http://gw", "ed", score=7.5, client=client)
    await client.aclose()
    assert captured["json"] == {"task_id": "ed", "score": 7.5}


# --- edition lifecycle ----------------------------------------------------


def test_edition_auto_reports_failure_on_exception(monkeypatch):
    calls: list = []
    monkeypatch.setattr(
        sdk, "report_outcome", lambda base, tid, **kw: calls.append((tid, kw))
    )
    with (
        pytest.raises(RuntimeError),
        sdk.edition(task_id="ed", report_to="http://gw"),
    ):
        raise RuntimeError("boom")
    assert calls == [("ed", {"success": False, "score": None})]


def test_edition_reports_nothing_on_clean_silent_exit(monkeypatch):
    calls: list = []
    monkeypatch.setattr(sdk, "report_outcome", lambda *a, **k: calls.append(1))
    with sdk.edition(task_id="ed", report_to="http://gw"):
        pass  # completed without crashing != succeeded
    assert calls == []


def test_explicit_report_suppresses_the_auto_failure(monkeypatch):
    calls: list = []
    monkeypatch.setattr(
        sdk, "report_outcome", lambda base, tid, **kw: calls.append(kw)
    )
    with sdk.edition(task_id="ed", report_to="http://gw") as run:
        run.report(success=True, score=0.8)
    assert calls == [{"success": True, "score": 0.8}]


def test_report_is_best_effort(monkeypatch):
    def boom(*a, **k):
        raise httpx.ConnectError("down")

    monkeypatch.setattr(sdk, "report_outcome", boom)
    with sdk.edition(task_id="ed", report_to="http://gw") as run:
        run.report(success=True)  # a failed report must not raise into the app


def test_report_without_report_to_warns_but_does_not_raise(caplog):
    with (
        caplog.at_level("WARNING"),
        sdk.edition(task_id="ed") as run,
    ):  # no report_to
        run.report(success=True)  # must not raise (nowhere to send)
    assert "report_to" in caplog.text


def test_exception_without_report_to_is_silent(caplog):
    # An app that opted out of reporting shouldn't get a warning on every crash.
    with (
        caplog.at_level("WARNING"),
        pytest.raises(RuntimeError),
        sdk.edition(task_id="ed"),
    ):  # no report_to
        raise RuntimeError("boom")
    assert "report_to" not in caplog.text


# --- thread / subprocess propagation guard --------------------------------


def _send_untasked(captured: dict):
    client = sdk.http_client(
        transport=httpx.MockTransport(_capture_handler(captured)),
        base_url="http://up",
    )
    client.post("/x", json={})
    client.close()


def test_untasked_send_while_edition_active_warns(caplog):
    # Simulates a worker thread the contextvar didn't reach: the edition is
    # "active" in the process but this call has no task bound.
    import threading

    captured: dict = {}
    with caplog.at_level("WARNING"), sdk.edition(task_id="ed"):
        t = threading.Thread(target=_send_untasked, args=(captured,))
        t.start()
        t.join()
    assert captured["task"] is None  # went out un-tasked
    assert "BASELINE" in caplog.text


def test_strict_mode_raises_on_untasked_send(monkeypatch):
    monkeypatch.setenv("CTRLRTN_STRICT", "1")
    errors: list = []

    def worker():
        try:
            _send_untasked({})
        except RuntimeError as exc:  # the hook raises inside the thread
            errors.append(exc)

    import threading

    with sdk.edition(task_id="ed"):
        t = threading.Thread(target=worker)
        t.start()
        t.join()
    assert errors and "no x-ctrlrtn-task" in str(errors[0])


def test_untasked_send_outside_any_edition_is_silent(caplog):
    captured: dict = {}
    with caplog.at_level("WARNING"):
        _send_untasked(captured)  # no edition anywhere
    assert captured["task"] is None
    assert "BASELINE" not in caplog.text


def test_bind_carries_the_edition_across_a_thread():
    import threading

    captured: dict = {}
    with sdk.edition(task_id="ed-bound"):
        t = threading.Thread(target=sdk.bind(_send_untasked), args=(captured,))
        t.start()
        t.join()
    assert captured["task"] == "ed-bound"  # bind copied the context in


# --- real Anthropic SDK integration (the load-bearing path) ---------------


def test_anthropic_sdk_stamps_through_the_passed_http_client():
    # The SDK's whole point is stamping calls made THROUGH the provider SDK, not
    # a raw httpx client. Verify the real dispatch path fires the hook. Skips
    # where anthropic isn't installed; runs in CI once it's a dev dependency.
    anthropic = pytest.importorskip("anthropic")
    try:
        import httpx2 as sdk_httpx  # the Anthropic SDK from 1.0 is built on it
    except ImportError:  # older SDKs take a plain httpx client
        sdk_httpx = httpx
    captured: dict = {}

    def handler(request):
        captured["task"] = request.headers.get("x-ctrlrtn-task")
        return sdk_httpx.Response(
            200,
            json={
                "id": "msg_1",
                "type": "message",
                "role": "assistant",
                "model": "claude-haiku-4-5",
                "content": [{"type": "text", "text": "hi"}],
                "stop_reason": "end_turn",
                "usage": {"input_tokens": 1, "output_tokens": 1},
            },
        )

    client = anthropic.Anthropic(
        api_key="test",
        base_url="http://gw",
        http_client=sdk_httpx.Client(
            event_hooks=sdk.event_hooks(),
            transport=sdk_httpx.MockTransport(handler),
        ),
    )
    with sdk.edition(task_id="ed-anthropic"):
        client.messages.create(
            model="claude-haiku-4-5",
            max_tokens=8,
            messages=[{"role": "user", "content": "hi"}],
        )
    assert captured["task"] == "ed-anthropic"

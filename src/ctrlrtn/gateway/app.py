"""ASGI application: the ctrlrtn gateway.

M0 is a transparent, fail-open pass-through with optional off-hot-path
recording. Routing hooks in here later without changing the hot-path contract.
"""

from __future__ import annotations

import asyncio
import ipaddress
import logging
import math
import time
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import httpx
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import PlainTextResponse, Response
from starlette.routing import Route

from ctrlrtn.config import Settings, load_settings
from ctrlrtn.gateway.inject import cache_inject_decide
from ctrlrtn.gateway.proxy import proxy_pass_through
from ctrlrtn.gateway.serving import ExperimentRouter
from ctrlrtn.gateway.shadow import ShadowManager
from ctrlrtn.policy.budget import BudgetGate
from ctrlrtn.recorder.models import Outcome
from ctrlrtn.recorder.protocols import ExperimentStore
from ctrlrtn.recorder.recorder import Recorder
from ctrlrtn.telemetry import pricing
from ctrlrtn.workflow.identity import WorkflowEvent, WorkflowIdentityError
from ctrlrtn.workflow.tool_operation import ToolOperationEvent

_PROXY_METHODS = ["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS", "HEAD"]
# The whole /ctrlrtn/ prefix is the local control plane; never proxy it.
_CONTROL_PLANE_PREFIX = "/ctrlrtn/"
_MAX_TASK_ID_LEN = 512
logger = logging.getLogger(__name__)


async def _apply_automatic_retention(
    retention_days: int, store: ExperimentStore | None
) -> None:
    """Apply the explicit startup payload policy before accepting traffic."""
    prune = getattr(store, "prune_trace_payloads", None)
    if not callable(prune):
        raise ValueError(
            "automatic retention requires a pruning store; "
            "disable retention_days or use the production SQLite store"
        )
    cutoff = time.time() - (retention_days * 86400)
    result = await asyncio.to_thread(prune, cutoff, apply=True)
    logger.info(
        "automatic retention pruned %d trace payload(s), protected %d",
        result.pruned_traces,
        result.protected_traces,
    )


def _trusted_host(host_header: str, extra: tuple[str, ...]) -> bool:
    """True when the Host names this box the way a legitimate client would:
    ``localhost``, an IP literal, or an operator-configured name. A rebound
    attack domain is a DNS name the operator never configured."""
    host = (host_header or "").strip()
    if host.startswith("["):  # bracketed IPv6, e.g. [::1]:4000
        end = host.find("]")
        host = host[1:end] if end != -1 else ""
    else:
        host = host.split(":", 1)[0]
    if not host:
        return False
    lowered = host.lower()
    if lowered == "localhost":
        return True
    if lowered in {name.lower() for name in extra}:
        return True
    try:
        ipaddress.ip_address(host)
    except ValueError:
        return False
    return True


def create_app(
    settings: Settings | None = None,
    *,
    upstream_client: httpx.AsyncClient | None = None,
    recorder: Recorder | None = None,
    store: ExperimentStore | None = None,
    close_store_on_shutdown: bool = False,
    budget_gate: BudgetGate | None = None,
    shadow_client: httpx.AsyncClient | None = None,
) -> Starlette:
    """Build the gateway app.

    ``upstream_client`` and ``recorder`` can be injected (tests point the client
    at a mock upstream and manage the recorder); in production the client is
    built from ``settings`` and the recorder is started/stopped via lifespan.
    ``store`` (the same store the recorder writes to) enables live A/B: when
    given, an ExperimentRouter reads its running experiments and can swap the
    served model. Without it the proxy only does optional cache injection.
    """
    settings = settings or load_settings()
    if (
        budget_gate is not None
        and budget_gate.policy.reserve_in_flight
        and recorder is None
    ):
        raise ValueError("in-flight budget reservations require a recorder")
    if (
        budget_gate is not None
        and budget_gate.policy.reserve_in_flight
        and recorder is not None
    ):
        # Persistence failure must not strand an in-flight reservation. The
        # recorder enriches a discarded trace before notifying the gate, so its
        # observed cost still settles capacity even when the row cannot be saved.
        recorder.add_discard_observer(budget_gate.observe)
    owns_upstream_client = upstream_client is None
    if close_store_on_shutdown and (
        store is None or not callable(getattr(store, "close", None))
    ):
        raise TypeError("owned store must define close()")
    experiment_router = (
        ExperimentRouter(store, inject_cache=settings.inject_cache)
        if store is not None
        else None
    )
    owns_shadow_client = shadow_client is None
    # Genuine capability detection, not a gap in the contract: a store need
    # only be a ServingRepository to run the gateway. Mirroring additionally
    # requires ShadowRepository, so a serving-only store degrades to no
    # shadowing rather than failing when the first mirror is submitted.
    shadow_capable = callable(
        getattr(store, "running_shadow_experiments", None)
    )
    actual_shadow_client = (
        shadow_client or httpx.AsyncClient(timeout=settings.timeout)
        if shadow_capable and recorder is not None
        else None
    )
    shadow_manager = (
        ShadowManager(
            store,
            recorder,
            settings.resolver(),
            client=actual_shadow_client,
        )
        if shadow_capable and recorder is not None
        else None
    )

    @asynccontextmanager
    async def lifespan(app: Starlette) -> AsyncIterator[None]:
        pricing.load()  # fail the boot on a broken CTRLRTN_PRICES override
        try:
            # Inside the try so a failed startup still runs the finally and
            # doesn't leak an already-started recorder or router.
            if settings.retention_days is not None:
                await _apply_automatic_retention(settings.retention_days, store)
            if recorder is not None:
                recorder.start()
            if experiment_router is not None:
                await experiment_router.start()
            if shadow_manager is not None:
                await shadow_manager.start()
            yield
        finally:
            if shadow_manager is not None:
                await shadow_manager.aclose()
            if experiment_router is not None:
                await experiment_router.aclose()
            if recorder is not None:
                await recorder.aclose()
            if owns_upstream_client:
                await app.state.upstream_client.aclose()
            if shadow_manager is not None and owns_shadow_client:
                await actual_shadow_client.aclose()
            if close_store_on_shutdown:
                await asyncio.to_thread(store.close)

    async def healthz(request: Request) -> Response:
        return PlainTextResponse("ok")

    async def outcome(request: Request) -> Response:
        """Control-plane: the app reports a task's result so it can be joined
        against the task's cost. Handled locally — the proxy reserves the whole
        ``/ctrlrtn/`` prefix, so this never reaches an upstream. Unauthenticated:
        bind the gateway to localhost (or front it with a token); anyone who
        can reach it can report outcomes for any task. The Host check below
        additionally blocks DNS rebinding — a browser page can reach a
        loopback-bound port via a rebound domain, sidestepping CORS."""
        recorder = request.app.state.recorder
        if recorder is None:
            return PlainTextResponse("recording disabled", status_code=503)
        if not _trusted_host(
            request.headers.get("host", ""), settings.control_hosts
        ):
            return PlainTextResponse(
                "untrusted Host (DNS-rebinding guard): address the router as "
                "localhost / an IP literal, or add the name to control_hosts.",
                status_code=403,
            )
        # Require an explicit JSON content-type: this forces a CORS preflight
        # for cross-origin callers, so a drive-by site can't POST outcomes.
        if not request.headers.get("content-type", "").startswith(
            "application/json"
        ):
            return PlainTextResponse(
                "content-type must be application/json", status_code=415
            )
        try:
            body = await request.json()
        except ValueError:  # malformed JSON is a client fault; let 5xx surface
            return PlainTextResponse("invalid JSON body", status_code=400)
        if not isinstance(body, dict):
            return PlainTextResponse(
                "body must be a JSON object", status_code=400
            )
        task_id = body.get("task_id")
        if not isinstance(task_id, str) or not task_id:
            return PlainTextResponse(
                "task_id (non-empty string) is required", status_code=400
            )
        if len(task_id) > _MAX_TASK_ID_LEN:
            return PlainTextResponse(
                f"task_id too long (max {_MAX_TASK_ID_LEN})", status_code=400
            )
        success = body.get("success")
        if success is not None and not isinstance(success, bool):
            return PlainTextResponse(
                "success must be a boolean", status_code=400
            )
        score = body.get("score")
        if score is not None:
            if isinstance(score, bool) or not isinstance(score, (int, float)):
                return PlainTextResponse(
                    "score must be a number", status_code=400
                )
            try:  # reject huge ints (OverflowError) and NaN/Infinity
                score = float(score)
            except (OverflowError, ValueError):
                return PlainTextResponse(
                    "score is out of range", status_code=400
                )
            if not math.isfinite(score):
                return PlainTextResponse(
                    "score must be a finite number", status_code=400
                )
        if success is None and score is None:
            return PlainTextResponse(
                "provide success and/or score", status_code=400
            )
        await recorder.record_outcome(
            Outcome(task_id=task_id, success=success, score=score)
        )
        return PlainTextResponse("ok")

    async def proxy(request: Request) -> Response:
        if request.url.path.startswith(_CONTROL_PLANE_PREFIX):
            # Own the /ctrlrtn/ namespace locally regardless of resolver mode. In
            # single-upstream mode the resolver matches every path, so without
            # this a near-miss (trailing slash, wrong method, typo) would be
            # forwarded upstream with the client's headers — key included.
            return PlainTextResponse(
                f"no control-plane route for {request.url.path}",
                status_code=404,
            )
        if settings.kill_switch:
            return PlainTextResponse(
                "routing disabled by kill switch", status_code=503
            )
        target = request.app.state.resolver.resolve_request(request.url.path)
        if target is None:
            # No provider owns this path; fail locally rather than forward to
            # the wrong upstream (which is what produced spurious 401s).
            return PlainTextResponse(
                f"no upstream configured for {request.url.path}",
                status_code=404,
            )
        if target.path.startswith(_CONTROL_PLANE_PREFIX):
            # A named mount such as /ollama must not turn its stripped
            # /ctrlrtn/... suffix into an upstream request. The control-plane
            # namespace stays local at every mount depth.
            return PlainTextResponse(
                f"no control-plane route for {request.url.path}",
                status_code=404,
            )
        return await proxy_pass_through(
            request,
            client=request.app.state.upstream_client,
            upstream_base_url=target.base_url,
            upstream_path=target.path,
            upstream_api=target.api,
            upstream_provider=target.name,
            upstream_free=target.free,
            upstream_credential=target.credential,
            resolve_provider=request.app.state.resolver.resolve_provider,
            recorder=request.app.state.recorder,
            decide=request.app.state.request_decider,
            fallback=request.app.state.request_fallback,
            budget_gate=request.app.state.budget_gate,
            shadow_manager=request.app.state.shadow_manager,
        )

    async def workflow_event(request: Request) -> Response:
        recorder = request.app.state.recorder
        if recorder is None:
            return PlainTextResponse("recording disabled", status_code=503)
        if not _trusted_host(
            request.headers.get("host", ""), settings.control_hosts
        ):
            return PlainTextResponse(
                "untrusted Host (DNS-rebinding guard)", status_code=403
            )
        if not request.headers.get("content-type", "").startswith(
            "application/json"
        ):
            return PlainTextResponse(
                "content-type must be application/json", status_code=415
            )
        try:
            event = WorkflowEvent.from_payload(await request.json())
        except (ValueError, WorkflowIdentityError) as exc:
            return PlainTextResponse(str(exc), status_code=400)
        await recorder.record_workflow_event(event)
        return PlainTextResponse("ok")

    async def tool_operation_event(request: Request) -> Response:
        recorder = request.app.state.recorder
        if recorder is None:
            return PlainTextResponse("recording disabled", status_code=503)
        if not _trusted_host(
            request.headers.get("host", ""), settings.control_hosts
        ):
            return PlainTextResponse(
                "untrusted Host (DNS-rebinding guard)", status_code=403
            )
        if not request.headers.get("content-type", "").startswith(
            "application/json"
        ):
            return PlainTextResponse(
                "content-type must be application/json", status_code=415
            )
        try:
            event = ToolOperationEvent.from_payload(await request.json())
        except (ValueError, WorkflowIdentityError) as exc:
            return PlainTextResponse(str(exc), status_code=400)
        await recorder.record_tool_operation_event(event)
        return PlainTextResponse("ok")

    app = Starlette(
        routes=[
            Route("/healthz", healthz, methods=["GET"]),
            Route("/ctrlrtn/outcome", outcome, methods=["POST"]),
            Route("/ctrlrtn/workflow-events", workflow_event, methods=["POST"]),
            Route(
                "/ctrlrtn/tool-operation-events",
                tool_operation_event,
                methods=["POST"],
            ),
            Route("/{path:path}", proxy, methods=_PROXY_METHODS),
        ],
        lifespan=lifespan,
    )
    app.state.resolver = settings.resolver()
    app.state.upstream_client = upstream_client or httpx.AsyncClient(
        timeout=settings.timeout
    )
    app.state.recorder = recorder
    app.state.budget_gate = budget_gate
    app.state.shadow_manager = shadow_manager
    # The request hook; None keeps the proxy byte-faithful. With a store, the
    # ExperimentRouter owns it (live A/B model swap + optional cache injection);
    # without one, just optional cache injection.
    app.state.experiment_router = experiment_router
    if experiment_router is not None:
        app.state.request_decider = experiment_router.decide
        app.state.request_fallback = experiment_router.fallback
    else:
        app.state.request_decider = (
            cache_inject_decide if settings.inject_cache else None
        )
        app.state.request_fallback = None
    return app

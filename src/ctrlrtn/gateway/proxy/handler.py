"""Byte-faithful streaming orchestration for the selected upstream."""

from __future__ import annotations

import logging
import time
from typing import TYPE_CHECKING, AsyncIterator

import httpx
from starlette.background import BackgroundTask
from starlette.requests import Request
from starlette.responses import JSONResponse, Response, StreamingResponse

from ctrlrtn.gateway.proxy.headers import (
    _forward_request_headers,
    _forward_response_headers,
    _recordable_headers,
)
from ctrlrtn.gateway.proxy.models import (
    _CEILING_RETRY_AFTER_SECONDS,
    DecideFn,
    FallbackFn,
    ProviderResolveFn,
    TerminalError,
)
from ctrlrtn.gateway.proxy.recording import (
    _record_stream,
    _record_synthetic,
)
from ctrlrtn.gateway.redact import strip_query_credentials
from ctrlrtn.recorder.redaction import redact_query

if TYPE_CHECKING:
    from ctrlrtn.gateway.decision import ServingDecision
    from ctrlrtn.gateway.shadow import ShadowManager
    from ctrlrtn.policy.budget import BudgetGate
    from ctrlrtn.recorder.recorder import Recorder
    from ctrlrtn.routing import ProviderCredential

logger = logging.getLogger(__name__)


async def proxy_pass_through(
    request: Request,
    *,
    client: httpx.AsyncClient,
    upstream_base_url: str,
    upstream_path: str | None = None,
    upstream_api: str | None = None,
    upstream_provider: str | None = None,
    upstream_free: bool = False,
    upstream_credential: "ProviderCredential | None" = None,
    resolve_provider: ProviderResolveFn | None = None,
    recorder: "Recorder | None" = None,
    decide: DecideFn | None = None,
    fallback: FallbackFn | None = None,
    budget_gate: "BudgetGate | None" = None,
    shadow_manager: "ShadowManager | None" = None,
) -> Response:
    """Forward ``request`` to the upstream provider and stream the response.

    ``decide`` (opt-in) inspects the request and may rewrite its body before it
    is forwarded — inject a prompt-cache breakpoint, or swap the model for a live
    A/B candidate — and returns an optional ``ServeDecision`` describing the arm
    it served. We record the *original* body so the recorded request stays the
    app's own intent and its fingerprint is stable; only the upstream sees the
    rewritten bytes, and the arm is recorded alongside. ``decide`` must be
    fail-open (a raise forwards the original body with no arm)."""
    started = time.monotonic()
    method = request.method
    path = request.url.path
    forward_path = upstream_path or path
    selected_base_url = upstream_base_url
    selected_api = upstream_api
    selected_provider = upstream_provider
    selected_free = upstream_free
    selected_credential = upstream_credential
    provider_switched = False
    budget_reservation_id: int | None = None
    forward_query = request.url.query
    # The RECORDED copies are redacted; the forwarded request is untouched.
    query = redact_query(forward_query)
    request_headers = _recordable_headers(request.headers)
    request_body = await request.body()

    forward_body = request_body
    serve: "ServingDecision | None" = None
    if decide is not None:
        try:
            # Pass the case-insensitive Headers (not the lowercased dict) so a
            # hook can look up x-ctrlrtn-task regardless of client casing.
            forward_body, serve = decide(
                forward_path, request.headers, request_body, upstream_api
            )
            if not isinstance(forward_body, (bytes, bytearray)):
                # A hook that returns a non-bytes body — e.g. (None, decision) —
                # would forward an empty/garbage request while recording the
                # original (and, in A/B, log a candidate arm that never served).
                # Treat it as a failure: forward the original, assign no arm.
                raise TypeError("decide must return bytes as the forward body")
        except TerminalError as terminal:
            # A divergence-ceiling breach: do NOT forward. Record the arm as a
            # counted failure (once — the router flags repeats) and return the
            # terminal status. Retry-After tells a well-behaved client to back off
            # rather than hammer the terminal (429 is otherwise auto-retried).
            if recorder is not None and terminal.record:
                _record_synthetic(
                    recorder,
                    terminal.serve,
                    terminal.status,
                    started,
                    reason="ceiling",
                    method=method,
                    path=path,
                    query=query,
                    request_headers=request_headers,
                    request_body=request_body,
                    provider=upstream_provider,
                    provider_free=upstream_free,
                    note=terminal.message.encode("utf-8"),
                )
            return JSONResponse(
                {
                    "error": {
                        "type": "ctrlrtn_divergence_ceiling",
                        "message": terminal.message,
                    }
                },
                status_code=terminal.status,
                headers={"Retry-After": str(_CEILING_RETRY_AFTER_SECONDS)},
            )
        except Exception:  # any other hook error must never break a call
            logger.exception("request decide failed; forwarding original")
            forward_body, serve = request_body, None

    provider_override = getattr(serve, "provider", None)
    if provider_override is not None:
        override = (
            resolve_provider(provider_override, forward_path)
            if resolve_provider is not None
            else None
        )
        compatible = (
            override is not None
            and selected_api is not None
            and override.api == selected_api
        )
        if not compatible:
            if recorder is not None and serve is not None:
                _record_synthetic(
                    recorder,
                    serve,
                    502,
                    started,
                    reason="provider",
                    method=method,
                    path=path,
                    query=query,
                    request_headers=request_headers,
                    request_body=request_body,
                    provider=(
                        override.name
                        if override is not None
                        else provider_override
                    ),
                    provider_free=(
                        override.free if override is not None else False
                    ),
                    note=b"candidate provider is missing or API-incompatible",
                )
            return JSONResponse(
                {
                    "error": {
                        "type": "ctrlrtn_provider_mismatch",
                        "message": "candidate provider is missing or uses a "
                        "different API",
                    }
                },
                status_code=502,
            )
        selected_base_url = override.base_url
        selected_api = override.api
        selected_provider = override.name
        selected_free = override.free
        selected_credential = override.credential
        provider_switched = selected_provider != upstream_provider

    if budget_gate is not None:
        budget = budget_gate.check(
            request.headers,
            request_body,
            bytes(forward_body),
            provider_free=selected_free,
        )
        if budget.error_type == "ctrlrtn_budget_fallback_required":
            fallback_body: bytes | bytearray = bytes(forward_body)
            fallback_serve: "ServingDecision | None" = None
            if fallback is not None and serve is None:
                try:
                    fallback_body, fallback_serve = fallback(
                        forward_path,
                        request.headers,
                        request_body,
                        bytes(forward_body),
                        upstream_api,
                    )
                    if not isinstance(fallback_body, (bytes, bytearray)):
                        raise TypeError("fallback must return bytes")
                except Exception:
                    logger.exception("budget fallback resolution failed")
                    fallback_body, fallback_serve = bytes(forward_body), None
            if fallback_serve is not None:
                fallback_provider = getattr(fallback_serve, "provider", None)
                fallback_override = (
                    resolve_provider(fallback_provider, forward_path)
                    if fallback_provider is not None
                    and resolve_provider is not None
                    else None
                )
                compatible = fallback_provider is None or (
                    fallback_override is not None
                    and upstream_api is not None
                    and fallback_override.api == upstream_api
                )
                if compatible:
                    forward_body = fallback_body
                    serve = fallback_serve
                    if fallback_override is not None:
                        selected_base_url = fallback_override.base_url
                        selected_api = fallback_override.api
                        selected_provider = fallback_override.name
                        selected_free = fallback_override.free
                        selected_credential = fallback_override.credential
                        provider_switched = (
                            selected_provider != upstream_provider
                        )
                    budget = budget_gate.check(
                        request.headers,
                        request_body,
                        bytes(forward_body),
                        provider_free=selected_free,
                        fallback_applied=True,
                    )
        if not budget.allowed:
            if budget.error_type in {
                "ctrlrtn_unpriced_model",
                "ctrlrtn_unknown_session_cost",
            }:
                status = 503
                if budget.error_type == "ctrlrtn_unpriced_model":
                    reason = "unpriced_model"
                    message = "model price is unknown under an active budget"
                else:
                    reason = "unknown_session_cost"
                    message = "session contains calls with unknown cost"
            elif budget.error_type == "ctrlrtn_unreservable_request":
                status = 400
                reason = "unreservable_request"
                message = (
                    "request needs a valid max_tokens or "
                    "max_completion_tokens under reservation policy"
                )
            elif budget.error_type == "ctrlrtn_session_required":
                status = 400
                reason = "session_required"
                message = (
                    "x-ctrlrtn-session is required by session budget policy"
                )
            elif budget.error_type == "ctrlrtn_budget_fallback_required":
                status = 429
                reason = "fallback_unavailable"
                message = (
                    "budget fallback has no applicable NON_INFERIOR evidence"
                )
            else:
                status = 429
                reason = "budget"
                message = (
                    "session budget exceeded"
                    if (budget.scope or "").startswith("session:")
                    else "daily budget exceeded"
                )
            if recorder is not None:
                _record_synthetic(
                    recorder,
                    serve,
                    status,
                    started,
                    reason=reason,
                    method=method,
                    path=path,
                    query=query,
                    request_headers=request_headers,
                    request_body=request_body,
                    provider=selected_provider,
                    provider_free=selected_free,
                    note=(budget.error_type or "budget rejected").encode(),
                )
            detail = {
                "type": (
                    "ctrlrtn_budget_fallback_unavailable"
                    if budget.error_type == "ctrlrtn_budget_fallback_required"
                    else budget.error_type
                ),
                "message": message,
            }
            if budget.scope is not None:
                detail.update(
                    {
                        "scope": budget.scope,
                        "spent_usd": budget.spent_usd,
                        "limit_usd": budget.limit_usd,
                    }
                )
            return JSONResponse({"error": detail}, status_code=status)
        budget_reservation_id = budget.reservation_id

    credentials_replaced = provider_switched or selected_credential is not None
    try:
        provider_headers = (
            selected_credential.headers() if selected_credential else {}
        )
    except ValueError:
        logger.error(
            "provider credential is unavailable for %s", selected_provider
        )
        if recorder is not None and (
            serve is not None or budget_reservation_id is not None
        ):
            _record_synthetic(
                recorder,
                serve,
                503,
                started,
                reason="credential",
                method=method,
                path=path,
                query=query,
                request_headers=request_headers,
                request_body=request_body,
                provider=selected_provider,
                provider_free=selected_free,
                note=b"provider credential is unavailable",
                budget_reservation_id=budget_reservation_id,
            )
        return JSONResponse(
            {
                "error": {
                    "type": "ctrlrtn_provider_credential_unavailable",
                    "message": "provider credential is unavailable",
                }
            },
            status_code=503,
        )

    target = selected_base_url.rstrip("/") + forward_path
    upstream_query = (
        strip_query_credentials(forward_query)
        if credentials_replaced
        else forward_query
    )
    if upstream_query:
        target += "?" + upstream_query

    forward_headers = _forward_request_headers(
        request.headers, strip_credentials=credentials_replaced
    )
    forward_headers.update(provider_headers)
    try:
        upstream_request = client.build_request(
            method=method,
            url=target,
            headers=forward_headers,
            content=forward_body,
        )
        upstream_response = await client.send(upstream_request, stream=True)
    except Exception:
        # An arm call that fails before it streams must still record the arm,
        # else a candidate call vanishes with no trace (a silent MNAR
        # confounder). Non-blocking so an upstream outage (every send failing)
        # can't pile every failing call onto the recorder queue. Then propagate.
        if recorder is not None and (
            serve is not None or budget_reservation_id is not None
        ):
            _record_synthetic(
                recorder,
                serve,
                502,
                started,
                reason="send_failure",
                method=method,
                path=path,
                query=query,
                request_headers=request_headers,
                request_body=request_body,
                provider=selected_provider,
                provider_free=selected_free,
                note=b"upstream send failed",
                budget_reservation_id=budget_reservation_id,
            )
        raise

    shadow_pair = None
    if shadow_manager is not None:
        try:
            # Mirror only after the actual request passed every local admission
            # check and the upstream accepted the send. A locally rejected or
            # failed request must never spend on an orphan candidate call.
            shadow_pair = shadow_manager.submit(
                method=method,
                path=path,
                provider_path=forward_path,
                query=forward_query,
                headers=request.headers,
                body=request_body,
                baseline_base_url=selected_base_url,
                baseline_api=selected_api,
                baseline_provider=selected_provider,
                baseline_free=selected_free,
                baseline_credential=selected_credential,
            )
        except Exception:
            logger.exception("shadow admission failed")

    if recorder is None:
        stream: AsyncIterator[bytes] = upstream_response.aiter_raw()
    else:
        stream = _record_stream(
            upstream_response,
            recorder,
            started,
            method=method,
            path=path,
            query=query,
            request_headers=request_headers,
            request_body=request_body,
            provider=selected_provider,
            provider_free=selected_free,
            serve=serve,
            budget_reservation_id=budget_reservation_id,
            shadow_pair=shadow_pair,
        )

    response = StreamingResponse(
        stream,
        status_code=upstream_response.status_code,
        background=BackgroundTask(upstream_response.aclose),
    )
    # Starlette's mapping-based header constructor coalesces duplicate fields.
    # Assign filtered raw fields so Set-Cookie and other repeatable headers
    # retain their independent wire semantics.
    response.raw_headers = list(
        _forward_response_headers(upstream_response.headers).raw
    )
    return response

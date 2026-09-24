"""Streaming and synthetic trace recording for the gateway proxy."""

from __future__ import annotations

import logging
import time
from typing import TYPE_CHECKING, AsyncIterator

import httpx

from ctrlrtn.gateway.proxy.headers import (
    _forward_response_headers,
    _recordable_headers,
)
from ctrlrtn.recorder.trace import Trace

if TYPE_CHECKING:
    from ctrlrtn.gateway.decision import ServingDecision
    from ctrlrtn.gateway.shadow import ShadowPair
    from ctrlrtn.recorder.recorder import Recorder

logger = logging.getLogger(__name__)


async def _record_stream(
    upstream_response: httpx.Response,
    recorder: "Recorder",
    started: float,
    *,
    method: str,
    path: str,
    query: str,
    request_headers: dict[str, str],
    request_body: bytes,
    provider: str | None,
    provider_free: bool,
    serve: ServingDecision | None,
    budget_reservation_id: int | None,
    shadow_pair: "ShadowPair | None",
) -> AsyncIterator[bytes]:
    """Forward the upstream body to the client while teeing a copy, then
    enqueue the trace once the stream completes."""
    chunks: list[bytes] = []
    try:
        async for chunk in upstream_response.aiter_raw():
            chunks.append(chunk)
            yield chunk
    finally:
        # Runs even on client disconnect (partial body captured). Recording
        # must never raise into the response.
        trace = Trace(
            method=method,
            path=path,
            query=query,
            request_headers=request_headers,
            request_body=request_body,
            status_code=upstream_response.status_code,
            response_headers=_recordable_headers(upstream_response.headers),
            response_body=b"".join(chunks),
            latency_ms=(time.monotonic() - started) * 1000.0,
            provider=provider,
            provider_free=provider_free,
            experiment_id=serve.experiment_id if serve else None,
            arm=serve.arm if serve else None,
            served_model=serve.served_model if serve else None,
            route_rule_scope=(
                getattr(serve, "rule_scope", None) if serve else None
            ),
            route_rule_key=(
                getattr(serve, "rule_key", None) if serve else None
            ),
            control_revision=(
                getattr(serve, "control_revision", None) if serve else None
            ),
            budget_fallback=bool(
                serve is not None
                and getattr(serve, "is_budget_fallback", False)
            ),
            budget_reservation_id=budget_reservation_id,
            shadow_experiment_id=(
                shadow_pair.shadow_id if shadow_pair else None
            ),
            shadow_pair_id=(shadow_pair.pair_id if shadow_pair else None),
            shadow_role="actual" if shadow_pair else None,
        )
        # This finally runs after the client has the full response, so awaiting a
        # queue slot for a candidate trace can't delay the client.
        try:
            await _enqueue_trace(recorder, trace, serve)
        except Exception:
            logger.exception("failed to record trace")


async def _enqueue_trace(
    recorder: "Recorder", trace: Trace, serve: ServingDecision | None
) -> None:
    """Use bounded backpressure for traces that cannot safely disappear.

    A lost candidate is an MNAR confounder; a lost reservation settlement
    strands capacity until restart. Other traces take the non-blocking path.
    """
    if (
        serve is not None and serve.is_candidate
    ) or trace.budget_reservation_id is not None:
        await recorder.enqueue_important(trace)
    else:
        recorder.enqueue(trace)


def _record_synthetic(
    recorder: "Recorder",
    serve: ServingDecision | None,
    status: int,
    started: float,
    *,
    reason: str,
    method: str,
    path: str,
    query: str,
    request_headers: dict[str, str],
    request_body: bytes,
    provider: str | None,
    provider_free: bool,
    note: bytes = b"",
    budget_reservation_id: int | None = None,
) -> None:
    """Record a router terminal that never reached or completed upstream.

    Experiment fields are included when an arm was assigned. ``terminal_reason``
    keeps every local rejection distinguishable from a genuine upstream error.

    This is called ON the client's response path (before the terminal/error is
    returned), so it uses the NON-blocking enqueue: a divergence terminal or a
    send failure must never gate the client's response on the recorder queue, and
    under an upstream outage every failing call would otherwise pile up on a
    blocking put. A drop here is counted in ``recorder.dropped``."""
    trace = Trace(
        method=method,
        path=path,
        query=query,
        request_headers=request_headers,
        request_body=request_body,
        status_code=status,
        response_headers={},
        response_body=note,
        latency_ms=(time.monotonic() - started) * 1000.0,
        provider=provider,
        provider_free=provider_free,
        experiment_id=serve.experiment_id if serve else None,
        arm=serve.arm if serve else None,
        served_model=serve.served_model if serve else None,
        route_rule_scope=(
            getattr(serve, "rule_scope", None) if serve else None
        ),
        route_rule_key=(getattr(serve, "rule_key", None) if serve else None),
        control_revision=(
            getattr(serve, "control_revision", None) if serve else None
        ),
        budget_fallback=bool(
            serve is not None and getattr(serve, "is_budget_fallback", False)
        ),
        terminal_reason=reason,
        budget_reservation_id=budget_reservation_id,
    )
    try:
        recorder.enqueue(trace)
    except Exception:
        logger.exception("failed to record synthetic trace")

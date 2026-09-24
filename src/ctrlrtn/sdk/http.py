"""httpx clients that stamp requests with the active SDK context."""

from __future__ import annotations

import httpx

from ctrlrtn.sdk.context import (
    _ROUTE_HEADER,
    _TASK_HEADER,
    _route,
    _step_identity,
    _strict,
    _task,
    logger,
)
from ctrlrtn.sdk.lifecycle import _edition_active


def _warn_or_raise_untasked() -> None:
    """A ctrlrtn client is sending a request with no task bound. If an edition is
    live in this process, the call is almost certainly on a thread/subprocess the
    context didn't reach and will be misfiled as baseline — surface it loudly.
    """
    if not _edition_active():
        return  # legitimately outside any edition — nothing to stamp
    msg = (
        "ctrlrtn: an LLM request is going out with no x-ctrlrtn-task while an "
        "edition is active in this process — it is on a thread/subprocess the "
        "edition context did not reach and will be recorded as BASELINE, "
        "silently splitting the experiment. Wrap the callable with sdk.bind "
        "before crossing the boundary."
    )
    if _strict():
        raise RuntimeError(msg)
    logger.warning(msg)


def _stamp_request(request: httpx.Request) -> None:
    tid = _task.get()
    if not tid:
        _warn_or_raise_untasked()
        return
    request.headers[_TASK_HEADER] = tid
    rt = _route.get()
    if rt:
        request.headers.setdefault(_ROUTE_HEADER, rt)
    identity = _step_identity.get()
    if identity is not None:
        for name, value in identity.headers().items():
            request.headers[name] = value


async def _astamp_request(request: httpx.Request) -> None:
    _stamp_request(request)


def _with_hook(hooks: dict | None, hook) -> dict:
    hooks = dict(hooks or {})
    hooks["request"] = [*hooks.get("request", []), hook]
    return hooks


def event_hooks(extra: dict | None = None) -> dict:
    """Request hooks that stamp the current edition's headers, for a client
    this module does not build itself.

    Provider SDKs built on ``httpx2`` (the Anthropic SDK from 1.0) reject an
    ``httpx.Client``; give them their own client with these hooks instead::

        anthropic.Anthropic(
            http_client=httpx2.Client(event_hooks=sdk.event_hooks())
        )

    ``extra`` merges hooks of your own; the stamping hook runs last.
    """
    return _with_hook(extra, _stamp_request)


def async_event_hooks(extra: dict | None = None) -> dict:
    """The async counterpart of :func:`event_hooks`."""
    return _with_hook(extra, _astamp_request)


def http_client(**kwargs) -> httpx.Client:
    """A sync httpx client that stamps the current edition's headers on every
    request. Pass it to your LLM SDK, e.g. ``OpenAI(http_client=...)``."""
    return httpx.Client(
        event_hooks=event_hooks(kwargs.pop("event_hooks", None)), **kwargs
    )


def async_http_client(**kwargs) -> httpx.AsyncClient:
    """An async httpx client that stamps the current edition's headers."""
    return httpx.AsyncClient(
        event_hooks=async_event_hooks(kwargs.pop("event_hooks", None)),
        **kwargs,
    )

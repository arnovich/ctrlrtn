"""Proxy contracts and terminal request errors."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from ctrlrtn.gateway.decision import ServingDecision
    from ctrlrtn.routing import ResolvedUpstream

# A ceiling breach won't relent for the life of the task, so tell a retrying
# client to back off a long time rather than hammer the terminal.
_CEILING_RETRY_AFTER_SECONDS = 3600

# The request hook: given (provider-facing path, case-insensitive request
# headers, original body, resolved provider API), return the bytes to forward
# upstream and an optional ServeDecision (a live A/B arm assignment, or None
# when no experiment applies). The proxy
# forwards the original body unchanged if this raises OR returns a non-bytes
# body. Contract: decide MUST be non-blocking and CPU-only — no I/O (a slice-4
# arm lookup reads an in-memory snapshot, never a sync DB/file/network call) —
# and side-effect-free until it returns (the arm is recorded only via the
# returned ServeDecision, never as an internal write).
DecideFn = Callable[
    [str, Mapping[str, str], bytes, str | None],
    "tuple[bytes, ServingDecision | None]",
]
FallbackFn = Callable[
    [str, Mapping[str, str], bytes, bytes, str | None],
    "tuple[bytes, ServingDecision | None]",
]
ProviderResolveFn = Callable[[str, str], "ResolvedUpstream | None"]


class TerminalError(Exception):
    """A decide() hook raises this to ABORT the request instead of forwarding.

    The proxy returns ``status`` to the client and records a counted-failure
    Trace carrying the arm (so the failure is *counted*, not dropped). Used when
    a candidate arm breaches its per-task divergence ceiling: a runaway candidate
    must become a visible failure, never vanish into 'unreported' (the MNAR
    trap). Distinct from a generic hook error, which fails open (forwards the
    original request).
    """

    def __init__(
        self,
        status: int,
        message: str,
        serve: ServingDecision,
        *,
        record: bool = True,
    ) -> None:
        super().__init__(message)
        self.status = status
        self.message = message
        self.serve = serve
        # False on a repeat breach the router has already recorded — the proxy
        # then returns the status without re-recording (one runaway = one
        # counted failure, even if the client retries the terminal).
        self.record = record

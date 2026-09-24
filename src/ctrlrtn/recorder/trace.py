"""The unit of recording: one request/response round trip."""

from __future__ import annotations

import time
from dataclasses import dataclass, field


@dataclass
class Trace:
    """A single proxied call. The fields after ``latency_ms`` are filled by the
    enrichment step (fingerprint + model + usage) in the recorder worker, off
    the hot path — except the ``experiment_id``/``arm``/``served_model`` trio and
    ``terminal_reason``, which the proxy sets on the hot path from a live A/B
    ``ServeDecision`` (None when no experiment applies). ``model`` stays the
    *requested* model (from the recorded original body); ``served_model`` is what
    was actually sent. ``terminal_reason`` marks a router-imposed terminal such
    as a divergence ceiling, budget rejection, or upstream send failure, so a
    later analysis can tell these counted failures apart from genuine upstream
    responses (which have no reason)."""

    method: str
    path: str
    query: str
    request_headers: dict[str, str]
    request_body: bytes
    status_code: int
    response_headers: dict[str, str]
    response_body: bytes
    latency_ms: float
    provider: str | None = None
    provider_free: bool = False
    model: str | None = None
    input_tokens: int | None = None
    output_tokens: int | None = None
    cache_read_tokens: int | None = None
    cache_write_tokens: int | None = None
    cost_usd: float | None = None
    use_case_key: str | None = None
    task_id: str | None = None
    session_id: str | None = None
    workflow: str | None = None
    workflow_version: str | None = None
    step: str | None = None
    step_run_id: str | None = None
    parent_step_run_id: str | None = None
    dependency_step_run_ids: tuple[str, ...] = ()
    step_attempt: int | None = None
    workflow_identity_error: str | None = None
    experiment_id: str | None = None
    arm: str | None = None
    served_model: str | None = None
    route_rule_scope: str | None = None
    route_rule_key: str | None = None
    control_revision: str | None = None
    shadow_experiment_id: str | None = None
    shadow_pair_id: str | None = None
    shadow_role: str | None = None
    budget_fallback: bool = False
    terminal_reason: str | None = None
    # Process-local admission fact used only until recorder reconciliation; it
    # is deliberately not persisted in SQLite.
    budget_reservation_id: int | None = field(
        default=None, repr=False, compare=False
    )
    ts: float = field(default_factory=time.time)

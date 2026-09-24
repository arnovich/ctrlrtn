"""Off-hot-path enrichment: derive the use-case key, model, token usage, and
cost for a recorded trace before it is persisted. Run by the recorder worker."""

from __future__ import annotations

from ctrlrtn.identify.fingerprint import fingerprint_trace
from ctrlrtn.recorder.trace import Trace
from ctrlrtn.telemetry.pricing import cost_usd
from ctrlrtn.telemetry.usage import (
    decode_body,
    extract_model,
    extract_response_model,
    extract_usage,
)
from ctrlrtn.workflow.identity import identity_from_headers


def enrich_trace(trace: Trace) -> None:
    trace.use_case_key = fingerprint_trace(trace)
    # Header keys are lowercased at capture (the proxy stores
    # dict(request.headers); the ASGI server lowercases names), so this
    # matches any client casing. Empty identity values normalize to None so
    # both stores place them in their explicit untagged buckets.
    trace.task_id = trace.request_headers.get("x-ctrlrtn-task") or None
    trace.session_id = trace.request_headers.get("x-ctrlrtn-session") or None
    identity, error = identity_from_headers(trace.request_headers)
    trace.workflow_identity_error = error
    if identity is not None:
        trace.workflow = identity.workflow
        trace.workflow_version = identity.workflow_version
        trace.step = identity.step
        trace.step_run_id = identity.step_run_id
        trace.parent_step_run_id = identity.parent_step_run_id
        trace.dependency_step_run_ids = identity.dependency_step_run_ids
        trace.step_attempt = identity.attempt
    trace.model = extract_model(trace.request_body)
    encoding = trace.response_headers.get("content-encoding")
    decoded_response = decode_body(trace.response_body, encoding)
    usage = extract_usage(decoded_response)
    trace.input_tokens = usage.input_tokens
    trace.output_tokens = usage.output_tokens
    trace.cache_read_tokens = usage.cache_read_tokens
    trace.cache_write_tokens = usage.cache_write_tokens
    # Price on the model actually SERVED (billed), not the requested one (which
    # ``trace.model`` keeps, for display). Prefer the model the provider echoed
    # in the response — ground truth, recomputable from stored bytes on reenrich,
    # and correct even if the provider served an alias/fallback we never asked
    # for. Fall back to the A/B served_model (what we swapped to), then requested.
    priced_model = (
        extract_response_model(decoded_response)
        or trace.served_model
        or trace.model
    )
    trace.cost_usd = cost_usd(priced_model, usage, free=trace.provider_free)

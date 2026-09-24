"""Explicit adapter boundary for safely attributable tool batching."""

from __future__ import annotations

import math
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Protocol

from ctrlrtn.workflow.tool_operation import ToolOperationIdentity


class ToolBatchError(ValueError):
    """Raised when a batch capability contract or item result is malformed."""

    pass


@dataclass(frozen=True)
class ToolBatchCapabilities:
    """An adapter's self-declared guarantees for safely batching tool calls.

    Batching runs only when every guarantee flag is true, so an adapter
    that cannot preserve order, keep per-operation idempotency, scope
    authentication, account for rate limits, attribute partial failures
    and cancel cooperatively is bypassed before dispatch. Declarations are
    trusted as stated; ctrlrtn never infers them.
    """

    adapter_id: str
    max_batch_size: int
    preserves_order: bool
    per_operation_idempotency: bool
    scoped_authentication: bool
    rate_limit_accounting: bool
    attributed_partial_failures: bool
    cooperative_cancellation: bool

    def __post_init__(self) -> None:
        if not isinstance(self.adapter_id, str) or not self.adapter_id:
            raise ToolBatchError("tool batch adapter id must be non-empty")
        if (
            isinstance(self.max_batch_size, bool)
            or not isinstance(self.max_batch_size, int)
            or self.max_batch_size < 2
        ):
            raise ToolBatchError("tool adapter batch size must be at least two")


@dataclass(frozen=True)
class ToolBatchOperation:
    """One explicit tool attempt and its arguments offered for batching.

    The identity's effect contract decides eligibility: only ``pure`` or
    ``idempotent`` operations may be batched.
    """

    identity: ToolOperationIdentity
    arguments: Mapping[str, object]


@dataclass(frozen=True)
class ToolBatchItemResult:
    """An adapter's outcome for exactly one attempt inside a batch.

    ``attempt_id`` must echo the operation's attempt so partial failures
    stay attributed; a failed item requires an ``error_code``.
    """

    attempt_id: str
    success: bool
    value: object | None = None
    error_code: str | None = None
    latency_ms: float | None = None
    cost_usd: float | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.attempt_id, str) or not self.attempt_id:
            raise ToolBatchError("batch result attempt id must be non-empty")
        if not isinstance(self.success, bool):
            raise ToolBatchError("batch result success must be boolean")
        if not self.success and not self.error_code:
            raise ToolBatchError("failed batch result requires an error code")
        for label, value in (
            ("latency", self.latency_ms),
            ("cost", self.cost_usd),
        ):
            if value is not None and (
                isinstance(value, bool)
                or not isinstance(value, (int, float))
                or not math.isfinite(value)
                or value < 0
            ):
                raise ToolBatchError(
                    f"batch result {label} must be finite and non-negative"
                )


@dataclass(frozen=True)
class ToolBatchResult:
    """What ``run_tool_batch`` did and why.

    ``mode`` is ``bypass`` (the original callable ran instead, before any
    dispatch), ``batched`` (every item succeeded), ``partial`` (some items
    failed but stayed attributed) or ``needs_operator`` (the adapter's
    result could not be trusted). After dispatch the original is never
    replayed, so ``value`` is the bypass result, the items, or ``None``.
    """

    mode: str
    value: object
    reason: str
    items: tuple[ToolBatchItemResult, ...] = ()


class ToolBatchAdapter(Protocol):
    """Protocol an application-supplied batching adapter must satisfy.

    It publishes validated ``capabilities`` and executes one batch,
    returning one result per operation in the same order.
    """

    capabilities: ToolBatchCapabilities

    def execute_batch(
        self, operations: tuple[ToolBatchOperation, ...]
    ) -> tuple[ToolBatchItemResult, ...]: ...


def _safe_capabilities(capabilities: ToolBatchCapabilities) -> bool:
    return all(
        (
            capabilities.preserves_order,
            capabilities.per_operation_idempotency,
            capabilities.scoped_authentication,
            capabilities.rate_limit_accounting,
            capabilities.attributed_partial_failures,
            capabilities.cooperative_cancellation,
        )
    )


def run_tool_batch(
    adapter: ToolBatchAdapter,
    operations: tuple[ToolBatchOperation, ...],
    original: Callable[[], object],
    *,
    opt_in: bool,
) -> ToolBatchResult:
    """Batch only before dispatch; never hide or replay a partial dispatch."""

    def bypass(reason: str) -> ToolBatchResult:
        return ToolBatchResult("bypass", original(), reason)

    if opt_in is not True:
        return bypass("application opt-in is disabled")
    capabilities = getattr(adapter, "capabilities", None)
    if not isinstance(capabilities, ToolBatchCapabilities):
        return bypass("adapter has no validated batch capability contract")
    if not _safe_capabilities(capabilities):
        return bypass("adapter batch capability contract is incomplete")
    if not 2 <= len(operations) <= capabilities.max_batch_size:
        return bypass("operation count is outside adapter batch bounds")
    attempt_ids = [operation.identity.attempt_id for operation in operations]
    operation_ids = [
        operation.identity.operation_id for operation in operations
    ]
    if len(set(attempt_ids)) != len(attempt_ids):
        return bypass("tool attempt identities must be unique")
    if len(set(operation_ids)) != len(operation_ids):
        return bypass("logical tool operation identities must be unique")
    workflow_steps = {
        (
            operation.identity.workflow_identity.task_id,
            operation.identity.workflow_identity.step_run_id,
        )
        for operation in operations
    }
    if len(workflow_steps) != 1:
        return bypass("a batch cannot cross task or step-run identity")
    if any(
        operation.identity.effect not in {"pure", "idempotent"}
        for operation in operations
    ):
        return bypass("unknown or stateful tool operations cannot be batched")
    if any(
        not isinstance(operation.arguments, Mapping) for operation in operations
    ):
        return bypass("tool batch arguments must be mappings")

    try:
        items = tuple(adapter.execute_batch(operations))
    except Exception as exc:
        return ToolBatchResult(
            "needs_operator",
            None,
            f"adapter dispatch failed: {type(exc).__name__}; original was not replayed",
        )
    returned = [item.attempt_id for item in items]
    if returned != attempt_ids or len(set(returned)) != len(returned):
        return ToolBatchResult(
            "needs_operator",
            None,
            "adapter result attribution is incomplete or reordered; original was not replayed",
            items,
        )
    if any(not isinstance(item.success, bool) for item in items):
        return ToolBatchResult(
            "needs_operator",
            None,
            "adapter returned an invalid per-operation outcome",
            items,
        )
    if any(not item.success for item in items):
        return ToolBatchResult(
            "partial",
            items,
            "partial failure retained per-operation attribution; original was not replayed",
            items,
        )
    return ToolBatchResult("batched", items, "adapter batch completed", items)

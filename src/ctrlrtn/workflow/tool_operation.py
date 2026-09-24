"""Authoritative, provider-independent tool operation lifecycle facts."""

from __future__ import annotations

import math
import time
import uuid
from dataclasses import dataclass, field
from typing import Mapping

from ctrlrtn.workflow.identity import (
    WorkflowIdentity,
    WorkflowIdentityError,
    _identifier,
)

EFFECTS = frozenset({"unknown", "pure", "idempotent", "stateful"})
STATUSES = frozenset({"started", "completed", "failed", "cancelled"})
TERMINAL_STATUSES = STATUSES - {"started"}


@dataclass(frozen=True)
class ToolOperationIdentity:
    workflow_identity: WorkflowIdentity
    operation: str
    operation_id: str
    attempt_id: str
    attempt: int = 1
    effect: str = "unknown"

    def __post_init__(self) -> None:
        _identifier("tool operation", self.operation)
        _identifier("tool operation id", self.operation_id)
        _identifier("tool attempt id", self.attempt_id)
        if (
            isinstance(self.attempt, bool)
            or not isinstance(self.attempt, int)
            or self.attempt < 1
        ):
            raise WorkflowIdentityError(
                "tool attempt must be a positive integer"
            )
        if self.effect not in EFFECTS:
            raise WorkflowIdentityError("tool effect contract is invalid")

    def carrier(self) -> dict:
        return {
            **self.workflow_identity.carrier(),
            "operation": self.operation,
            "operation_id": self.operation_id,
            "tool_attempt_id": self.attempt_id,
            "tool_attempt": self.attempt,
            "effect": self.effect,
        }


@dataclass(frozen=True)
class ToolOperationEvent:
    identity: ToolOperationIdentity
    status: str
    event_id: str = field(default_factory=lambda: uuid.uuid4().hex)
    success: bool | None = None
    error_code: str | None = None
    latency_ms: float | None = None
    cost_usd: float | None = None
    ts: float = field(default_factory=time.time)

    def __post_init__(self) -> None:
        _identifier("event_id", self.event_id)
        if self.status not in STATUSES:
            raise WorkflowIdentityError("tool event status is invalid")
        if isinstance(self.ts, bool) or not isinstance(self.ts, (int, float)):
            raise WorkflowIdentityError("tool event timestamp must be a number")
        if not math.isfinite(float(self.ts)):
            raise WorkflowIdentityError("tool event timestamp must be finite")
        _identifier("error_code", self.error_code, optional=True)
        if self.success is not None and not isinstance(self.success, bool):
            raise WorkflowIdentityError("tool success must be a boolean")
        for label, value in (
            ("tool latency", self.latency_ms),
            ("tool cost", self.cost_usd),
        ):
            if value is not None and (
                isinstance(value, bool)
                or not isinstance(value, (int, float))
                or not math.isfinite(float(value))
                or value < 0
            ):
                raise WorkflowIdentityError(
                    f"{label} must be finite and non-negative"
                )
        outcomes = (
            self.success,
            self.error_code,
            self.latency_ms,
            self.cost_usd,
        )
        if self.status not in TERMINAL_STATUSES and any(
            value is not None for value in outcomes
        ):
            raise WorkflowIdentityError(
                "tool outcomes require a terminal status"
            )
        if self.status in TERMINAL_STATUSES and self.success is None:
            raise WorkflowIdentityError(
                "terminal tool events require explicit success"
            )

    def payload(self) -> dict:
        return {
            "event_id": self.event_id,
            "ts": self.ts,
            **self.identity.carrier(),
            "status": self.status,
            "success": self.success,
            "error_code": self.error_code,
            "latency_ms": self.latency_ms,
            "cost_usd": self.cost_usd,
        }

    @classmethod
    def from_payload(cls, value: Mapping) -> "ToolOperationEvent":
        if not isinstance(value, Mapping):
            raise WorkflowIdentityError("tool event must be an object")
        workflow_keys = set(WorkflowIdentity.__dataclass_fields__)
        identity_keys = {
            "operation",
            "operation_id",
            "tool_attempt_id",
            "tool_attempt",
            "effect",
        }
        event_keys = {
            "event_id",
            "ts",
            "status",
            "success",
            "error_code",
            "latency_ms",
            "cost_usd",
        }
        unknown = set(value) - workflow_keys - identity_keys - event_keys
        if unknown:
            raise WorkflowIdentityError(
                f"unknown tool event fields: {', '.join(sorted(unknown))}"
            )
        workflow = WorkflowIdentity.from_carrier(
            {key: value[key] for key in workflow_keys if key in value}
        )
        try:
            identity = ToolOperationIdentity(
                workflow,
                operation=value["operation"],
                operation_id=value["operation_id"],
                attempt_id=value["tool_attempt_id"],
                attempt=value.get("tool_attempt", 1),
                effect=value.get("effect", "unknown"),
            )
            return cls(
                identity,
                **{key: value[key] for key in event_keys if key in value},
            )
        except (KeyError, TypeError) as exc:
            raise WorkflowIdentityError(
                f"incomplete tool event: {exc}"
            ) from exc

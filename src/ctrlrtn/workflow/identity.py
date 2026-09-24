"""Provider-independent workflow identity and lifecycle facts."""

from __future__ import annotations

import math
import re
import time
import uuid
from collections.abc import Mapping
from dataclasses import dataclass, field

WORKFLOW_HEADERS = {
    "task_id": "x-ctrlrtn-task",
    "workflow": "x-ctrlrtn-workflow",
    "workflow_version": "x-ctrlrtn-workflow-version",
    "step": "x-ctrlrtn-step",
    "step_run_id": "x-ctrlrtn-step-run",
    "parent_step_run_id": "x-ctrlrtn-step-parent",
    "dependency_step_run_ids": "x-ctrlrtn-step-dependencies",
    "attempt": "x-ctrlrtn-step-attempt",
}
CTRLRTN_HEADER_PREFIX = "x-ctrlrtn-"
MAX_ID_BYTES = 128
MAX_DEPENDENCIES = 32
MAX_HEADER_BYTES = 4096
_IDENTIFIER = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/-]*$")
TERMINAL_STATUSES = frozenset({"completed", "failed", "cancelled", "skipped"})
EVENT_STATUSES = TERMINAL_STATUSES | {"started"}


class WorkflowIdentityError(ValueError):
    pass


def _identifier(
    name: str, value: object, *, optional: bool = False
) -> str | None:
    if value is None and optional:
        return None
    if not isinstance(value, str) or not value:
        raise WorkflowIdentityError(f"{name} must be a non-empty string")
    if len(value.encode("ascii", errors="replace")) > MAX_ID_BYTES:
        raise WorkflowIdentityError(f"{name} exceeds {MAX_ID_BYTES} bytes")
    if not _IDENTIFIER.fullmatch(value):
        raise WorkflowIdentityError(f"{name} has invalid characters")
    return value


@dataclass(frozen=True)
class WorkflowIdentity:
    task_id: str
    workflow: str
    workflow_version: str
    step: str
    step_run_id: str
    parent_step_run_id: str | None = None
    dependency_step_run_ids: tuple[str, ...] = ()
    attempt: int = 1

    def __post_init__(self) -> None:
        for name in (
            "task_id",
            "workflow",
            "workflow_version",
            "step",
            "step_run_id",
        ):
            _identifier(name, getattr(self, name))
        _identifier(
            "parent_step_run_id", self.parent_step_run_id, optional=True
        )
        if (
            isinstance(self.attempt, bool)
            or not isinstance(self.attempt, int)
            or self.attempt < 1
        ):
            raise WorkflowIdentityError("attempt must be a positive integer")
        deps = tuple(self.dependency_step_run_ids)
        if len(deps) > MAX_DEPENDENCIES:
            raise WorkflowIdentityError(
                f"at most {MAX_DEPENDENCIES} dependencies are allowed"
            )
        if len(set(deps)) != len(deps):
            raise WorkflowIdentityError("dependency IDs must be unique")
        for dependency in deps:
            _identifier("dependency_step_run_id", dependency)
        if self.step_run_id in deps:
            raise WorkflowIdentityError("a step run cannot depend on itself")
        object.__setattr__(self, "dependency_step_run_ids", deps)
        if (
            sum(len(k) + len(v) for k, v in self.headers().items())
            > MAX_HEADER_BYTES
        ):
            raise WorkflowIdentityError("workflow headers exceed 4096 bytes")

    def headers(self) -> dict[str, str]:
        values = {
            "task_id": self.task_id,
            "workflow": self.workflow,
            "workflow_version": self.workflow_version,
            "step": self.step,
            "step_run_id": self.step_run_id,
            "parent_step_run_id": self.parent_step_run_id,
            "dependency_step_run_ids": ",".join(self.dependency_step_run_ids)
            or None,
            "attempt": str(self.attempt),
        }
        return {
            WORKFLOW_HEADERS[key]: value
            for key, value in values.items()
            if value is not None
        }

    def carrier(self) -> dict:
        return {
            "task_id": self.task_id,
            "workflow": self.workflow,
            "workflow_version": self.workflow_version,
            "step": self.step,
            "step_run_id": self.step_run_id,
            "parent_step_run_id": self.parent_step_run_id,
            "dependency_step_run_ids": list(self.dependency_step_run_ids),
            "attempt": self.attempt,
        }

    @classmethod
    def from_carrier(cls, value: Mapping) -> WorkflowIdentity:
        if not isinstance(value, Mapping):
            raise WorkflowIdentityError("workflow carrier must be an object")
        allowed = set(cls.__dataclass_fields__)
        unknown = set(value) - allowed
        if unknown:
            raise WorkflowIdentityError(
                f"unknown carrier fields: {', '.join(sorted(unknown))}"
            )
        dependencies = value.get("dependency_step_run_ids", ())
        if not isinstance(dependencies, (list, tuple)):
            raise WorkflowIdentityError(
                "dependency_step_run_ids must be an array"
            )
        try:
            return cls(**dict(value))
        except TypeError as exc:
            raise WorkflowIdentityError(
                f"incomplete workflow carrier: {exc}"
            ) from exc


def identity_from_headers(
    headers: Mapping[str, str],
) -> tuple[WorkflowIdentity | None, str | None]:
    values = {
        key: headers.get(header) for key, header in WORKFLOW_HEADERS.items()
    }
    present = {key: value for key, value in values.items() if value}
    workflow_fields = set(present) - {"task_id"}
    if not workflow_fields:
        return None, None
    required = {
        "task_id",
        "workflow",
        "workflow_version",
        "step",
        "step_run_id",
    }
    missing = required - set(present)
    if missing:
        return (
            None,
            f"partial workflow identity; missing {', '.join(sorted(missing))}",
        )
    try:
        dependencies = tuple(
            filter(None, (values["dependency_step_run_ids"] or "").split(","))
        )
        attempt = int(values["attempt"] or "1")
        identity = WorkflowIdentity(
            task_id=present["task_id"],
            workflow=present["workflow"],
            workflow_version=present["workflow_version"],
            step=present["step"],
            step_run_id=present["step_run_id"],
            parent_step_run_id=values["parent_step_run_id"] or None,
            dependency_step_run_ids=dependencies,
            attempt=attempt,
        )
    except (ValueError, TypeError) as exc:
        return None, str(exc)
    return identity, None


@dataclass(frozen=True)
class WorkflowEvent:
    identity: WorkflowIdentity
    status: str
    event_id: str = field(default_factory=lambda: uuid.uuid4().hex)
    success: bool | None = None
    score: float | None = None
    error_code: str | None = None
    ts: float = field(default_factory=time.time)

    def __post_init__(self) -> None:
        _identifier("event_id", self.event_id)
        if self.status not in EVENT_STATUSES:
            raise WorkflowIdentityError(
                f"invalid workflow event status: {self.status}"
            )
        if isinstance(self.ts, bool) or not isinstance(self.ts, (int, float)):
            raise WorkflowIdentityError("ts must be a number")
        if not math.isfinite(float(self.ts)):
            raise WorkflowIdentityError("ts must be finite")
        _identifier("error_code", self.error_code, optional=True)
        if self.success is not None and not isinstance(self.success, bool):
            raise WorkflowIdentityError("success must be a boolean")
        if self.score is not None:
            if isinstance(self.score, bool) or not isinstance(
                self.score, (int, float)
            ):
                raise WorkflowIdentityError("score must be a number")
            if not math.isfinite(float(self.score)):
                raise WorkflowIdentityError("score must be finite")
        if self.status not in TERMINAL_STATUSES and any(
            value is not None
            for value in (self.success, self.score, self.error_code)
        ):
            raise WorkflowIdentityError(
                "outcome fields require a terminal status"
            )

    def payload(self) -> dict:
        return {
            "event_id": self.event_id,
            "ts": self.ts,
            **self.identity.carrier(),
            "status": self.status,
            "success": self.success,
            "score": self.score,
            "error_code": self.error_code,
        }

    @classmethod
    def from_payload(cls, value: Mapping) -> WorkflowEvent:
        if not isinstance(value, Mapping):
            raise WorkflowIdentityError("workflow event must be an object")
        identity_keys = set(WorkflowIdentity.__dataclass_fields__)
        event_keys = {
            "event_id",
            "ts",
            "status",
            "success",
            "score",
            "error_code",
        }
        unknown = set(value) - identity_keys - event_keys
        if unknown:
            raise WorkflowIdentityError(
                f"unknown event fields: {', '.join(sorted(unknown))}"
            )
        identity = WorkflowIdentity.from_carrier(
            {k: value[k] for k in identity_keys if k in value}
        )
        try:
            return cls(
                identity=identity,
                **{k: value[k] for k in event_keys if k in value},
            )
        except TypeError as exc:
            raise WorkflowIdentityError(
                f"incomplete workflow event: {exc}"
            ) from exc

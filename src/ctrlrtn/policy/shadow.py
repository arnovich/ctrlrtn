"""Online shadow experiments: mirror live inputs without serving their output."""

from __future__ import annotations

import hashlib
import time
import uuid
from dataclasses import dataclass, field, replace

from ctrlrtn.policy.scope import ExperimentScope

RUNNING = "running"
STOPPED = "stopped"


def _new_id() -> str:
    return "shadow:" + uuid.uuid4().hex[:16]


@dataclass(frozen=True)
class ShadowExperiment:
    """A shadow experiment record: which use-case (optionally one exact step)
    is mirrored, to which candidate model, at what sample percentage.
    Immutable; ``stopped()`` returns the stopped copy. A whole-workflow scope
    is rejected because nothing could run the continuation."""

    use_case_key: str
    candidate_model: str
    sample_pct: int
    shadow_id: str = field(default_factory=_new_id)
    candidate_provider: str | None = None
    status: str = RUNNING
    created_epoch: float = field(default_factory=time.time)
    workflow: str | None = None
    workflow_version: str | None = None
    step: str | None = None

    def __post_init__(self) -> None:
        if not self.use_case_key:
            raise ValueError("use_case_key is required")
        scope = ExperimentScope(
            self.use_case_key, self.workflow, self.workflow_version, self.step
        )
        if scope.is_workflow_scoped and not scope.is_step_scoped:
            raise ValueError(
                "whole-workflow shadowing requires a candidate-owned "
                "continuation runner; use an exact step or live split"
            )
        if not self.candidate_model:
            raise ValueError("candidate_model is required")
        if not 1 <= self.sample_pct <= 100:
            raise ValueError("sample_pct must be 1..100")
        if self.candidate_provider is not None and not self.candidate_provider:
            raise ValueError("candidate_provider must be non-empty when set")
        if self.status not in {RUNNING, STOPPED}:
            raise ValueError("invalid shadow status")
        object.__setattr__(self, "created_epoch", float(self.created_epoch))

    @property
    def is_running(self) -> bool:
        return self.status == RUNNING

    def stopped(self) -> ShadowExperiment:
        return replace(self, status=STOPPED)

    @property
    def scope(self) -> ExperimentScope:
        return ExperimentScope(
            self.use_case_key, self.workflow, self.workflow_version, self.step
        )


def selected(experiment: ShadowExperiment, unit: str) -> bool:
    digest = hashlib.sha256(
        experiment.shadow_id.encode() + b"\0" + unit.encode()
    ).digest()
    return int.from_bytes(digest[:8], "big") % 100 < experiment.sample_pct


@dataclass(frozen=True)
class ShadowStats:
    """Durable attrition counters for one shadow experiment: mirrors
    submitted, completed, failed upstream, and dropped before or after
    sending."""

    shadow_id: str
    submitted: int = 0
    completed: int = 0
    failed: int = 0
    dropped: int = 0

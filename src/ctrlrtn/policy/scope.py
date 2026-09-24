"""Stable, explicit scope shared by offline, shadow, and split experiments."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass

from ctrlrtn.workflow.identity import (
    WorkflowIdentity,
    identity_from_headers,
)


@dataclass(frozen=True)
class ExperimentScope:
    use_case_key: str
    workflow: str | None = None
    workflow_version: str | None = None
    step: str | None = None

    def __post_init__(self) -> None:
        if not self.use_case_key:
            raise ValueError("use_case_key is required")
        if (self.workflow is None) != (self.workflow_version is None):
            raise ValueError(
                "workflow and workflow_version must be set together"
            )
        if self.step is not None and self.workflow is None:
            raise ValueError("step requires workflow and workflow_version")

    @property
    def is_workflow_scoped(self) -> bool:
        return self.workflow is not None

    @property
    def is_step_scoped(self) -> bool:
        return self.step is not None

    @property
    def key(self) -> str:
        if not self.is_workflow_scoped:
            return self.use_case_key
        key = f"{self.use_case_key}|{self.workflow}@{self.workflow_version}"
        return f"{key}/{self.step}" if self.is_step_scoped else key

    @property
    def label(self) -> str:
        if not self.is_workflow_scoped:
            return self.use_case_key
        label = f"{self.use_case_key} · {self.workflow}@{self.workflow_version}"
        return f"{label}/{self.step}" if self.is_step_scoped else label

    def matches_identity(self, identity: WorkflowIdentity | None) -> bool:
        if not self.is_workflow_scoped:
            return True
        return (
            identity is not None
            and identity.workflow == self.workflow
            and identity.workflow_version == self.workflow_version
            and (not self.is_step_scoped or identity.step == self.step)
        )

    def matches_headers(self, headers: Mapping[str, str]) -> bool:
        identity, _ = identity_from_headers(headers)
        return self.matches_identity(identity)

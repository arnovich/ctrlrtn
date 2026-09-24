"""Immutable desired-state documents and Git revision provenance."""

from __future__ import annotations

import time
from dataclasses import dataclass, field

from ctrlrtn.policy.experiment import Experiment
from ctrlrtn.policy.route import Route, WorkflowRoute

VERSION = 1
DEFAULT_FILE = "routing.yaml"


class ControlConfigError(ValueError):
    """An invalid desired-state document or unprovable Git revision."""


@dataclass(frozen=True)
class WorkflowStepDefinition:
    name: str
    predecessors: tuple[str, ...] = ()
    fan_out: bool = False
    retry: bool = False
    condition: str | None = None


@dataclass(frozen=True)
class WorkflowDefinition:
    workflow: str
    workflow_version: str
    steps: tuple[WorkflowStepDefinition, ...]


@dataclass(frozen=True)
class ControlConfig:
    routes: tuple[Route, ...] = field(default_factory=tuple)
    experiments: tuple[Experiment, ...] = field(default_factory=tuple)
    workflow_routes: tuple[WorkflowRoute, ...] = field(default_factory=tuple)
    workflows: tuple[WorkflowDefinition, ...] = field(default_factory=tuple)


@dataclass(frozen=True)
class ControlRevision:
    revision: str
    source_path: str
    document_sha256: str
    activated_at: float = field(default_factory=time.time)

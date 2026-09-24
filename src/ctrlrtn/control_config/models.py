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
    """One declared step of a workflow: its name, the steps that must run
    before it, and whether it fans out, retries or runs conditionally."""

    name: str
    predecessors: tuple[str, ...] = ()
    fan_out: bool = False
    retry: bool = False
    condition: str | None = None


@dataclass(frozen=True)
class WorkflowDefinition:
    """A declared workflow version and its steps, exactly as written in the
    desired-state document."""

    workflow: str
    workflow_version: str
    steps: tuple[WorkflowStepDefinition, ...]


@dataclass(frozen=True)
class ControlConfig:
    """One parsed desired-state document: the complete set of routes,
    experiments, workflow routes and workflow definitions that should be
    live. Inert until activated; activation reconciles the store to match it
    exactly, stopping and creating experiments as needed."""

    routes: tuple[Route, ...] = field(default_factory=tuple)
    experiments: tuple[Experiment, ...] = field(default_factory=tuple)
    workflow_routes: tuple[WorkflowRoute, ...] = field(default_factory=tuple)
    workflows: tuple[WorkflowDefinition, ...] = field(default_factory=tuple)


@dataclass(frozen=True)
class ControlRevision:
    """Provenance of an activated desired-state document: the Git revision
    and path it came from, the digest of the document actually applied, and
    when. Kept as a singleton so live control state is traceable to a
    commit."""

    revision: str
    source_path: str
    document_sha256: str
    activated_at: float = field(default_factory=time.time)

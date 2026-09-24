"""Git-backed desired state for routes, experiments, and workflows.

Documents remain inert until explicit activation. This facade preserves the
original public import path while parsing, rendering, and Git provenance remain
separate from persistence and activation.
"""

from ctrlrtn.control_config.models import (
    DEFAULT_FILE,
    VERSION,
    ControlConfig,
    ControlConfigError,
    ControlRevision,
    WorkflowDefinition,
    WorkflowStepDefinition,
)
from ctrlrtn.control_config.parser import load_control_config
from ctrlrtn.control_config.provenance import (
    document_sha256,
    verify_git_revision,
)
from ctrlrtn.control_config.rendering import (
    canonical_document,
    config_diff,
    live_config,
    revision_json,
)

__all__ = [
    "DEFAULT_FILE",
    "VERSION",
    "ControlConfig",
    "ControlConfigError",
    "ControlRevision",
    "WorkflowDefinition",
    "WorkflowStepDefinition",
    "canonical_document",
    "config_diff",
    "document_sha256",
    "live_config",
    "load_control_config",
    "revision_json",
    "verify_git_revision",
]

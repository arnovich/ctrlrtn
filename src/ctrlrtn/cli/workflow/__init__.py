"""Composed CLI adapter for workflow analysis and job submission."""

from .core import WorkflowCommandContext
from .discovery import WorkflowDiscoveryCommands
from .inference import WorkflowInferenceCommands
from .inspection import WorkflowInspectionCommands


class WorkflowCommands(
    WorkflowInferenceCommands,
    WorkflowDiscoveryCommands,
    WorkflowInspectionCommands,
    WorkflowCommandContext,
):
    """Expose workflow handlers while delegating each command family."""


__all__ = ["WorkflowCommands"]

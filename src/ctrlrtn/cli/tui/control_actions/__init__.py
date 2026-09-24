"""Composed experiment, routing, shadow, and Git-config TUI actions."""

from .experiments import LiveExperimentControlActions
from .git_config import GitConfigControlActions
from .routes import RouteControlActions
from .selection import ControlSelectionMixin
from .shadows import ShadowControlActions


class ControlActions(
    ShadowControlActions,
    GitConfigControlActions,
    LiveExperimentControlActions,
    RouteControlActions,
    ControlSelectionMixin,
):
    """Expose control actions while delegating each family to one module."""


__all__ = ["ControlActions"]

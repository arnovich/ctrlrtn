"""Composed experiment analysis, calibration, replay, and campaign CLI."""

from .calibration import CalibrationCommands
from .campaign import CampaignReportCommands
from .core import EvaluationCommandContext
from .loading import load_labels
from .replay import ReplayEvaluationCommands
from .status import EvaluationStatusCommands


class EvaluationCommands(
    EvaluationStatusCommands,
    CalibrationCommands,
    ReplayEvaluationCommands,
    CampaignReportCommands,
    EvaluationCommandContext,
):
    """Expose evaluation handlers while delegating each command family."""


__all__ = [
    "EvaluationCommands",
    "load_labels",
]

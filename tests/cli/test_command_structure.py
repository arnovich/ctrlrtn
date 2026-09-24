"""Architecture guards for the residual CLI command-family facades."""

from ctrlrtn.cli.evaluation import EvaluationCommands, load_labels
from ctrlrtn.cli.evaluation.calibration import CalibrationCommands
from ctrlrtn.cli.evaluation.campaign import CampaignReportCommands
from ctrlrtn.cli.evaluation.core import EvaluationCommandContext
from ctrlrtn.cli.evaluation.replay import ReplayEvaluationCommands
from ctrlrtn.cli.evaluation.status import EvaluationStatusCommands
from ctrlrtn.cli.workflow import WorkflowCommands
from ctrlrtn.cli.workflow.core import WorkflowCommandContext
from ctrlrtn.cli.workflow.discovery import WorkflowDiscoveryCommands
from ctrlrtn.cli.workflow.inference import WorkflowInferenceCommands
from ctrlrtn.cli.workflow.inspection import WorkflowInspectionCommands


def test_evaluation_commands_compose_command_families():
    assert EvaluationCommands.__bases__ == (
        EvaluationStatusCommands,
        CalibrationCommands,
        ReplayEvaluationCommands,
        CampaignReportCommands,
        EvaluationCommandContext,
    )
    assert load_labels.__module__.endswith(".evaluation.loading")


def test_workflow_commands_compose_command_families():
    assert WorkflowCommands.__bases__ == (
        WorkflowInferenceCommands,
        WorkflowDiscoveryCommands,
        WorkflowInspectionCommands,
        WorkflowCommandContext,
    )


def test_command_facades_do_not_own_handlers():
    for facade in (EvaluationCommands, WorkflowCommands):
        assert not {
            name
            for name, value in facade.__dict__.items()
            if callable(value) and not name.startswith("__")
        }

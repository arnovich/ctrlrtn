"""Stable import facade for the decomposed Textual console."""

from ctrlrtn.cli.tui.app import ConsoleApp, run_console
from ctrlrtn.cli.tui.formatting import (
    command_panel_text,
    graph_axis,
    graph_label,
    page_heading,
)
from ctrlrtn.cli.tui.forms import (
    ConfirmScreen,
    ConsoleUnavailable,
    GitConfigScreen,
    LiveExperimentScreen,
    OfflineExperimentScreen,
    RouteScreen,
    ShadowExperimentScreen,
    WorkflowDiscoveryScopeScreen,
    WorkflowIdentifyScreen,
    WorkflowProposalVerifyScreen,
    field_matches,
)
from ctrlrtn.cli.tui.models import (
    _DEFAULT_GRAPH_WINDOW,
    _DEFAULT_TABLE_WINDOW,
    _GRAPH_WINDOWS,
    _PAGE_SIZE,
    _TABLE_WINDOWS,
)
from ctrlrtn.cli.tui.screens import (
    DiscoveredWorkflowDagScreen,
    WindowPickerScreen,
)
from ctrlrtn.cli.tui.state import (
    call_detail,
    experiment_detail,
    job_detail,
    load_state,
    routing_status,
    shadow_detail,
    task_detail,
    usecase_detail,
)

_EMPTY = "No data yet. Start the gateway or queue an experiment job."

__all__ = [
    "_DEFAULT_GRAPH_WINDOW",
    "_DEFAULT_TABLE_WINDOW",
    "_EMPTY",
    "_GRAPH_WINDOWS",
    "_PAGE_SIZE",
    "_TABLE_WINDOWS",
    "ConfirmScreen",
    "ConsoleApp",
    "ConsoleUnavailable",
    "DiscoveredWorkflowDagScreen",
    "GitConfigScreen",
    "LiveExperimentScreen",
    "OfflineExperimentScreen",
    "RouteScreen",
    "ShadowExperimentScreen",
    "WindowPickerScreen",
    "WorkflowDiscoveryScopeScreen",
    "WorkflowIdentifyScreen",
    "WorkflowProposalVerifyScreen",
    "call_detail",
    "command_panel_text",
    "experiment_detail",
    "field_matches",
    "graph_axis",
    "graph_label",
    "job_detail",
    "load_state",
    "page_heading",
    "routing_status",
    "run_console",
    "shadow_detail",
    "task_detail",
    "usecase_detail",
]

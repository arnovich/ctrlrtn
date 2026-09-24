"""Late-bound reporting calls used by SDK lifecycle objects.

Applications and tests may replace the reporting functions on
``ctrlrtn.sdk``. Lifecycle code calls through this seam so a replacement
installed on the facade is the one that runs.
"""

from __future__ import annotations

import sys
from types import ModuleType


def _facade() -> ModuleType:
    return sys.modules["ctrlrtn.sdk"]


def report_outcome(*args, **kwargs):
    """Forward to ``ctrlrtn.sdk.report_outcome`` as currently bound on the
    facade, so a replacement installed there is honoured."""
    return _facade().report_outcome(*args, **kwargs)


def report_workflow_event(*args, **kwargs):
    """Forward to ``ctrlrtn.sdk.report_workflow_event`` as currently bound on
    the facade, so a replacement installed there is honoured."""
    return _facade().report_workflow_event(*args, **kwargs)


def report_tool_operation_event(*args, **kwargs):
    """Forward to ``ctrlrtn.sdk.report_tool_operation_event`` as currently
    bound on the facade, so a replacement installed there is honoured."""
    return _facade().report_tool_operation_event(*args, **kwargs)

"""Late-bound reporting calls used by SDK lifecycle objects.

Reporting functions have always been replaceable on ``ctrlrtn.sdk`` (for
tests and application adapters). Lifecycle code calls through this seam so a
replacement made on the public facade remains visible after decomposition.
"""

from __future__ import annotations

import sys
from types import ModuleType


def _facade() -> ModuleType:
    return sys.modules["ctrlrtn.sdk"]


def report_outcome(*args, **kwargs):
    return _facade().report_outcome(*args, **kwargs)


def report_workflow_event(*args, **kwargs):
    return _facade().report_workflow_event(*args, **kwargs)


def report_tool_operation_event(*args, **kwargs):
    return _facade().report_tool_operation_event(*args, **kwargs)

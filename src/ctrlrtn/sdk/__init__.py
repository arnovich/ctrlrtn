"""Client-side helpers for instrumenting an agentic edition.

An app points its LLM client's ``base_url`` at the gateway and wraps a run in
``edition(...)``. Every request made through a ctrlrtn-wrapped httpx client inside
that block is stamped with the same task id automatically; sub-agents running
in the same process and async context inherit it.

Threads and subprocesses start with an empty context. Wrap a callable with
``bind`` before crossing that boundary, or explicitly export and import the
workflow carrier. In strict mode (``CTRLRTN_STRICT=1``), an unbound request made
while an edition is active raises instead of being silently recorded as
baseline.
"""

from ctrlrtn.sdk.context import (
    bind,
    current_route,
    current_task_id,
    export_carrier,
    import_carrier,
    new_task_id,
    route,
    stamp,
)
from ctrlrtn.sdk.http import (
    async_event_hooks,
    async_http_client,
    event_hooks,
    http_client,
)
from ctrlrtn.sdk.lifecycle import Edition, Step, ToolOperation, edition
from ctrlrtn.sdk.reporting import (
    areport_outcome,
    report_outcome,
    report_tool_operation_event,
    report_workflow_event,
)

__all__ = [
    "Edition",
    "Step",
    "ToolOperation",
    "areport_outcome",
    "async_event_hooks",
    "async_http_client",
    "bind",
    "current_route",
    "current_task_id",
    "edition",
    "event_hooks",
    "export_carrier",
    "http_client",
    "import_carrier",
    "new_task_id",
    "report_outcome",
    "report_tool_operation_event",
    "report_workflow_event",
    "route",
    "stamp",
]

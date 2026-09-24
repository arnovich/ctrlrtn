"""Architecture guards for the decomposed client SDK."""

import ast
import inspect

from ctrlrtn import sdk


def test_sdk_package_is_an_import_facade():
    tree = ast.parse(inspect.getsource(sdk))
    assert not any(
        isinstance(node, (ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef))
        for node in tree.body
    )
    assert sdk.__all__ == [
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


def test_sdk_capabilities_stay_in_their_modules():
    assert sdk.bind.__module__ == "ctrlrtn.sdk.context"
    assert sdk.http_client.__module__ == "ctrlrtn.sdk.http"
    assert sdk.Edition.__module__ == "ctrlrtn.sdk.lifecycle"
    assert sdk.Step.__module__ == "ctrlrtn.sdk.lifecycle"
    assert sdk.ToolOperation.__module__ == "ctrlrtn.sdk.lifecycle"
    assert sdk.report_outcome.__module__ == "ctrlrtn.sdk.reporting"

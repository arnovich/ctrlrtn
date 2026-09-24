"""Architecture guards for decomposed Git-backed desired state."""

import ast
import inspect

import ctrlrtn.control_config as facade


def test_control_config_package_is_an_import_facade():
    tree = ast.parse(inspect.getsource(facade))
    assert not any(
        isinstance(node, (ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef))
        for node in tree.body
    )
    assert facade.__all__ == [
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


def test_control_config_capabilities_stay_in_their_modules():
    assert facade.ControlConfig.__module__ == ("ctrlrtn.control_config.models")
    assert facade.load_control_config.__module__ == (
        "ctrlrtn.control_config.parser"
    )
    assert facade.canonical_document.__module__ == (
        "ctrlrtn.control_config.rendering"
    )
    assert facade.verify_git_revision.__module__ == (
        "ctrlrtn.control_config.provenance"
    )

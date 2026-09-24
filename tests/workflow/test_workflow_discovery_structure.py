"""Architecture guards for passive workflow-family discovery."""

import ast
import inspect

import ctrlrtn.workflow.discovery as facade


def test_workflow_discovery_package_is_an_import_facade():
    tree = ast.parse(inspect.getsource(facade))
    assert not any(
        isinstance(node, (ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef))
        for node in tree.body
    )
    assert facade.__all__ == [
        "ALGORITHM",
        "DiscoveredFamilyEdge",
        "DiscoveredFamilyNode",
        "DiscoveredWorkflowFamily",
        "DiscoveryProgress",
        "ObservedTaskStructure",
        "SyntheticCorrelation",
        "WorkflowDiscoveryReport",
        "WorkflowFamilyAssignment",
        "build_observed_task_structures",
        "correlate_unscoped_traces",
        "discover_workflow_families",
        "render_workflow_discovery",
        "structure_similarity",
    ]


def test_workflow_discovery_capabilities_stay_in_their_modules():
    assert facade.WorkflowDiscoveryReport.__module__ == (
        "ctrlrtn.workflow.discovery.models"
    )
    assert facade.correlate_unscoped_traces.__module__ == (
        "ctrlrtn.workflow.discovery.correlation"
    )
    assert facade.build_observed_task_structures.__module__ == (
        "ctrlrtn.workflow.discovery.structures"
    )
    assert facade.discover_workflow_families.__module__ == (
        "ctrlrtn.workflow.discovery.families"
    )
    assert facade.render_workflow_discovery.__module__ == (
        "ctrlrtn.workflow.discovery.rendering"
    )

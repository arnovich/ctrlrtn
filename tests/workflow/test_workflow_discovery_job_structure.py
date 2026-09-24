"""Architecture guards for the workflow-discovery job package."""

import ast
import inspect

import ctrlrtn.workflow.discovery_job as facade


def test_workflow_discovery_job_package_is_an_import_facade():
    tree = ast.parse(inspect.getsource(facade))
    assert not any(
        isinstance(node, (ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef))
        for node in tree.body
    )
    assert facade.__all__ == [
        "ARTIFACT_KIND",
        "AUTHORITY",
        "KIND",
        "MAX_LIMIT",
        "MAX_TRACES",
        "SUPPORTED_ARTIFACT_VERSIONS",
        "VERSION",
        "WorkflowDiscoveryComparison",
        "WorkflowDiscoveryJobError",
        "WorkflowDiscoveryJobPlan",
        "WorkflowDiscoveryScope",
        "WorkflowFamilySnapshotMatch",
        "build_workflow_discovery_artifact",
        "compare_workflow_discovery_artifacts",
        "prepare_workflow_discovery_job",
        "projections_from_workflow_discovery_artifact",
        "render_workflow_discovery_comparison",
        "render_workflow_discovery_selection",
        "report_from_workflow_discovery_artifact",
        "run_workflow_discovery_job",
        "scope_from_workflow_discovery_artifact",
        "validate_workflow_discovery_scope",
        "verify_workflow_discovery_artifact",
    ]


def test_workflow_discovery_job_capabilities_stay_in_their_modules():
    assert facade.WorkflowDiscoveryScope.__module__ == (
        "ctrlrtn.workflow.discovery_job.models"
    )
    assert facade.prepare_workflow_discovery_job.__module__ == (
        "ctrlrtn.workflow.discovery_job.planning"
    )
    assert facade.verify_workflow_discovery_artifact.__module__ == (
        "ctrlrtn.workflow.discovery_job.artifacts"
    )
    assert facade.run_workflow_discovery_job.__module__ == (
        "ctrlrtn.workflow.discovery_job.handler"
    )
    assert facade.compare_workflow_discovery_artifacts.__module__ == (
        "ctrlrtn.workflow.discovery_job.comparison"
    )

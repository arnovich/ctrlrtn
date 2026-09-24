"""Analysis-only discovery of recurring workflow families from legacy traffic."""

from ctrlrtn.workflow.discovery.correlation import (
    correlate_unscoped_traces,
)
from ctrlrtn.workflow.discovery.families import (
    discover_workflow_families,
)
from ctrlrtn.workflow.discovery.models import (
    ALGORITHM,
    DiscoveredFamilyEdge,
    DiscoveredFamilyNode,
    DiscoveredWorkflowFamily,
    DiscoveryProgress,
    ObservedTaskStructure,
    SyntheticCorrelation,
    WorkflowDiscoveryReport,
    WorkflowFamilyAssignment,
)
from ctrlrtn.workflow.discovery.rendering import (
    render_workflow_discovery,
)
from ctrlrtn.workflow.discovery.structures import (
    build_observed_task_structures,
    structure_similarity,
)

__all__ = [
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

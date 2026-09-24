"""Discovered families create immutable, inert identification proposals."""

import json
from datetime import UTC, datetime

import pytest

from ctrlrtn.workflow.discovery import (
    DiscoveredFamilyEdge,
    DiscoveredFamilyNode,
    DiscoveredWorkflowFamily,
)
from ctrlrtn.workflow.proposal import (
    WorkflowProposalError,
    build_workflow_proposal,
    load_workflow_proposal,
    write_workflow_proposal,
)


def _family():
    return DiscoveredWorkflowFamily(
        "family:abc",
        3,
        2,
        0.9,
        1.0,
        "task-digest",
        ("a", "b", "c"),
        (
            DiscoveredFamilyNode("tag:researcher[search]", 3, 3),
            DiscoveredFamilyNode("tag:writer", 3, 4),
        ),
        (
            DiscoveredFamilyEdge(
                "tag:researcher[search]", "tag:writer", "tool-result", 3, 3
            ),
        ),
    )


def test_proposal_maps_observed_labels_to_reviewable_control_fragment(tmp_path):
    proposal = build_workflow_proposal(
        _family(),
        "newsroom",
        "git:abc123",
        created_at=datetime(2026, 8, 5, tzinfo=UTC),
    )
    steps = proposal["proposed_control_fragment"]["workflows"]["newsroom"][
        "git:abc123"
    ]["steps"]
    assert steps["writer"]["predecessors"] == ["researcher"]
    assert steps["writer"]["allows"] == {"retry": True}
    assert proposal["authority"] == "analysis_only_no_routing_or_execution"
    assert proposal["review"]["required"] is True

    path = tmp_path / "proposal.json"
    write_workflow_proposal(path, proposal)
    assert load_workflow_proposal(path) == proposal
    with pytest.raises(WorkflowProposalError, match="already exists"):
        write_workflow_proposal(path, proposal)


def test_proposal_tampering_and_invalid_identity_fail_closed(tmp_path):
    with pytest.raises(WorkflowProposalError, match="stable identifier"):
        build_workflow_proposal(_family(), "contains spaces", "v1")
    proposal = build_workflow_proposal(_family(), "flow", "v1")
    proposal["family"]["support"] = 999
    path = tmp_path / "tampered.json"
    path.write_text(json.dumps(proposal), encoding="utf-8")
    with pytest.raises(WorkflowProposalError, match="digest mismatch"):
        load_workflow_proposal(path)

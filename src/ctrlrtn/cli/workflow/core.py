"""CLI adapter for workflow discovery, diagnostics, and analysis."""

from __future__ import annotations

import argparse
import os
from collections.abc import Callable
from dataclasses import asdict
from typing import NoReturn

from ctrlrtn.recorder.sqlite.store import SqliteTraceStore
from ctrlrtn.workflow.catalog import (
    build_workflow_catalog,
    render_workflow_catalog,
    render_workflow_comparison,
)
from ctrlrtn.workflow.discovery import (
    discover_workflow_families,
    render_workflow_discovery,
)
from ctrlrtn.workflow.discovery_job import KIND as WORKFLOW_DISCOVERY_JOB_KIND
from ctrlrtn.workflow.discovery_job import (
    WorkflowDiscoveryJobError,
    WorkflowDiscoveryScope,
    compare_workflow_discovery_artifacts,
    prepare_workflow_discovery_job,
    projections_from_workflow_discovery_artifact,
    render_workflow_discovery_comparison,
    render_workflow_discovery_selection,
    report_from_workflow_discovery_artifact,
    validate_workflow_discovery_scope,
)
from ctrlrtn.workflow.discovery_projection import (
    render_discovered_family_json,
    render_discovered_family_mermaid,
    render_discovered_family_projection,
)
from ctrlrtn.workflow.graph import (
    build_workflow_flow,
    render_workflow_flow,
    render_workflow_mermaid,
    render_workflow_sankey,
)
from ctrlrtn.workflow.inference import ALGORITHM, infer_workflow_edges
from ctrlrtn.workflow.proposal import (
    WorkflowProposalError,
    build_workflow_proposal,
    load_workflow_proposal,
    write_workflow_proposal,
)
from ctrlrtn.workflow.recommend import (
    build_workflow_recommendations,
    render_workflow_recommendations,
)
from ctrlrtn.workflow.step_detail import render_workflow_step_detail

DatabasePath = Callable[[], str]
Fail = Callable[[str], NoReturn]
ReadJson = Callable[[str, str], dict]


class WorkflowCommandContext:
    """Bound terminal handlers for workflow analysis and job submission."""

    def __init__(
        self, database_path: DatabasePath, fail: Fail, read_json: ReadJson
    ) -> None:
        self._database_path = database_path
        self._fail = fail
        self._read_json = read_json

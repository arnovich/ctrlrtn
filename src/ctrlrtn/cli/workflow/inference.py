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


class WorkflowInferenceCommands:
    """Infer and inspect candidate workflow edges."""

    def _workflow_infer(self, args: argparse.Namespace) -> None:
        store = SqliteTraceStore(self._database_path())
        try:
            traces, explicit, explicit_runs = store.workflow_inference_inputs()
            report = infer_workflow_edges(traces, explicit, explicit_runs)
            store.replace_inferred_workflow_edges(report.edges, ALGORITHM)
        finally:
            store.close()
        precision = (
            "N/A" if report.precision is None else f"{report.precision:.1%}"
        )
        recall = "N/A" if report.recall is None else f"{report.recall:.1%}"
        print(
            f"Inferred {len(report.edges)} evidence-only edge(s) with "
            f"{ALGORITHM}; ambiguous evidence skipped: "
            f"{report.ambiguous_evidence}."
        )
        print(
            f"Explicit-ground-truth accuracy: TP={report.true_positive} "
            f"FP={report.false_positive} FN={report.false_negative} "
            f"precision={precision} recall={recall}."
        )
        print("Inferred edges are analysis-only and cannot affect routing.")

    def _workflow_inferred(self, args: argparse.Namespace) -> None:
        store = SqliteTraceStore(self._database_path())
        try:
            rows = store.inferred_workflow_edges()
        finally:
            store.close()
        if not rows:
            print("No inferred workflow edges. Run `workflow infer` first.")
            return
        print(
            "task                 source -> target                         "
            "confidence state"
        )
        for row in rows:
            print(
                f"{row.task_id[:20]:<20} {row.source_step_run_id[:14]:<14} -> "
                f"{row.target_step_run_id[:14]:<14} {row.confidence:>8.0%} "
                f"{row.confirmation}"
            )

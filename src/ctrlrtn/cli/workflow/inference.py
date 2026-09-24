"""CLI adapter for workflow discovery, diagnostics, and analysis."""

from __future__ import annotations

import argparse
from collections.abc import Callable
from typing import NoReturn

from ctrlrtn.recorder.sqlite.store import SqliteTraceStore
from ctrlrtn.workflow.inference import ALGORITHM, infer_workflow_edges

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

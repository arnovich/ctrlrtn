"""CLI adapter for workflow discovery, diagnostics, and analysis."""

from __future__ import annotations

import argparse
import os
from collections.abc import Callable
from typing import NoReturn

from ctrlrtn.recorder.sqlite.store import SqliteTraceStore
from ctrlrtn.workflow.catalog import (
    build_workflow_catalog,
    render_workflow_catalog,
    render_workflow_comparison,
)
from ctrlrtn.workflow.graph import (
    build_workflow_flow,
    render_workflow_flow,
    render_workflow_mermaid,
    render_workflow_sankey,
)
from ctrlrtn.workflow.recommend import (
    build_workflow_recommendations,
    render_workflow_recommendations,
)
from ctrlrtn.workflow.step_detail import render_workflow_step_detail

DatabasePath = Callable[[], str]
Fail = Callable[[str], NoReturn]
ReadJson = Callable[[str, str], dict]


class WorkflowInspectionCommands:
    """Inspect, render, compare, and erase recorded workflow state."""

    def _workflow_erase_task(self, args: argparse.Namespace) -> None:
        store = SqliteTraceStore(self._database_path())
        try:
            try:
                result = store.erase_workflow_task(
                    args.task_id, apply=args.apply
                )
            except ValueError as exc:
                self._fail(f"workflow task erasure failed: {exc}")
        finally:
            store.close()
        action = "Erased" if result.applied else "Would erase"
        print(
            f"{action} task {result.task_id}: {result.traces} trace(s), "
            f"{result.workflow_events} workflow event(s), "
            f"{result.tool_operation_events} tool event(s), "
            f"{result.inferred_edges} inferred edge(s), and "
            f"{result.outcomes} outcome(s)."
        )
        if result.protected_jobs:
            print(
                f"Protected by {result.protected_jobs} queued or running job(s)."
            )
        if not result.applied:
            print("Dry run only; pass --apply to erase these database records.")

    def _workflow_diagnostics(self, args: argparse.Namespace) -> None:
        store = SqliteTraceStore(self._database_path())
        try:
            report = store.workflow_diagnostics()
        finally:
            store.close()
        print(
            f"Workflow traces: {report['valid_traces']} valid · "
            f"{report['invalid_traces']} invalid"
        )
        print(
            f"Lifecycle consistency: {report['reused_step_run_ids']} reused run "
            f"IDs · {report['cross_task_dependencies']} cross-task dependencies "
            f"· {report['conflicting_terminal_runs']} conflicting terminal runs"
        )
        print(
            f"Attribution: {report['unreported_step_runs']} step runs without an "
            f"explicit outcome · {report['inconsistent_step_runs']} inconsistent"
        )
        for error, count in report["errors"].items():
            print(f"  {count} × {error}")

    def _workflow_steps(self, args: argparse.Namespace) -> None:
        if args.version and not args.workflow:
            self._fail("--version requires --workflow")
        store = SqliteTraceStore(self._database_path())
        try:
            rows = store.workflow_step_metrics(args.workflow, args.version)
        finally:
            store.close()
        if not rows:
            print("No explicit workflow step runs.")
            return
        print(
            "workflow@version                     / step             runs calls "
            "cost       call/run ms  state(c/f/x/s/a/!) outcomes(+/-/?) score"
        )
        for row in rows:
            duration = (
                "-"
                if row.avg_run_duration_ms is None
                else f"{row.avg_run_duration_ms:.0f}"
            )
            score = "-" if row.avg_score is None else f"{row.avg_score:.2f}"
            print(
                f"{(row.workflow + '@' + row.workflow_version)[:36]:<36} / "
                f"{row.step[:15]:<15} {row.runs:>4} {row.calls:>5} "
                f"${row.cost_usd:<9.4f} {row.avg_call_latency_ms:>4.0f}/"
                f"{duration:<6} {row.completed}/{row.failed}/{row.cancelled}/"
                f"{row.skipped}/{row.active}/{row.inconsistent} "
                f"{row.successful_outcomes}/{row.failed_outcomes}/"
                f"{row.unreported_runs} {score}"
            )
        print(
            "Outcomes above are explicit step events; task outcomes are not "
            "inherited."
        )

    def _workflow_diagram(self, args: argparse.Namespace) -> None:
        store = SqliteTraceStore(self._database_path())
        try:
            graph = store.workflow_graph(args.task_id)
        finally:
            store.close()
        if graph is None:
            self._fail(f"no explicit workflow graph for task {args.task_id!r}")
        diagram = render_workflow_mermaid(graph) + "\n"
        if args.output is None:
            print(diagram, end="")
            return
        parent = os.path.dirname(args.output) or "."
        if not os.path.isdir(parent) or not os.access(parent, os.W_OK):
            self._fail(f"diagram path is not writable: {args.output}")
        try:
            with open(args.output, "w", encoding="utf-8") as handle:
                handle.write(diagram)
        except OSError as exc:
            self._fail(f"could not export workflow diagram: {exc}")
        print(f"Wrote {args.output}.")

    def _workflow_flow(self, args: argparse.Namespace) -> None:
        if args.limit < 1:
            self._fail("--limit must be at least 1")
        store = SqliteTraceStore(self._database_path())
        try:
            graphs = store.workflow_graphs(
                args.workflow, args.version, args.limit
            )
        finally:
            store.close()
        flow = build_workflow_flow(graphs)
        if flow is None:
            self._fail(f"no task graphs for {args.workflow!r}@{args.version!r}")
        print(render_workflow_flow(flow))
        if args.output:
            try:
                with open(args.output, "x", encoding="utf-8") as handle:
                    handle.write(render_workflow_sankey(flow) + "\n")
            except FileExistsError:
                self._fail(f"workflow Sankey already exists: {args.output}")
            except OSError as exc:
                self._fail(f"could not export workflow Sankey: {exc}")
            print(f"Wrote explicit-edge Mermaid Sankey to {args.output}.")

    def _workflow_catalog(self, args: argparse.Namespace) -> None:
        store = SqliteTraceStore(self._database_path(), read_only=True)
        try:
            rows = build_workflow_catalog(store, args.workflow)
        finally:
            store.close()
        print(render_workflow_catalog(rows))

    def _workflow_compare(self, args: argparse.Namespace) -> None:
        store = SqliteTraceStore(self._database_path(), read_only=True)
        try:
            rows = build_workflow_catalog(store, args.workflow)
        finally:
            store.close()
        by_version = {row.version: row for row in rows}
        missing = {args.left_version, args.right_version} - set(by_version)
        if missing:
            self._fail(
                f"unknown workflow version(s): {', '.join(sorted(missing))}"
            )
        print(
            render_workflow_comparison(
                by_version[args.left_version],
                by_version[args.right_version],
            )
        )

    def _workflow_step_detail(self, args: argparse.Namespace) -> None:
        store = SqliteTraceStore(self._database_path(), read_only=True)
        try:
            detail = store.workflow_step_detail(args.task_id, args.step_run_id)
        finally:
            store.close()
        if detail is None:
            self._fail(
                f"no step run {args.step_run_id!r} for task {args.task_id!r}"
            )
        print(render_workflow_step_detail(detail))

    def _workflow_recommendations(self, args: argparse.Namespace) -> None:
        store = SqliteTraceStore(self._database_path())
        try:
            metrics = store.workflow_step_metrics(args.workflow, args.version)
            definitions = [
                definition
                for definition in store.workflow_definitions()
                if (
                    args.workflow is None
                    or definition.workflow == args.workflow
                )
                and (
                    args.version is None
                    or definition.workflow_version == args.version
                )
            ]
            identities = sorted(
                {(row.workflow, row.workflow_version) for row in metrics}
            )
            graphs = [
                graph
                for workflow, version in identities
                for graph in store.workflow_graphs(workflow, version, 5000)
            ]
        finally:
            store.close()
        print(
            render_workflow_recommendations(
                build_workflow_recommendations(metrics, definitions, graphs)
            )
        )

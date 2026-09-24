"""CLI adapter for workflow discovery, diagnostics, and analysis."""

from __future__ import annotations

import argparse
from collections.abc import Callable
from dataclasses import asdict
from typing import NoReturn

from ctrlrtn.recorder.sqlite.store import SqliteTraceStore
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
from ctrlrtn.workflow.proposal import (
    WorkflowProposalError,
    build_workflow_proposal,
    load_workflow_proposal,
    write_workflow_proposal,
)

DatabasePath = Callable[[], str]
Fail = Callable[[str], NoReturn]
ReadJson = Callable[[str, str], dict]


class WorkflowDiscoveryCommands:
    """Discover, compare, project, and identify workflow families."""

    def _workflow_discover(self, args: argparse.Namespace) -> None:
        if args.limit < 1:
            self._fail("--limit must be at least 1")
        if args.min_support < 2:
            self._fail("--min-support must be at least 2")
        if not 0 < args.similarity <= 1:
            self._fail("--similarity must be in (0, 1]")
        scope = WorkflowDiscoveryScope(
            since=args.since,
            until=args.until,
            provider=args.provider,
            model=args.model,
            experiment_id=args.experiment,
            arm=args.arm,
        )
        try:
            validate_workflow_discovery_scope(scope)
        except WorkflowDiscoveryJobError as exc:
            self._fail(str(exc))
        store = SqliteTraceStore(
            self._database_path(), read_only=not args.background
        )
        try:
            if args.background:
                try:
                    plan = prepare_workflow_discovery_job(
                        store,
                        limit=args.limit,
                        min_support=args.min_support,
                        similarity=args.similarity,
                        scope=scope,
                    )
                    store.create_job(plan.job)
                except WorkflowDiscoveryJobError as exc:
                    self._fail(str(exc))
                print(
                    f"Queued {plan.job.job_id} with {plan.traces} frozen "
                    "trace(s). Run `ctrlrtn worker`; monitor with "
                    "`jobs list` or the console."
                )
                print(render_workflow_discovery_selection(plan.selection))
                return
            traces = store.workflow_discovery_inputs(
                args.limit,
                since=scope.since,
                until=scope.until,
                provider=scope.provider,
                model=scope.model,
                experiment_id=scope.experiment_id,
                arm=scope.arm,
            )
            selection = asdict(
                store.workflow_discovery_input_diagnostics(
                    traces,
                    since=scope.since,
                    until=scope.until,
                    provider=scope.provider,
                    model=scope.model,
                    experiment_id=scope.experiment_id,
                    arm=scope.arm,
                )
            )
        finally:
            store.close()
        report = discover_workflow_families(
            traces,
            min_support=args.min_support,
            similarity=args.similarity,
        )
        print(render_workflow_discovery_selection(selection))
        print()
        print(render_workflow_discovery(report))

    def _workflow_discovery_compare(self, args: argparse.Namespace) -> None:
        store = SqliteTraceStore(self._database_path(), read_only=True)
        try:
            previous = store.job(args.previous_job)
            current = store.job(args.current_job)
        finally:
            store.close()
        for label, job in (("previous", previous), ("current", current)):
            if (
                job is None
                or job.kind != WORKFLOW_DISCOVERY_JOB_KIND
                or job.status != "succeeded"
                or job.result is None
            ):
                self._fail(f"{label} job is not a completed workflow discovery")
        try:
            comparison = compare_workflow_discovery_artifacts(
                previous.result,
                current.result,
                min_similarity=args.similarity,
                allow_unrelated=args.allow_unrelated,
            )
        except WorkflowDiscoveryJobError as exc:
            self._fail(str(exc))
        print(render_workflow_discovery_comparison(comparison))

    def _workflow_discovery_project(self, args: argparse.Namespace) -> None:
        store = SqliteTraceStore(self._database_path(), read_only=True)
        try:
            job = store.job(args.job_id)
        finally:
            store.close()
        if (
            job is None
            or job.kind != WORKFLOW_DISCOVERY_JOB_KIND
            or job.status != "succeeded"
            or job.result is None
        ):
            self._fail("job is not a completed workflow discovery")
        try:
            report = report_from_workflow_discovery_artifact(job.result)
            projection = next(
                (
                    item
                    for item in projections_from_workflow_discovery_artifact(
                        job.result
                    )
                    if item.family_id == args.family_id
                ),
                None,
            )
        except WorkflowDiscoveryJobError as exc:
            self._fail(str(exc))
        family = next(
            (
                item
                for item in report.families
                if item.family_id == args.family_id
            ),
            None,
        )
        if family is None or projection is None:
            self._fail(
                f"discovered family projection not found: {args.family_id}"
            )
        rendered = {
            "text": lambda: render_discovered_family_projection(projection),
            "mermaid": lambda: render_discovered_family_mermaid(
                projection, family.edges
            ),
            "json": lambda: render_discovered_family_json(projection).rstrip(),
        }[args.format]() + "\n"
        if args.output is None:
            print(rendered, end="")
            return
        try:
            with open(args.output, "x", encoding="utf-8") as handle:
                handle.write(rendered)
        except FileExistsError:
            self._fail(f"projection export already exists: {args.output}")
        except OSError as exc:
            self._fail(f"could not export workflow projection: {exc}")
        print(f"Wrote {args.format} projection to {args.output}.")

    def _workflow_identify(self, args: argparse.Namespace) -> None:
        if args.limit < 1:
            self._fail("--limit must be at least 1")
        if args.min_support < 2:
            self._fail("--min-support must be at least 2")
        if not 0 < args.similarity <= 1:
            self._fail("--similarity must be in (0, 1]")
        store = SqliteTraceStore(self._database_path(), read_only=True)
        try:
            traces = store.workflow_discovery_inputs(args.limit)
        finally:
            store.close()
        report = discover_workflow_families(
            traces,
            min_support=args.min_support,
            similarity=args.similarity,
        )
        family = next(
            (
                item
                for item in report.families
                if item.family_id == args.family_id
            ),
            None,
        )
        if family is None:
            self._fail(
                f"discovered workflow family not found: {args.family_id}"
            )
        try:
            proposal = build_workflow_proposal(
                family, args.workflow, args.workflow_version
            )
            write_workflow_proposal(args.output, proposal)
            load_workflow_proposal(args.output)
        except WorkflowProposalError as exc:
            self._fail(str(exc))
        print(
            f"Wrote inert workflow identification proposal {args.output} "
            f"({proposal['artifact_sha256']})."
        )
        print(
            "Review and merge its control fragment explicitly; nothing was "
            "activated."
        )

    def _workflow_identify_verify(self, args: argparse.Namespace) -> None:
        try:
            proposal = load_workflow_proposal(args.path)
        except WorkflowProposalError as exc:
            self._fail(str(exc))
        print(
            "Valid inert workflow identification proposal: "
            f"{proposal['artifact_sha256']}"
        )
        print(
            "No workflow, routing, experiment, or execution authority was "
            "activated."
        )

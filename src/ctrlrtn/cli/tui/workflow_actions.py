"""Workflow discovery, inspection, comparison, and proposal TUI actions."""

from __future__ import annotations

import sqlite3

from ctrlrtn.cli.tui.forms import (
    WorkflowDiscoveryScopeScreen,
    WorkflowIdentifyScreen,
    WorkflowProposalVerifyScreen,
)
from ctrlrtn.cli.tui.screens import (
    DiscoveredWorkflowDagScreen,
    WorkflowStepScreen,
)
from ctrlrtn.workflow.discovery_job import KIND as WORKFLOW_DISCOVERY_JOB_KIND
from ctrlrtn.workflow.discovery_job import (
    WorkflowDiscoveryJobError,
    WorkflowDiscoveryScope,
    compare_workflow_discovery_artifacts,
    prepare_workflow_discovery_job,
    projections_from_workflow_discovery_artifact,
    render_workflow_discovery_comparison,
)
from ctrlrtn.workflow.proposal import (
    WorkflowProposalError,
    build_workflow_proposal,
    load_workflow_proposal,
    write_workflow_proposal,
)


class WorkflowActions:
    def action_workflow_steps(self) -> None:
        if self._active_table() != "workflows":
            self._notice(
                "Focus a workflow task before opening step drill-down."
            )
            return
        task_id = self._selected.get("workflows")
        graph = self._store.workflow_graph(task_id) if task_id else None
        if graph is None:
            self._notice(
                "The selected workflow graph is unavailable.", error=True
            )
            return
        self.push_screen(WorkflowStepScreen(self._store, graph))

    def action_discovered_workflow_dag(self) -> None:
        if self._active_table() != "discovered":
            self._notice("Focus a discovered family before opening its DAG.")
            return
        family_id = self._selected.get("discovered")
        job = next(
            (
                item
                for item in self._jobs
                if item.job_id == self._discovery_job_id
                and item.result is not None
            ),
            None,
        )
        if not family_id or job is None:
            self._notice(
                "The selected discovered family is unavailable.", error=True
            )
            return
        try:
            projection = next(
                (
                    item
                    for item in projections_from_workflow_discovery_artifact(
                        job.result
                    )
                    if item.family_id == family_id
                ),
                None,
            )
        except WorkflowDiscoveryJobError as exc:
            self._notice(f"Cannot open discovered DAG: {exc}", error=True)
            return
        if projection is None:
            self._notice(
                "The selected discovered family is unavailable.", error=True
            )
            return
        self.push_screen(DiscoveredWorkflowDagScreen(projection))

    def action_discover_workflows(self) -> None:
        """Freeze and queue discovery; the normal worker owns clustering."""
        self._queue_workflow_discovery(WorkflowDiscoveryScope())

    def action_discover_workflows_scoped(self) -> None:
        self.push_screen(
            WorkflowDiscoveryScopeScreen(),
            self._queue_scoped_workflow_discovery,
        )

    def _queue_scoped_workflow_discovery(self, values: dict | None) -> None:
        if values is None:
            return
        self._queue_workflow_discovery(
            WorkflowDiscoveryScope(
                since=values["since"],
                until=values["until"],
                provider=values["provider"] or None,
                model=values["model"] or None,
                experiment_id=values["experiment"] or None,
                arm=values["arm"] or None,
            )
        )

    def _queue_workflow_discovery(self, scope: WorkflowDiscoveryScope) -> None:
        try:
            store = self._control_store()
            try:
                plan = prepare_workflow_discovery_job(store, scope=scope)
                store.create_job(plan.job)
            finally:
                store.close()
        except (WorkflowDiscoveryJobError, ValueError, sqlite3.Error) as exc:
            self._notice(
                f"could not queue workflow discovery: {exc}", error=True
            )
            return
        self._notice(
            f"Queued {plan.job.job_id} with {plan.traces} frozen trace(s); "
            "run the worker and monitor Jobs."
        )
        self._reload()

    def action_identify_workflow(self) -> None:
        if self._active_table() != "discovered":
            self._notice(
                "Focus a discovered workflow family first.", error=True
            )
            return
        family_id = self._selected.get("discovered")
        if not family_id:
            self._notice(
                "No discovered workflow family is selected.", error=True
            )
            return
        self.push_screen(
            WorkflowIdentifyScreen(family_id),
            lambda values: self._create_workflow_proposal(family_id, values),
        )

    def action_compare_workflow_discoveries(self) -> None:
        if self._active_table() != "jobs":
            self._notice("Focus a completed discovery job to compare it.")
            return
        job_id = self._selected.get("jobs")
        current = next(
            (
                job
                for job in self._jobs
                if job.job_id == job_id
                and job.kind == WORKFLOW_DISCOVERY_JOB_KIND
                and job.status == "succeeded"
                and job.result is not None
            ),
            None,
        )
        if current is None:
            self._notice("The selected job is not a completed discovery.")
            return
        previous = next(
            (
                job
                for job in self._jobs
                if job.kind == WORKFLOW_DISCOVERY_JOB_KIND
                and job.status == "succeeded"
                and job.result is not None
                and job.created_at < current.created_at
            ),
            None,
        )
        if previous is None:
            self._notice("No earlier completed discovery is available.")
            return
        try:
            comparison = compare_workflow_discovery_artifacts(
                previous.result, current.result
            )
        except WorkflowDiscoveryJobError as exc:
            self._notice(f"could not compare discoveries: {exc}", error=True)
            return
        self._show(render_workflow_discovery_comparison(comparison))

    def _create_workflow_proposal(
        self, family_id: str, values: dict | None
    ) -> None:
        if values is None:
            return
        family = next(
            (item for item in self._discovered if item.family_id == family_id),
            None,
        )
        if family is None:
            self._notice(
                "Discovered family is no longer available.", error=True
            )
            return
        try:
            proposal = build_workflow_proposal(
                family, values["workflow"], values["workflow_version"]
            )
            write_workflow_proposal(values["output"], proposal)
            load_workflow_proposal(values["output"])
        except (WorkflowProposalError, OSError) as exc:
            self._notice(
                f"could not create workflow proposal: {exc}", error=True
            )
            return
        self._notice(
            f"Created inert workflow proposal {values['output']}; nothing activated."
        )

    def action_verify_workflow_proposal(self) -> None:
        self.push_screen(
            WorkflowProposalVerifyScreen(), self._verify_workflow_proposal
        )

    def _verify_workflow_proposal(self, path: str | None) -> None:
        if path is None:
            return
        try:
            proposal = load_workflow_proposal(path)
        except WorkflowProposalError as exc:
            self._notice(
                f"workflow proposal verification failed: {exc}", error=True
            )
            return
        self._notice(
            f"Verified inert workflow proposal {proposal['artifact_sha256'][:16]}; "
            "nothing activated."
        )

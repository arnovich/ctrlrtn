"""Background-job action orchestration for the TUI."""

from __future__ import annotations

import sqlite3

from ctrlrtn.cli.tui.control_actions import ControlActions
from ctrlrtn.cli.tui.forms import (
    ConfirmScreen,
    OfflineExperimentScreen,
)
from ctrlrtn.cli.tui.workflow_actions import WorkflowActions
from ctrlrtn.eval.ni import _MIN_UNITS as MIN_NI_UNITS
from ctrlrtn.jobs.replay import ReplayJobPlan, prepare_replay_job
from ctrlrtn.recorder.sqlite.store import SqliteTraceStore
from ctrlrtn.telemetry import pricing


class ConsoleActions(WorkflowActions, ControlActions):
    def _control_store(self) -> SqliteTraceStore:
        if not self._db_path:
            raise ValueError("console control actions require a database path")
        return SqliteTraceStore(self._db_path)

    def action_new_offline(self) -> None:
        selected = self._selected.get("usecases")
        use_case = selected or (
            self._rankings[0].use_case if self._rankings else ""
        )
        self.push_screen(
            OfflineExperimentScreen(
                use_case,
                use_cases=self._known_use_cases(),
                models=self._known_models(),
            ),
            self._review_offline_plan,
        )

    def _known_use_cases(self) -> tuple[str, ...]:
        """Use-cases with recorded traffic — the only ones there is anything
        to replay for."""
        return tuple(row.use_case for row in self._rankings)

    def _known_models(self) -> tuple[str, ...]:
        """Models the router can price, plus any it has actually served. A
        candidate is often one you have never run, so these are completions,
        not a closed list: anything typed is accepted."""
        served = {row.model for row in self._models if row.model != "(unknown)"}
        return tuple(sorted(set(pricing.known_models()) | served))

    def _review_offline_plan(self, values: dict | None) -> None:
        if values is None:
            return
        try:
            store = self._control_store()
            try:
                plan = prepare_replay_job(store, **values)
            finally:
                store.close()
        except (ValueError, sqlite3.Error) as exc:
            self._notice(str(exc), error=True)
            return
        warning = (
            f"\nWARNING: only {plan.units} independent units; at least "
            f"{MIN_NI_UNITS} are needed for a powered conclusion."
            if plan.units < MIN_NI_UNITS
            else ""
        )
        message = (
            f"Offline replay plan\n\n"
            f"use-case: {values['use_case']}\n"
            f"scope: "
            f"{(plan.job.config['workflow'] + '@' + plan.job.config['workflow_version'] + '/' + plan.job.config['step']) if plan.job.config.get('workflow') else 'whole use-case'}\n"
            f"baseline: {plan.job.config['baseline_model']}\n"
            f"candidate: {values['candidate_model']}\n"
            f"inputs: {len(plan.rows)} ({plan.units} independent units)\n"
            f"replay calls: {plan.replay_calls}\n"
            f"judge calls: {plan.judge_calls}\n"
            f"total upstream calls billed to the worker key: {plan.total_calls}"
            f"{warning}"
        )
        self.push_screen(
            ConfirmScreen(message, "Queue paid replay"),
            lambda confirmed: self._queue_offline_plan(plan, confirmed),
        )

    def _queue_offline_plan(self, plan: ReplayJobPlan, confirmed: bool) -> None:
        if not confirmed:
            return
        try:
            store = self._control_store()
            try:
                store.create_job(plan.job)
            finally:
                store.close()
        except (ValueError, sqlite3.Error) as exc:
            self._notice(f"could not queue job: {exc}", error=True)
            return
        self._notice(f"Queued {plan.job.job_id}")
        self._reload()

    def action_cancel_job(self) -> None:
        if self._active_table() != "jobs":
            self._notice("Focus a queued or running job before cancelling.")
            return
        job_id = self._selected["jobs"]
        job = next((row for row in self._jobs if row.job_id == job_id), None)
        if job is None or job.status not in {"queued", "running"}:
            self._notice("The selected job is not queued or running.")
            return
        self.push_screen(
            ConfirmScreen(f"Cancel {job.job_id}?", "Cancel job"),
            lambda confirmed: self._cancel_job(job.job_id, confirmed),
        )

    def _cancel_job(self, job_id: str, confirmed: bool) -> None:
        if not confirmed:
            return
        try:
            store = self._control_store()
            try:
                changed = store.request_job_cancel(job_id)
            finally:
                store.close()
        except sqlite3.Error as exc:
            self._notice(f"could not cancel job: {exc}", error=True)
            return
        self._notice(
            f"Cancellation requested for {job_id}."
            if changed
            else f"Job {job_id} is no longer cancellable."
        )
        self._reload()

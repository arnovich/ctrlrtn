"""Experiment, shadow, routing, and Git-config TUI actions."""

from __future__ import annotations

import sqlite3

from ctrlrtn.cli.tui.forms import (
    ConfirmScreen,
    LiveExperimentScreen,
    _provider_exists,
    _unknown_provider,
)
from ctrlrtn.control.service import (
    AdoptionPlan,
    apply_adoption,
    prepare_adoption,
)
from ctrlrtn.policy.experiment import (
    Experiment,
)


class LiveExperimentControlActions:
    """Create, stop, and adopt live experiments."""

    def action_new_live(self) -> None:
        self.push_screen(
            LiveExperimentScreen(self._selected_use_case()),
            self._review_live_experiment,
        )

    def _review_live_experiment(self, values: dict | None) -> None:
        if values is None:
            return
        provider_error = _unknown_provider(values["candidate_provider"])
        if provider_error:
            self._notice(provider_error, error=True)
            return
        try:
            exp = Experiment(**values)
        except ValueError as exc:
            self._notice(f"invalid experiment: {exc}", error=True)
            return
        message = (
            "Start live A/B experiment\n\n"
            f"use-case: {exp.use_case_key}\n"
            f"scope: {exp.scope.label}\n"
            f"candidate: {exp.candidate_model}"
            f"{f' via {exp.candidate_provider}' if exp.candidate_provider else ''}\n"
            f"traffic: {exp.split_pct}% candidate / {100 - exp.split_pct}% baseline\n"
            f"candidate ceiling: {exp.max_calls_per_task} calls/task\n\n"
            "Task assignment is sticky. The gateway applies this within ~10s."
        )
        self.push_screen(
            ConfirmScreen(message, "Start live A/B"),
            lambda confirmed: self._start_live_experiment(exp, confirmed),
        )

    def _start_live_experiment(self, exp: Experiment, confirmed: bool) -> None:
        if not confirmed:
            return
        try:
            store = self._control_store()
            try:
                store.create_experiment(exp)
            finally:
                store.close()
        except (ValueError, sqlite3.Error) as exc:
            self._notice(f"could not start experiment: {exc}", error=True)
            return
        self._notice(
            f"Started {exp.experiment_id}; gateway updates within ~10s."
        )
        self._reload()

    def action_stop_experiment(self) -> None:
        exp = self._selected_experiment()
        if exp is None or not exp.is_running:
            self._notice("Focus a running experiment before stopping it.")
            return
        self.push_screen(
            ConfirmScreen(
                f"Stop {exp.experiment_id}?\n\n"
                "Traffic returns to the persistent route, if present, otherwise "
                "pass-through.",
                "Stop A/B",
            ),
            lambda confirmed: self._stop_experiment(
                exp.experiment_id, confirmed
            ),
        )

    def _stop_experiment(self, experiment_id: str, confirmed: bool) -> None:
        if not confirmed:
            return
        try:
            store = self._control_store()
            try:
                stopped = store.stop_experiment(experiment_id)
            finally:
                store.close()
        except sqlite3.Error as exc:
            self._notice(f"could not stop experiment: {exc}", error=True)
            return
        self._notice(
            f"Stopped {experiment_id}; gateway updates within ~10s."
            if stopped
            else f"Experiment {experiment_id} is no longer running."
        )
        self._reload()

    def action_adopt_experiment(self) -> None:
        exp = self._selected_experiment()
        if exp is None:
            self._notice("Focus an experiment before adopting its candidate.")
            return
        try:
            plan = prepare_adoption(
                self._store,
                exp.experiment_id,
                provider_exists=_provider_exists,
            )
        except ValueError as exc:
            self._notice(str(exc), error=True)
            return
        warnings = "".join(
            f"\nWARNING: {notice.message}." for notice in plan.notices
        )
        message = (
            f"Adopt candidate from {plan.experiment.experiment_id}\n\n"
            f"use-case: {plan.route.use_case_key}\n"
            f"route: {plan.route.previous_model or '(unknown)'} -> "
            f"{plan.route.model}\n"
            f"provider: {plan.route.provider or 'baseline provider'}"
            f"{warnings}\n\n"
            "This stops the experiment if running and routes 100% of matching "
            "traffic to the candidate."
        )
        self.push_screen(
            ConfirmScreen(message, "Adopt candidate"),
            lambda confirmed: self._adopt_experiment(plan, confirmed),
        )

    def _adopt_experiment(self, plan: AdoptionPlan, confirmed: bool) -> None:
        if not confirmed:
            return
        try:
            store = self._control_store()
            try:
                applied = apply_adoption(store, plan)
            finally:
                store.close()
        except (ValueError, sqlite3.Error) as exc:
            self._notice(f"could not adopt candidate: {exc}", error=True)
            return
        if not applied:
            self._notice(
                f"Experiment {plan.experiment.experiment_id} no longer exists.",
                error=True,
            )
            return
        self._notice(
            f"Routing {plan.route.use_case_key} -> {plan.route.model}; "
            "gateway updates within ~10s."
        )
        self._reload()

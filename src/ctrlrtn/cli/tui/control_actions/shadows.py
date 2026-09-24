"""Experiment, shadow, routing, and Git-config TUI actions."""

from __future__ import annotations

import sqlite3

from ctrlrtn.cli.tui.forms import (
    ConfirmScreen,
    ShadowExperimentScreen,
    _unknown_provider,
)
from ctrlrtn.policy.shadow import ShadowExperiment


class ShadowControlActions:
    def action_new_shadow(self) -> None:
        self.push_screen(
            ShadowExperimentScreen(self._selected_use_case()),
            self._review_shadow,
        )

    def _review_shadow(self, values: dict | None) -> None:
        if values is None:
            return
        provider_error = _unknown_provider(values["candidate_provider"])
        if provider_error:
            self._notice(provider_error, error=True)
            return
        try:
            experiment = ShadowExperiment(**values)
        except ValueError as exc:
            self._notice(f"invalid shadow experiment: {exc}", error=True)
            return
        message = (
            "Start online shadow experiment\n\n"
            f"use-case: {experiment.use_case_key}\n"
            f"scope: {experiment.scope.label}\n"
            f"candidate: {experiment.candidate_model}\n"
            f"provider: {experiment.candidate_provider or 'baseline provider'}\n"
            f"mirror: {experiment.sample_pct}% of eligible live inputs\n\n"
            "Candidate output is recorded but never served to users."
        )
        self.push_screen(
            ConfirmScreen(message, "Start shadow"),
            lambda confirmed: self._start_shadow(experiment, confirmed),
        )

    def _start_shadow(
        self, experiment: ShadowExperiment, confirmed: bool
    ) -> None:
        if not confirmed:
            return
        try:
            store = self._control_store()
            try:
                store.create_shadow_experiment(experiment)
            finally:
                store.close()
        except (ValueError, sqlite3.Error) as exc:
            self._notice(f"could not start shadow: {exc}", error=True)
            return
        self._notice(
            f"Started {experiment.shadow_id}; gateway updates within ~10s."
        )
        self._reload()

    def action_stop_shadow(self) -> None:
        if self._active_table() != "shadows":
            self._notice("Focus a running shadow before stopping it.")
            return
        shadow_id = self._selected.get("shadows")
        experiment = next(
            (row for row in self._shadows if row.shadow_id == shadow_id), None
        )
        if experiment is None or not experiment.is_running:
            self._notice("The selected shadow is not running.")
            return
        self.push_screen(
            ConfirmScreen(f"Stop {experiment.shadow_id}?", "Stop shadow"),
            lambda confirmed: self._stop_shadow(
                experiment.shadow_id, confirmed
            ),
        )

    def _stop_shadow(self, shadow_id: str, confirmed: bool) -> None:
        if not confirmed:
            return
        try:
            store = self._control_store()
            try:
                stopped = store.stop_shadow_experiment(shadow_id)
            finally:
                store.close()
        except sqlite3.Error as exc:
            self._notice(f"could not stop shadow: {exc}", error=True)
            return
        self._notice(
            f"Stopped {shadow_id}; gateway updates within ~10s."
            if stopped
            else f"Shadow {shadow_id} is no longer running."
        )
        self._reload()
